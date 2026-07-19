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
HAVE_DISPLAY = bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))
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


class FakeMPV:
    """A stand-in for an mpv backend object.

    It supports the two surfaces ``PlayerManager`` uses:

    * *registration* — the ``on_key_press`` / ``property_observer`` /
      ``event_callback`` decorators used in ``_init_mpv``. Registered callbacks
      are stored so a test can fire them later (``fire_property`` / ``fire_event``),
      optionally from another thread, to reproduce observer-ordering races.
    * *control / property* access — ``command``, ``play``, ``show_text`` etc. are
      recorded; the many scalar properties (``pause``, ``playback_abort``,
      ``playback_time`` …) are plain attributes tests can set to script state.

    Property reads/writes can be made to raise a "disconnect" error to exercise
    the ``_mpv_errors`` handling paths (``fail_with``).
    """

    # Class attr so ``hasattr(mpv, "ShutdownError")`` is true on the module.
    ShutdownError = ShutdownError

    def __init__(self, **_options):
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
        cb = self._key_bindings.get(key)
        if cb is not None:
            cb()

    # -- control surface ----------------------------------------------------

    def command(self, *args):
        if self.fail_with is not None:
            raise self.fail_with
        self.commands.append(args)

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
    settings.enable_osc = False
    settings.osc_style = "default"  # keep the OSC lua out of these legs
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
        sys.modules["python_mpv_jsonipc"] = make_fake_mpv_module("jsonipc")
    else:
        settings.mpv_ext = False
        sys.modules["mpv"] = make_fake_mpv_module("libmpv")

    import jellyfin_mpv_shim.player as player_module
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
    from threading import RLock, Lock
    from jellyfin_mpv_shim.utils import Timer

    PlayerManager = player_module.PlayerManager
    pm = PlayerManager.__new__(PlayerManager)

    pm._player = FakeMPV()
    pm._video = video
    pm.evt_queue = Queue()
    pm._lock = RLock()
    pm._tl_lock = RLock()
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
    pm.get_webview = lambda: None
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
    pm._last_intro_msg_time = 0.0

    pm.repeat_mode = "none"
    pm._osc_script_loaded = False
    pm.mpvtk_active = False
    pm.trickplay_meta = None
    pm._hud_skip = None
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
    pm.notify_update = None
    pm._showing_browse_bg = False

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

    def menu_action(self, action):
        self.actions.append(action)

    def show_menu(self):
        self.is_menu_shown = True

    def hide_menu(self):
        self.is_menu_shown = False

    def update_player(self, player):
        # Mirrors OSDMenu.update_player: the menu survives mpv re-creation and
        # is pointed at the new player handle.
        self.player = player


class _FakeSyncplay:
    def __init__(self):
        self._enabled = False

    def is_enabled(self):
        return self._enabled

    def disable_sync_play(self, *_a):
        self._enabled = False

    def sync_playback_time(self):
        pass


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

def make_test_clip(path, duration=2, size="160x120", label=None):
    """Generate a tiny, deterministic H.264 clip with ffmpeg. Cheap enough to
    regenerate per test; no network, no external assets."""
    src = "testsrc=duration=%d:size=%s:rate=10" % (duration, size)
    if label:
        src += ",drawtext=text='%s':fontcolor=white:x=10:y=10" % label
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-f", "lavfi", "-i", src,
        "-pix_fmt", "yuv420p", "-c:v", "libx264", "-preset", "ultrafast",
        path,
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE)
    return path


class TmpDirTest(unittest.TestCase):
    """Base test that provides a self-cleaning temp dir (matches the fast
    suite's TmpTest pattern in tests/test_sync_manager.py)."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jms-itest-")
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
