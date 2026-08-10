"""Shared harness for the jellyfin-mpv-shim integration / concurrency suite.

This module holds the reusable test doubles and probes the heavy tests build
on. Nothing here launches mpv, Tk, or a server at import time — everything is
opt-in behind a function call or a capability gate, so importing the harness is
cheap and side-effect free (the fast suite never touches it).

Contents:

* Capability probes (``HAVE_*``) + ``require_*`` skip helpers.
* ``FakeMPV`` — a scriptable stand-in for the python-mpv / jsonipc backend that
  records the observer/event/key callbacks ``PlayerManager`` registers and lets
  a test fire them on an arbitrary thread. This is what makes the player
  state-machine races reproducible without a real libmpv.
* ``import_player_with_fake_mpv`` — installs ``FakeMPV`` as the ``mpv`` module
  and imports ``jellyfin_mpv_shim.player`` against it, so the module-level
  ``PlayerManager()`` singleton constructs without a real player/window.
* Concurrency-forcing helpers (``run_concurrently``, ``spin_barrier``).
* ``make_test_clip`` — ffmpeg-generated deterministic sample media (Tier 2).
"""

import functools
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest


# --------------------------------------------------------------------------
# Capability probes
# --------------------------------------------------------------------------

def _probe_import(modname):
    try:
        __import__(modname)
        return True
    except Exception:
        return False


HAVE_MPV_LIB = _probe_import("mpv")               # libmpv binding
HAVE_MPV_JSONIPC = _probe_import("python_mpv_jsonipc")

# Which mpv backend this process is exercising. Set by run_integration.py per
# matrix leg (a fresh subprocess per backend, so player.py's import-time backend
# selection and the interdependent singletons start clean each time). Defaults
# to libmpv for a bare ``python -m unittest`` run.
BACKEND = os.environ.get("JMS_TEST_BACKEND", "libmpv")
assert BACKEND in ("libmpv", "jsonipc"), "unknown JMS_TEST_BACKEND %r" % BACKEND
HAVE_FFMPEG = shutil.which("ffmpeg") is not None
HAVE_MPV_BIN = shutil.which("mpv") is not None
HAVE_XVFB = shutil.which("Xvfb") is not None or shutil.which("xvfb-run") is not None
# Windows has no DISPLAY variable and no Xvfb -- the session's desktop is
# simply there -- so the X-shaped probe answers "headless" on a machine that
# can open a window fine. That answer is worse than useless here: every
# @require_real_mpv class self-skips, and run_integration counts a skip as a
# pass, so a Windows CI leg reports green having started no real player at
# all. (--strict is the other half of that guard.)
HAVE_DISPLAY = bool(
    os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
) or os.name == "nt"
# A real mpv smoke test needs a working X display: either an inherited one, or
# xvfb to conjure one. The runner (run_integration.py) re-execs itself under
# xvfb-run when no display is present, so by the time a test runs we only need
# "a display exists".
HAVE_MPV_DISPLAY = (HAVE_MPV_LIB or HAVE_MPV_JSONIPC) and HAVE_MPV_BIN and (
    HAVE_DISPLAY or HAVE_XVFB
)


def require_ffmpeg(obj):
    return unittest.skipUnless(HAVE_FFMPEG, "ffmpeg not available")(obj)


def require_real_mpv(obj):
    return unittest.skipUnless(
        HAVE_MPV_DISPLAY and HAVE_FFMPEG,
        "real mpv smoke needs mpv + a display (xvfb) + ffmpeg",
    )(obj)


# --------------------------------------------------------------------------
# FakeMPV — scriptable player backend
# --------------------------------------------------------------------------

class ShutdownError(Exception):
    """Mirror of libmpv's ShutdownError so player.py's _mpv_errors tuple picks
    up the libmpv branch (BrokenPipeError, ShutdownError) when FakeMPV stands in
    as the ``mpv`` module."""


#: What mpv itself binds, in the shape ``input-bindings`` reports it —
#: enough of it for a key sweep to have something to find. Weak, because
#: that is what mpv marks its own builtins with and what tells them from a
#: line the user wrote; the seek distances and the fullscreen toggle are
#: mpv's real defaults, character for character, because ``_seek_is_ours``
#: compares against exactly those.
DEFAULT_BINDINGS = [
    {"key": "SPACE", "cmd": "cycle pause", "is_weak": True, "priority": -1},
    {"key": "p", "cmd": "cycle pause", "is_weak": True, "priority": -1},
    {"key": "f", "cmd": "cycle fullscreen", "is_weak": True, "priority": -1},
    {"key": "LEFT", "cmd": "seek -5", "is_weak": True, "priority": -1},
    {"key": "RIGHT", "cmd": "seek 5", "is_weak": True, "priority": -1},
    {"key": "UP", "cmd": "seek 60", "is_weak": True, "priority": -1},
    {"key": "DOWN", "cmd": "seek -60", "is_weak": True, "priority": -1},
    {"key": "m", "cmd": "cycle mute", "is_weak": True, "priority": -1},
    {"key": "WHEEL_LEFT", "cmd": "seek -10", "is_weak": True,
     "priority": -1},
    {"key": "WHEEL_RIGHT", "cmd": "seek 10", "is_weak": True,
     "priority": -1},
]


class FakeMPV:
    """A stand-in for an mpv backend object.

    It supports the two surfaces ``PlayerManager`` uses:

    * *registration* — the ``on_key_press`` / ``property_observer`` /
      ``event_callback`` decorators used in ``_init_mpv``. Registered callbacks
      are stored so a test can fire them later (``fire_property`` / ``fire_event``),
      optionally from another thread, to reproduce observer-ordering races.
    * *input sections* — ``define-section`` / ``enable-section`` /
      ``disable-section`` are kept as state, and ``press_key`` consults them
      before the script bindings, so a #16 key *claim* can be exercised
      rather than merely recorded.
    * *control / property* access — ``command``, ``play``, ``show_text`` etc. are
      recorded; the many scalar properties (``pause``, ``playback_abort``,
      ``playback_time`` …) are plain attributes tests can set to script state.

    Property reads/writes can be made to raise a "disconnect" error to exercise
    the ``_mpv_errors`` handling paths (``fail_with``).
    """

    # Class attr so ``hasattr(mpv, "ShutdownError")`` is true on the module.
    ShutdownError = ShutdownError

    def __init__(self, **options):
        # What _init_mpv asked mpv to start with. Most option plumbing is not
        # worth re-testing here, but options that must be set *at startup*
        # are: mpv before 0.41 ignores a runtime force-window while idle, so
        # a startup-only option is the difference between a window and none.
        self.init_options = dict(options)

        # Scalar player properties with defaults matching an idle player.
        self.playback_abort = True
        self.playback_time = None
        self.duration = None
        self.pause = False
        self.volume = 100
        self.mute = False
        self.speed = 1.0
        self.cache_buffering_state = 0
        self.fs = False
        self.sub = "no"
        self.audio = "auto"
        self.osc = False
        self.keep_open = False
        self.force_window = False
        self.resume_playback = True
        self.image_display_duration = 1
        self.screenshot_directory = None
        self.input_ipc_server = None
        self.force_media_title = None
        self.sub_pos = 100
        self.sub_scale = 1.0
        self.sub_color = "#FFFFFFFF"
        self.osd_back_color = "#C8000000"
        self.osd_font_size = 55
        self.osd_border_style = "outline-and-shadow"

        # Registered callbacks.
        self._property_observers = {}   # name -> [callbacks]
        self._event_callbacks = {}      # name -> [callbacks]
        self._key_bindings = {}         # key -> callback

        # What `input-bindings` answers, which is what a key sweep reads
        # (#16). A fake without it is not a player with no bindings -- the
        # sweep's read raises, player.py logs it and carries on with an
        # EMPTY sweep, so every claim installs nothing and every claimed
        # key silently does nothing. That is what it was, and it is why
        # five keyboard tests failed pointing at innocent code.
        self.input_bindings = [dict(b) for b in DEFAULT_BINDINGS]

        # Input sections: `name -> {key: command}`, plus the enable order.
        # A claim is a SECTION rather than per-key bindings (the one
        # mechanism both backends have), so a fake that models only
        # on_key_press cannot see a claimed key at all.
        self._sections = {}
        self._enabled = []

        # Records for assertions.
        self.commands = []
        self.played = []
        self.texts = []
        self.terminated = False
        self._sub_counter = 0

        # If set to an exception instance/class, property access raises it (to
        # simulate an mpv that died under us).
        self.fail_with = None

    # -- registration decorators (used by PlayerManager._init_mpv) ----------

    def on_key_press(self, key):
        def deco(func):
            self._key_bindings[key] = func
            return func

        return deco

    def property_observer(self, name):
        def deco(func):
            self._property_observers.setdefault(name, []).append(func)
            return func

        return deco

    def event_callback(self, name):
        def deco(func):
            self._event_callbacks.setdefault(name, []).append(func)
            return func

        return deco

    # jsonipc-style aliases, provided for completeness.
    def bind_property_observer(self, name, func):
        self._property_observers.setdefault(name, []).append(func)
        return len(self._property_observers[name])

    def bind_event(self, name, func):
        self._event_callbacks.setdefault(name, []).append(func)

    # -- test drivers -------------------------------------------------------

    def fire_property(self, name, value):
        """Invoke every observer registered for ``name`` with (name, value),
        mirroring an mpv property-change notification. Run this from a spawned
        thread to reproduce an observer firing off the player thread."""
        setattr(self, name.replace("-", "_"), value)
        for cb in list(self._property_observers.get(name, [])):
            cb(name, value)

    def fire_event(self, name, event=None):
        for cb in list(self._event_callbacks.get(name, [])):
            cb(event)

    def press_key(self, key):
        """Deliver a key the way mpv would: enabled sections first, then the
        script bindings ``on_key_press`` registered.

        Both sections the shim installs are defined ``force``, which in mpv
        outranks everything else, and the later-enabled section wins -- so
        the menu's arrows beat a standing seek claim while the menu is up,
        which is the ordering the two claimants were designed around.

        Key names match **exactly**, where mpv compares named keys without
        case. So press the spelling the thing under test used: a claim
        carries whatever ``input-bindings`` reported (``SPACE``, ``LEFT``),
        a Python binding whatever the setting holds (``space``, ``left``).
        Not worth emulating -- the alternative is a fake that quietly
        answers a question about mpv's parser that no test here is asking.
        """
        for name in reversed(self._enabled):
            cmd = self._sections.get(name, {}).get(key)
            if cmd is None:
                continue
            self._run_section_command(cmd)
            return
        cb = self._key_bindings.get(key)
        if cb is not None:
            cb()

    def _run_section_command(self, cmd):
        """What a section line does when its key is pressed. shlex, because
        ``keysweep.section_lines`` quotes the key it passes along."""
        parts = shlex.split(cmd)
        if not parts or parts[0] == "ignore":
            # `ignore` is mpv dropping the key. Recorded nowhere on
            # purpose: the point of a suppression is that nothing sees it.
            return
        if parts[0] == "script-message":
            # Which is how a claimed key reaches Python: as a client
            # message carrying the semantic and the key it came from.
            self.fire_event("client-message", {"args": parts[1:]})
            return
        self.commands.append(tuple(parts))

    # -- control surface ----------------------------------------------------

    def command(self, *args):
        if self.fail_with is not None:
            raise self.fail_with
        self.commands.append(args)
        self._section_command(args)

    def _section_command(self, args):
        """define/enable/disable-section, kept as state rather than only as
        a recorded call -- otherwise a test can assert that a claim was
        INSTALLED and never that it does anything."""
        if not args:
            return
        verb = args[0]
        if verb == "define-section" and len(args) >= 3:
            lines = {}
            for line in (args[2] or "").splitlines():
                line = line.strip()
                if not line:
                    continue
                key, _sp, cmd = line.partition(" ")
                lines[key] = cmd.strip()
            self._sections[args[1]] = lines
        elif verb == "enable-section" and len(args) >= 2:
            # Re-enabling moves a section to the top, as mpv does.
            self._disable_section(args[1])
            self._enabled.append(args[1])
        elif verb == "disable-section" and len(args) >= 2:
            self._disable_section(args[1])

    def _disable_section(self, name):
        self._enabled = [n for n in self._enabled if n != name]

    def play(self, url):
        self.played.append(url)
        # A real play() clears the aborted/idle state; duration becomes known
        # shortly after. Tests that use wait_property drive that separately.
        self.playback_abort = False

    def show_text(self, text, duration=None, level=None):
        self.texts.append((text, duration, level))

    def sub_add(self, url):
        self._sub_counter += 1
        self.sub = self._sub_counter
        return self._sub_counter

    def screenshot(self):
        self.commands.append(("screenshot",))

    def terminate(self):
        self.terminated = True

    # Property access hook for the fail_with paths. We can't intercept normal
    # attribute reads cheaply without __getattribute__ gymnastics, so tests that
    # need a raising read use ``raise_on`` below instead.
    def raise_on_next_property(self, exc):
        self.fail_with = exc


def make_fake_mpv_module(backend="libmpv"):
    """Return an object usable as a stand-in for the imported mpv backend.

    Both backends expose an ``MPV`` factory; only libmpv (python-mpv) exposes
    ``ShutdownError``. player.py keys its ``_mpv_errors`` tuple off exactly that
    (``hasattr(mpv, "ShutdownError")``), so the presence/absence here is what
    makes the two matrix legs diverge — libmpv -> (BrokenPipeError,
    ShutdownError), jsonipc -> (BrokenPipeError, TimeoutError)."""
    import types

    name = "mpv" if backend == "libmpv" else "python_mpv_jsonipc"
    mod = types.ModuleType(name)
    mod.MPV = FakeMPV
    if backend == "libmpv":
        mod.ShutdownError = ShutdownError
    return mod


def backend_disconnect_error(player_module):
    """The backend-specific 'mpv is gone' exception type — the second member of
    ``_mpv_errors`` (ShutdownError on libmpv, TimeoutError on jsonipc). Used by
    the matrix tests to prove the disconnect guards catch the *divergent*
    member, not just the shared BrokenPipeError."""
    return player_module._mpv_errors[1]


def prime_args(config_dir=None):
    """Prime ``args.get_args()``'s module-level cache with a clean parse.

    The app parses ``sys.argv`` the first time any module resolves the config
    dir; under a test runner ``sys.argv`` carries pytest/unittest tokens the
    app's argparse rejects. Parsing once here against a clean argv (optionally
    pinning ``--config`` to a temp dir) caches a valid Namespace for the rest of
    the process, matching how the fast suite's single_instance test sidesteps
    the same trap by mocking ``conffile.get``.
    """
    import jellyfin_mpv_shim.args as args_mod
    if args_mod._args is not None:
        return args_mod._args
    argv = ["jellyfin-mpv-shim"]
    if config_dir is not None:
        argv += ["--config", config_dir]
    saved = sys.argv
    sys.argv = argv
    try:
        return args_mod.get_args()
    finally:
        sys.argv = saved


_PLAYER_MODULE = None


def import_player_with_fake_mpv():
    """Import ``jellyfin_mpv_shim.player`` bound to :class:`FakeMPV`.

    player.py does a bare ``import mpv`` and constructs a module-level
    ``PlayerManager()`` singleton at import time (which would otherwise launch a
    real player + window). We install a fake ``mpv`` module and quiet the
    settings that would pull in trickplay / shader packs / the OSC, then import
    once and cache the module.

    Returns the imported ``player`` module. ``player.PlayerManager`` is the class
    under test; use :func:`build_player` to get a controllable instance.
    """
    global _PLAYER_MODULE
    if _PLAYER_MODULE is not None:
        return _PLAYER_MODULE

    if "jellyfin_mpv_shim.player" in sys.modules:
        # Something already imported the real thing; refuse rather than pretend.
        raise RuntimeError(
            "jellyfin_mpv_shim.player already imported without the fake mpv; "
            "import_player_with_fake_mpv must run first."
        )

    # Keep config writes out of the user's real ~/.config, and pin the arg
    # parser to it so confdir resolution doesn't choke on the runner's argv.
    tmp_conf = tempfile.mkdtemp(prefix="jms-itest-conf-")
    os.environ["XDG_CONFIG_HOME"] = tmp_conf
    prime_args(tmp_conf)

    from jellyfin_mpv_shim.conf import settings
    # Disable the heavyweight optional features so _init_mpv / OSDMenu build
    # against the fake without touching disk shaders or spawning threads.
    settings.thumbnail_enable = False
    settings.shader_pack_enable = False
    settings.menu_mouse = False
    settings.svp_enable = False
    settings.discord_presence = False
    # "none": no shim OSC lua loaded AND mpv's own OSC suppressed. This
    # was `osc_style = "default"` plus `enable_osc = False` until #615
    # retired the second half, which left the write dead and these legs
    # quietly running with mpv's OSC up.
    settings.osc_style = "none"
    settings.check_updates = False

    # Flip the import-time backend selector: player.py imports libmpv when
    # mpv_ext is false, else python_mpv_jsonipc. Install the matching fake
    # module so the real backend never loads.
    # NOTE: overwrite, don't setdefault — the capability probes at harness
    # import already loaded the *real* backend module into sys.modules, and a
    # setdefault would leave that in place (silently constructing a real mpv).
    if BACKEND == "jsonipc":
        settings.mpv_ext = True
        settings.mpv_ext_start = False       # don't try to spawn a real mpv
        name = "python_mpv_jsonipc"
    else:
        settings.mpv_ext = False
        name = "mpv"
    real = sys.modules.get(name)
    sys.modules[name] = make_fake_mpv_module(BACKEND)

    try:
        import jellyfin_mpv_shim.player as player_module
    finally:
        # Put the real backend back. player.py did `import mpv` while the fake
        # was installed, so its module global still points at the fake and the
        # FakeMPV-driven tests are unaffected — but sys.modules is process-wide
        # and permanent, so leaving the fake there handed it to every *later*
        # importer too. That is what made the whole integration suite look
        # flaky: tests/integration/test_mpvtk_browser.py does `import mpv as
        # libmpv` to spawn a real handle, got the fake once any player test had
        # run first, and every real-mpv test after it failed with "renderer
        # never became ready" — 15s of timeout each. In isolation they passed,
        # which is exactly what a resource-contention problem looks like.
        if real is not None:
            sys.modules[name] = real
        else:
            sys.modules.pop(name, None)

    _PLAYER_MODULE = player_module
    return player_module


def build_player(player_module, video=None):
    """Construct a ``PlayerManager`` bypassing ``__init__`` and wire the minimal
    state the state-machine methods touch, backed by a fresh :class:`FakeMPV`.

    We deliberately avoid the real ``__init__`` here: the goal is to drive the
    epoch / lock / queue logic in isolation, not to re-test mpv option plumbing.
    Collaborators the tested methods call out to (``play``, timeline sends) are
    left as real methods; tests stub the ones they want to observe.
    """
    from queue import Queue
    from threading import RLock, Lock, Event
    from jellyfin_mpv_shim.utils import Timer
    from jellyfin_mpv_shim.session_reporter import SessionReporter

    PlayerManager = player_module.PlayerManager
    pm = PlayerManager.__new__(PlayerManager)

    pm._player = FakeMPV()
    pm._video = video
    pm.evt_queue = Queue()
    pm._lock = RLock()
    pm._tl_lock = RLock()
    # Audio state. _init_mpv calls apply_audio_settings, which is wrapped in a
    # try/except so a half-built mpv can't abort the init -- meaning a missing
    # attribute here shows up as a logged traceback rather than a failure.
    pm._audio_lock = RLock()
    pm._audio_configured = False
    pm._audio_snapshot = None
    # Separate slot from _audio_snapshot: the chosen output device is not part
    # of the audio *mode*, so it must survive a return to "auto".
    pm._device_snapshot = None
    # Whether this mpv honours a runtime force-window change; _init_mpv reads
    # it off the real version. FakeMPV has none, so assume the modern
    # behaviour and let the tests that care set it explicitly.
    pm._runtime_force_window = True
    pm._finished_lock = Lock()
    pm.timeline_trigger = None
    pm.action_trigger = None
    pm._track_memory = None
    pm.external_subtitles = {}
    pm.external_subtitles_rev = {}
    pm.should_send_timeline = False
    pm.start_time = 0.0
    pm.url = None
    pm.last_update = Timer()
    pm._jf_settings = None
    pm.pause_ignore = None
    pm.do_not_handle_pause = False
    pm._last_offline_record = float("-inf")
    pm.last_seek = None
    pm.warned_about_transcode = False
    pm.fullscreen_disable = False
    pm.is_in_intro = False
    pm.playback_time_before_seek = None
    pm.trickplay = None
    pm._mpv_alive = True
    pm._idle_quit = False
    pm._terminate_thread = None
    pm._last_offline_record = float("-inf")
    pm._play_epoch = 0
    pm._reached_eof = False
    pm._last_playback_position = 0
    pm._stall_position = None
    pm._stall_since = 0.0
    pm._last_intro_msg_time = 0.0

    # #16 key claims. `_swept`/`_swept_ptr` are None -- the *uncached*
    # state -- so a claim really does sweep FakeMPV.input_bindings rather
    # than being handed a prepared answer: that sweep is the half of the
    # feature a fake can get wrong without anything noticing.
    pm._key_claims = {}
    pm._key_actions = {}
    pm._swept = None
    pm._swept_ptr = None
    # The lua probe, unanswered. None means "not asked yet" for both, which
    # is what the constructor sets; _effective_osc_style reads the override
    # on every _init_mpv, so a missing one aborts mpv creation outright.
    pm._lua_works = None
    pm._lua_probe = None
    pm._osc_style_override = None

    pm.repeat_mode = "none"
    pm._osc_script_loaded = False
    pm.mpvtk_active = False
    pm._hud_skip = None
    pm._trickplay_pending = False

    # Load/start bookkeeping. update() and the stop/advance paths read these,
    # so they have to exist even for tests that never load anything.
    pm._loading = False
    # A real one: it is lazy (no thread until something is submitted) and the
    # stop/advance paths genuinely queue reports through it, so a stub would
    # hide ordering bugs rather than expose them.
    pm._reporter = SessionReporter()
    pm._load_failed = Event()
    pm._load_completed = Event()
    pm._load_cancelled = False
    pm._load_error_detail = None
    pm._load_generation = 0
    pm._start_in_progress = False
    pm._failed_playback = None
    pm._session_ready = Event()
    pm._last_ui_seek_time = 0.0
    pm._browse_bg_deferred = False

    # Optional UI hooks. This harness builds a PlayerManager without running
    # __init__, so anything the real constructor defines has to be set here or
    # the code that reads it raises instead of taking its "no handler" path.
    pm.on_window_closed = None
    pm.on_mpv_gone = None
    pm.on_mpv_recreated = None
    pm.on_nav_back = None
    pm.on_nav_command = None
    pm.on_hud_menu = None
    pm.on_playstate = None
    pm.on_syncplay_change = None
    pm.notify_update = None
    pm.notify_syncplay = None
    pm.syncplay_menu_reachable = None
    pm.on_load_start = None
    pm.on_load_error = None
    pm.on_mpv_terminated = None
    pm.on_decorations_changed = None
    pm._showing_browse_bg = False
    # Window bookkeeping the force-window paths read before they write:
    # _rearm_window_geometry compares against _geometry_armed, and
    # clear_stats()/toggle_stats() read _stats_shown. Both are plain
    # constructor state, so the fake mirrors the constructor's initial value.
    pm._geometry_armed = None
    pm._stats_shown = False

    pm.menu = _FakeMenu()
    pm.syncplay = _FakeSyncplay()
    pm.update_check = _FakeUpdateCheck()
    from jellyfin_mpv_shim.osc_bridge import OscBridge
    pm.osc_bridge = OscBridge(pm)
    return pm


class _FakeMenu:
    is_menu_shown = False

    def __init__(self):
        self.actions = []
        # The drawing surface OSDMenu carries. The player reads these back
        # (mouse_select resolves a click against menu_list, and the shader
        # profile menu is reached through profile_manager), so a stand-in
        # without them turns those paths into AttributeError rather than a
        # test. tools/audit_fake_contracts.py keeps the set honest.
        self.menu_list = []
        self.menu_selection = 0
        self.profile_manager = None

    def menu_action(self, action):
        self.actions.append(action)

    def put_menu(self, title, entries=None, selected=0):
        self.menu_list = entries if entries is not None else []
        self.menu_selection = selected

    def mouse_select(self, *_a, **_kw):
        pass

    def show_menu(self):
        self.is_menu_shown = True

    def hide_menu(self):
        self.is_menu_shown = False

    def update_player(self, player):
        # Mirrors OSDMenu.update_player: the menu survives mpv re-creation and
        # is pointed at the new player handle.
        self.player = player


class _FakeSyncplay:
    """Membership and following are separate, as they are in the real one.

    A halted member (in a group, not playing its content) is the state that
    reaches mpv re-creation — stop() halts rather than leaves, and idle_quit
    is gated on is_enabled, which a halted session passes. Collapsing both
    onto one flag could not express it, so nothing could test what a re-create
    does to a group.

    Like _FakeMenu, this survives mpv re-creation: PlayerManager builds the
    real manager once and keeps it, because group membership is not a
    property of the mpv handle.
    """

    def __init__(self):
        self._enabled = False
        self._following = True
        self.client = None
        self.current_group = None
        #: Everything the player asked this object to do, in order. The
        #: player↔SyncPlay wiring is a sequence, and a stand-in that only
        #: answers questions cannot show a command being sent twice, in the
        #: wrong order, or to a session that has been left.
        self.calls = []

    def is_enabled(self):
        return self._enabled and self._following

    def in_group(self):
        return self._enabled

    def is_halted(self):
        return self._enabled and not self._following

    def halt_group_playback(self, *_a, **_kw):
        self._following = False
        self.calls.append(("halt_group_playback", ()))

    def resume_group_playback(self, *_a, **_kw):
        self._following = True
        self.calls.append(("resume_group_playback", ()))

    def join_group(self, *a, **_kw):
        self._enabled = True
        self._following = True
        self.calls.append(("join_group", a))

    def disable_sync_play(self, *_a):
        self._enabled = False
        self._following = True
        self.calls.append(("disable_sync_play", ()))

    def sync_playback_time(self):
        pass

    # The rest of the surface the player reaches for. Recorded rather than
    # ignored: a no-op that swallows the call still leaves "did we forward
    # this?" unanswerable, and these are the paths where the wiring bugs are
    # (a pause broadcast to a group we had left, a buffer never reported).
    # tools/audit_fake_contracts.py is what keeps this list complete.
    def _record(name):
        def call(self, *a, **_kw):
            self.calls.append((name, a))
        call.__name__ = name
        return call

    on_buffer = _record("on_buffer")
    on_buffer_done = _record("on_buffer_done")
    pause_request = _record("pause_request")
    play_request = _record("play_request")
    play_done = _record("play_done")
    seek_request = _record("seek_request")
    request_next = _record("request_next")
    request_prev = _record("request_prev")
    request_skip = _record("request_skip")
    process_command = _record("process_command")
    process_group_update = _record("process_group_update")
    del _record


class _FakeUpdateCheck:
    def check(self):
        pass


# --------------------------------------------------------------------------
# Concurrency-forcing helpers
# --------------------------------------------------------------------------

def run_concurrently(target, count, *, args_for=None, join_timeout=10):
    """Start ``count`` threads all running ``target`` and join them.

    ``args_for(i)`` (optional) supplies per-thread positional args. Exceptions
    raised in any worker are captured and re-raised in the caller so a race that
    corrupts state surfaces as a test failure, not a silent thread death.

    Returns the list of per-thread return values in thread-index order.
    """
    results = [None] * count
    errors = [None] * count

    def wrap(i):
        try:
            a = args_for(i) if args_for is not None else ()
            results[i] = target(*a)
        except Exception as exc:  # noqa: BLE001 - surfaced below
            errors[i] = exc

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(count)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(join_timeout)
    alive = [t for t in threads if t.is_alive()]
    if alive:
        raise AssertionError("%d worker thread(s) did not finish (deadlock?)" %
                             len(alive))
    for exc in errors:
        if exc is not None:
            raise exc
    return results


def spin_barrier(n):
    """A Barrier that all N racing threads wait on to line up their critical
    section, plus the main thread — so the interleaving is forced, not hoped
    for via sleeps."""
    return threading.Barrier(n)


# --------------------------------------------------------------------------
# ffmpeg sample media (Tier 2)
# --------------------------------------------------------------------------

_WINDOWS_FONTS = (r"C:\Windows\Fonts", ("arial.ttf", "segoeui.ttf"))


def _drawtext(label):
    """``(filter, cwd)`` drawing ``label``, or ``(None, None)`` if this machine
    cannot draw one.

    drawtext resolves its default font through fontconfig, which Windows does
    not have: ffmpeg exits ENOENT there rather than falling back to a font,
    which took out every test whose clip carried a label while the identical
    unlabelled clips built fine. The label is a debugging affordance -- it is
    what tells two clips apart when you watch the window go by -- so name a
    font explicitly where one is findable, and go without where it is not.

    The font is named *relatively*, from ffmpeg's working directory, because a
    Windows path cannot be spelled inside a filter graph without guessing how
    many times its drive colon will be unescaped on the way in. It is a
    separator there, and one backslash is consumed by the graph parser before
    the option parser ever sees it -- so ``fontfile=C\\:/...`` arrives as the
    option ``fontfile=C`` followed by the junk option ``/Windows/...``. A bare
    filename has no colon to argue about."""
    spec = "drawtext=text='%s':fontcolor=white:x=10:y=10" % label
    if os.name != "nt":
        return spec, None
    fontdir, names = _WINDOWS_FONTS
    for name in names:
        if os.path.isfile(os.path.join(fontdir, name)):
            return spec + ":fontfile=" + name, fontdir
    return None, None


def make_test_clip(path, duration=2, size="160x120", label=None):
    """Generate a tiny, deterministic H.264 clip with ffmpeg. Cheap enough to
    regenerate per test; no network, no external assets."""
    src = "testsrc=duration=%d:size=%s:rate=10" % (duration, size)
    cwd = None
    if label:
        drawtext, cwd = _drawtext(label)
        if drawtext:
            src += "," + drawtext
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", src,
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
        # Absolute, so the working directory above cannot move the output.
        os.path.abspath(path),
    ]
    # Not check=True: CalledProcessError prints the command and swallows the
    # captured stderr, so a filter ffmpeg refused and a filter it could not
    # find a font for are the same opaque exit status.
    proc = subprocess.run(cmd, cwd=cwd, stdout=subprocess.DEVNULL,
                          stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        raise RuntimeError("ffmpeg exited %d building %s: %s" % (
            proc.returncode, path, (proc.stderr or "").strip()))
    return path


def start_live_stream(path, size="160x120"):
    """Start an endless, duration-less stream on a FIFO at ``path``.

    Models what a client actually gets for live TV, and for a .strm whose
    origin is an open-ended feed: mpv can play it, but the duration property
    never arrives. That distinction matters because the playback-start gate
    used to wait on ``duration``, which for these sources meant waiting out
    the whole timeout and killing a stream that was playing fine.

    Raw Annex-B H.264 rather than MPEG-TS on purpose: a TS over a pipe still
    lets ffmpeg estimate a (steadily growing) duration from the timestamps it
    has seen, which would make the test assert nothing. An elementary stream
    carries no container timestamps, so the duration stays genuinely absent.

    No ``-re``: the writer is throttled by the FIFO buffer filling anyway, and
    pacing it in realtime instead made mpv wait seconds for enough data to
    probe. Unthrottled, file-loaded fires immediately and the stream is still
    endless.

    Returns the ffmpeg Popen; the caller must terminate it (the writer blocks
    on the FIFO until a reader opens it, so it exits on its own only once mpv
    has gone away).
    """
    if not hasattr(os, "mkfifo"):
        # Windows. A named pipe there is a \\.\pipe\ object created through
        # the Win32 API, not a filesystem node os.mkfifo can make, so this
        # leg needs a different writer rather than a different path. Skipped
        # rather than quietly swapped for a regular file: a file has a
        # duration, which is the one thing this test is about not having.
        raise unittest.SkipTest("no os.mkfifo on this platform")
    os.mkfifo(path)
    proc = subprocess.Popen(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-f", "lavfi", "-i", "testsrc=size=%s:rate=10" % size,
         "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
         "-bsf:v", "h264_mp4toannexb", "-f", "h264", path],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return proc


class TmpDirTest(unittest.TestCase):
    """Base test that provides a self-cleaning temp dir (matches the fast
    suite's TmpTest pattern in tests/test_sync_manager.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jms-itest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
