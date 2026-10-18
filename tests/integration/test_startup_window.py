"""The in-window UI has to ask mpv for its window on the command line.

mpv before 0.41 accepts a runtime ``force-window`` change and stores it, but
never acts on it while idle: the VO is created only if the option was set at
startup, and once created it can no longer be released. Reduced to a repro
with no shim involved (``--no-config --idle=yes``, set force-window over IPC,
read ``vo-configured``): false on 0.40.0, true on 0.41.0. Since the browser IS
the window's whole content, on 0.40 the app came up invisible and the tray's
Show Library Browser had nothing to show.

So ``_init_mpv`` passes ``force_window`` up front, which is also what the
mpvtk demo does. These pin *when* it is asked for -- always asking would
break the two states that deliberately have no window (start_minimized, and
being a cast target with the library closed).
"""

import linecache
import sys
import threading
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
# ...and the repo root. Run as a script -- which the __main__ block at the
# bottom invites -- `sys.path[0]` is this directory and the root is on the
# path nowhere, so `jellyfin_mpv_shim` resolves to whatever is pip-installed:
# silently, and it *runs*, against the previous release. Measured once as a
# renderer.lua from a fortnight ago failing a test about this tree.
# run_integration.py is unaffected (it spawns -m unittest with cwd=root).
sys.path.insert(0, __import__("os").path.dirname(__import__("os").path.dirname(
    __import__("os").path.dirname(__import__("os").path.abspath(__file__)))))
import _harness as h  # noqa: E402


player_module = h.import_player_with_fake_mpv()
settings = player_module.settings


class StartupForceWindowTest(unittest.TestCase):
    """Drives the real ``_init_mpv`` (via ``_ensure_mpv``) against FakeMPV and
    reads back the options it constructed the player with."""

    def _init_options(self, osc_style="mpvtk", start_minimized=False,
                      mpvtk_active=False, reopen=False):
        pm = h.build_player(player_module, test=self)
        pm.mpvtk_active = mpvtk_active
        # _init_mpv reads `reopen` off whether a player already exists; a
        # first launch has none. _ensure_mpv is the seam that re-inits.
        if not reopen:
            pm._player = None
        pm._mpv_alive = False
        with mock.patch.object(settings, "osc_style", osc_style), \
                mock.patch.object(settings, "start_minimized", start_minimized), \
                mock.patch.object(settings, "enable_gui", True), \
                mock.patch.object(settings, "thumbnail_enable", False):
            pm._ensure_mpv()
        return pm._player.init_options

    def test_first_launch_takes_the_window(self):
        self.assertTrue(self._init_options().get("force_window"),
                        "the browser would have no window to draw into")

    def test_start_minimized_does_not(self):
        """The windowless state is the whole point of the setting: running,
        castable, reachable from the tray."""
        self.assertNotIn("force_window",
                         self._init_options(start_minimized=True))

    def test_a_minimized_start_gets_a_window_when_it_is_activated(self):
        """The other half, and the one a user notices (#718).

        Launched minimized there is no window at all -- so the app somebody
        launches a SECOND time to "open it" has nothing to raise, and
        `raise_window` cannot help: it un-minimizes a window, and there is
        none. What puts one back is the browse path -- `activate()` ->
        `enter_browse()` -> `on_browse_enter` -> `set_browse_window(True)` --
        which builds an mpv when none is alive.

        The test above pins that we take no window at startup. Without this
        one, "took no window" and "can never take a window" are the same
        passing result.
        """
        pm = h.build_player(player_module, test=self)
        pm._player = None
        pm._mpv_alive = False
        with mock.patch.object(settings, "enable_gui", True), \
                mock.patch.object(settings, "osc_style", "mpvtk"):
            pm.set_browse_window(True)
        self.assertIsNotNone(pm._player, "no mpv was built to show")
        self.assertTrue(pm._player.init_options.get("force_window"),
                        "the browser was given no window to draw into")

    def test_the_window_it_gets_is_the_library_browser_state(self):
        """`set_browse_window`'s own table: the library browser is
        playback_abort=yes AND force_window=yes. Asserting the pair rather
        than force_window alone, because force_window with playback_abort
        off is the *playing* row -- a window, but one waiting for video."""
        pm = h.build_player(player_module, test=self)
        pm._player = None
        pm._mpv_alive = False
        with mock.patch.object(settings, "enable_gui", True), \
                mock.patch.object(settings, "osc_style", "mpvtk"):
            pm.set_browse_window(True)
        self.assertIs(pm._player.force_window, True)
        self.assertIs(pm._player.playback_abort, True)

    def test_minimizing_with_no_window_does_not_build_one(self):
        """The guard the test above must not have broken: dropping to the
        windowless state when there is already no window is a no-op, not a
        reason to start mpv."""
        pm = h.build_player(player_module, test=self)
        pm._player = None
        pm._mpv_alive = False
        with mock.patch.object(settings, "enable_gui", True):
            pm.set_browse_window(False)
        self.assertIsNone(pm._player, "minimizing started a player")

    def test_it_survives_being_minimized_and_reopened_repeatedly(self):
        """Three rounds, because one cannot see state feeding back.

        Every activation after the first runs against whatever the previous
        minimize left behind -- and here that is nothing at all: releasing
        the window with nothing playing lets mpv go, so round two is
        `_init_mpv` again with a whole session's worth of state carried
        across.

        **Asserted on the live properties, not on `init_options`.** The
        window arrives two different ways: built into the FIRST mpv's
        construction, and written onto a REBUILT one afterwards (a reopen
        only passes `force_window` up front when the browser is already on
        screen -- see the reopen test above). A test that reads the
        construction options sees the second round as a failure and the
        product is fine; what the user has either way is a window.
        """
        pm = h.build_player(player_module, test=self)
        pm._player = None
        pm._mpv_alive = False
        with mock.patch.object(settings, "enable_gui", True), \
                mock.patch.object(settings, "osc_style", "mpvtk"):
            for round_no in range(1, 4):
                with self.subTest(round=round_no):
                    pm.set_browse_window(True)
                    self.assertTrue(pm._mpv_alive,
                                    "round %d has no mpv" % round_no)
                    self.assertIs(pm._player.force_window, True,
                                  "round %d came back with no window"
                                  % round_no)
                    self.assertIs(pm._player.playback_abort, True)
                    pm.set_browse_window(False)
                    # ...and the minimize really released it, or "the window
                    # came back" above would be "the window never left".
                    self.assertIs(pm._player.force_window, False,
                                  "round %d did not release the window"
                                  % round_no)

    def test_a_reopen_takes_the_window_only_if_the_browser_is_on_screen(self):
        # Re-opened from the tray with the library up.
        self.assertTrue(
            self._init_options(reopen=True, mpvtk_active=True)
            .get("force_window"))

    def test_a_reopen_for_playback_leaves_it_alone(self):
        """The play path re-opens mpv while minimized (idle-quit, a cast
        arriving). Loading a file brings the VO up by itself, and forcing a
        window here would flash an empty one first."""
        self.assertNotIn("force_window",
                         self._init_options(reopen=True, mpvtk_active=False))

    def test_other_osc_styles_are_untouched(self):
        """Only the in-window UI needs a window with nothing playing; the lua
        OSC and 'default' draw over real video."""
        for style in ("mpv", "default"):
            with self.subTest(style=style):
                self.assertNotIn("force_window", self._init_options(style))

    def test_the_legacy_alias_still_counts_as_the_in_window_ui(self):
        """osc_style 'jellyfin' is the retired name for the mpvtk HUD; a
        config carrying it must not lose its window."""
        self.assertTrue(self._init_options("jellyfin").get("force_window"))


def wait_parked(thread, timeout=5.0):
    """Wait until `thread` is blocked acquiring `_lock` -- an RLock acquire
    has no Python frame, so that is: its innermost frame is stopped on a
    `with ..._lock` line. False if it finished or never got there.

    Observed rather than slept for: a sleep long enough "to reach the lock"
    passes just as well when the thread arrives after the build is over."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not thread.is_alive():
            return False
        frame = sys._current_frames().get(thread.ident)
        if frame is not None:
            line = linecache.getline(frame.f_code.co_filename,
                                     frame.f_lineno)
            if line.strip().startswith("with") and "_lock" in line:
                return True
        time.sleep(0.002)
    return False


class ConcurrentReopenTest(unittest.TestCase):
    """A caller that finds mpv dead while another thread is re-creating it
    waits for that build and then acts on the mpv it built (#828;
    docs/mpv-backends.md section 8).

    The real trigger: the browser re-opens mpv from the tray, and the NEW
    handle's initial `volume` observation arrives on its event thread while
    `_init_mpv` is still running -- `push_playstate` -> `on_playstate` ->
    `enter_browse` -> `set_browse_window(True)`. Unguarded, that built a
    second mpv owned by nobody.

    The second caller is started from inside `_construct_mpv` on the
    builder's thread, which is where the real one lands, and the build goes
    on only once it is parked on `_lock`."""

    ROUNDS = 3

    def _player(self):
        pm = h.build_player(player_module, test=self)
        pm.menu.is_menu_shown = False
        pm.mpvtk_active = False
        self.recreated = []
        pm.on_mpv_recreated = lambda: self.recreated.append(pm._player)
        self.built = []
        self.hook_errors = []
        inner = pm._construct_mpv

        def construct(*a, **kw):
            hook, self.during = self.during, None
            if hook is not None:
                try:
                    hook(pm)
                except BaseException as e:   # on the builder's thread
                    self.hook_errors.append(e)
            player = inner(*a, **kw)
            self.built.append(player)
            return player

        pm._construct_mpv = construct
        # What the gateway and the OSD menu reach for -- by import path:
        # in the whole-suite leg sys.modules can hold a different player
        # module from `player_module`, and theirs is the one imported.
        patch = mock.patch("jellyfin_mpv_shim.player.playerManager", pm)
        patch.start()
        self.addCleanup(patch.stop)
        return pm

    def _rounds(self, during, build=lambda pm: pm.set_browse_window(True)):
        """Idle-quit, then re-open with `build` (the tray's, by default) and
        `during` running in the middle of it. Returns the player."""
        self.during = None
        builder = None
        pm = self._player()
        # mpv_ext_start: the harness turns it off so the fake spawns nothing,
        # and idle_quit leaves alone an external mpv the shim did not start.
        with mock.patch.object(settings, "enable_gui", True), \
                mock.patch.object(settings, "osc_style", "mpvtk"), \
                mock.patch.object(settings, "thumbnail_enable", False), \
                mock.patch.object(settings, "mpv_ext_start", True):
            for round_no in range(1, self.ROUNDS + 1):
                if builder is not None and builder.is_alive():
                    # Deadlocked, and holding _lock: the next idle_quit
                    # would hang the run rather than fail it.
                    break
                with self.subTest(round=round_no):
                    pm.mpvtk_active = False
                    pm.idle_quit("test")
                    self.assertFalse(pm._mpv_alive)
                    self.outgoing = pm._player
                    self.outgoing_seen = len(self.outgoing.commands)
                    del self.built[:], self.recreated[:], self.hook_errors[:]
                    self.during = during
                    pm.mpvtk_active = True
                    # On a thread, so a deadlock fails the test, not the run.
                    builder = threading.Thread(target=build, args=(pm,),
                                               daemon=True)
                    builder.start()
                    builder.join(10)
                    self.assertFalse(builder.is_alive(),
                                     "round %d: the re-open deadlocked"
                                     % round_no)
                    self.during = None
                    self._join_others()
                    if self.hook_errors:
                        raise self.hook_errors[0]
                    self.assertEqual(
                        len(self.built), 1,
                        "round %d built %d mpvs for one re-open"
                        % (round_no, len(self.built)))
                    self.assertIs(pm._player, self.built[0])
                    self.assertTrue(pm._mpv_alive)
                    self.assertEqual(self.recreated, [pm._player],
                                     "the UI must follow exactly one handle")
                    self.assertEqual(
                        self.outgoing.commands[self.outgoing_seen:], [],
                        "round %d: a command reached the outgoing mpv"
                        % round_no)
        return pm

    def _join_others(self):
        threads, self._threads = getattr(self, "_threads", []), []
        for t in threads:
            t.join(10)
            self.assertFalse(t.is_alive(), "the second caller never returned")
            if "error" in t.box:
                raise t.box["error"]

    def _spawn(self, target):
        """`target` on a thread, parked on `_lock` before this returns."""
        box = {}

        def run():
            try:
                target()
            except BaseException as e:
                box["error"] = e

        t = threading.Thread(target=run, daemon=True)
        t.box = box
        self._threads = getattr(self, "_threads", []) + [t]
        t.start()
        self.assertTrue(wait_parked(t), "the second caller did not wait "
                        "for the build")
        return t

    def test_the_browser_asking_mid_build_waits_then_applies(self):
        """Through the gateway, mid-way through the OSD menu's re-open: a
        build that does not apply the browse window itself, so only the
        caller that waited can have put it there."""
        from jellyfin_mpv_shim.mpvtk_browser.gateway.playback import \
            PlaybackMixin

        def during(pm):
            self._spawn(lambda: PlaybackMixin.on_browse_enter(None))

        pm = self._rounds(during, build=lambda pm: pm.force_window(True))
        self.assertEqual(pm._player.background, "color",
                         "the browse request was lost")
        self.assertFalse(pm._player.keepaspect)

    def test_the_osd_menu_asking_mid_play_build_claims_keys_on_the_new_mpv(
            self):
        """The real `show_menu`, which claims its keys after `force_window`
        on the strength of `force_window` having re-created a dead mpv.
        Handed back before the build was done, it claimed them on the
        outgoing handle -- a command after terminate, which libmpv answers
        with a segfault."""
        from jellyfin_mpv_shim.menu import OSDMenu

        def during(pm):
            # A real terminated handle raises on the read, which
            # playback_is_aborted answers with True; FakeMPV keeps the last
            # round's logo playing.
            self.outgoing.playback_abort = True
            menu = OSDMenu.__new__(OSDMenu)
            menu.playerManager = pm
            menu.profile_menu = None
            menu.svp_menu = None
            menu.refresh_menu = lambda *a, **k: None
            self._spawn(menu.show_menu)

        def play_path(pm):
            with pm._lock:      # as _play_media holds it
                pm._ensure_mpv()

        pm = self._rounds(during, build=play_path)
        self.assertIn(pm.MENU_SECTION, pm._player._sections,
                      "the new mpv never got the menu's keys")

    def test_a_lock_free_ensure_mid_build_gets_a_live_mpv(self):
        seen = []

        def during(pm):
            def ensure():
                pm._ensure_mpv()
                seen.append(pm._mpv_alive)
            self._spawn(ensure)

        self._rounds(during)
        self.assertEqual(seen, [True] * self.ROUNDS)

    def test_a_play_mid_build_gets_the_built_mpv(self):
        """`_play_media` holds `_lock` around `_ensure_mpv`, as here."""
        seen = []

        def during(pm):
            def play_path():
                with pm._lock:
                    pm._ensure_mpv()
                    seen.append(pm._mpv_alive)
            self._spawn(play_path)

        self._rounds(during)
        self.assertEqual(seen, [True] * self.ROUNDS)

    def test_a_call_from_inside_the_build_does_not_build_again(self):
        """`_lock` is an RLock, so the builder's own thread is the one caller
        that would get past it. Nothing does this today; if something
        starts to, it must not orphan the mpv being built."""
        nested = []

        def during(pm):
            with self.assertLogs("window", "ERROR"):
                nested.append(pm._reopen_mpv())

        self._rounds(during)
        self.assertEqual(nested, [False] * self.ROUNDS)


class _ReopenCase(unittest.TestCase):
    """An idle-quit player, with `_construct_mpv` recording every mpv built
    and running `self.during` (once) inside the first build."""

    def setUp(self):
        for p in (mock.patch.object(settings, "enable_gui", True),
                  mock.patch.object(settings, "osc_style", "mpvtk"),
                  mock.patch.object(settings, "thumbnail_enable", False),
                  # idle_quit leaves alone an external mpv it did not start.
                  mock.patch.object(settings, "mpv_ext_start", True)):
            p.start()
            self.addCleanup(p.stop)
        self.pm = pm = h.build_player(player_module, test=self)
        pm.menu.is_menu_shown = False
        pm.mpvtk_active = False
        pm.on_mpv_recreated = None
        self.exits = []
        pm.on_reopen_failed = self.exits.append
        self.built = []
        self.during = None
        self.spawned = []
        inner = pm._construct_mpv

        def construct(*a, **kw):
            hook, self.during = self.during, None
            if hook is not None:
                hook(pm)
            player = inner(*a, **kw)
            self.built.append(player)
            return player

        pm._construct_mpv = construct
        pm.idle_quit("test")
        self.assertFalse(pm._mpv_alive)
        self.outgoing = pm._player

    def spawn(self, target):
        """`target` on a daemon thread; its return or exception lands in
        `thread.box`."""
        box = {}

        def run():
            try:
                box["result"] = target()
            except BaseException as e:
                box["error"] = e

        t = threading.Thread(target=run, daemon=True)
        t.box = box
        self.spawned.append(t)
        t.start()
        return t

    def join_all(self, timeout=10):
        for t in self.spawned:
            t.join(timeout)
            self.assertFalse(t.is_alive(), "a caller never returned")

    def assert_shut_down(self):
        """A failed re-open shuts the app down: the hook fired once, and a
        request after it is refused rather than built."""
        self.assertEqual(len(self.exits), 1,
                         "exit hook calls: %r" % self.exits)
        built = len(self.built)
        with self.assertRaises(RuntimeError):
            self.pm._ensure_mpv()
        self.assertEqual(len(self.built), built,
                         "built another mpv while shutting down")

    def raise_spawned(self):
        for t in self.spawned:
            if "error" in t.box:
                raise t.box["error"]


class OptionalWritesTest(_ReopenCase):
    """An optional property write that fails must not abort the build: past
    `_construct_mpv` that leaves a running mpv the shim does not drive."""

    def _build_with_failing_write(self, prop):
        real = h.FakeMPV.__setattr__

        def write(fake, name, value):
            if name == prop and fake.__dict__.get("_armed_to_fail"):
                raise RuntimeError("simulated IPC failure on %s" % prop)
            return real(fake, name, value)

        inner = self.pm._construct_mpv

        def construct(*a, **kw):
            player = inner(*a, **kw)
            object.__setattr__(player, "_armed_to_fail", True)
            return player

        self.pm._construct_mpv = construct
        with mock.patch.object(h.FakeMPV, "__setattr__", write), \
                mock.patch.object(settings, "screenshot_dir", "/tmp/shots"), \
                self.assertLogs(player_module.log, "ERROR"):
            self.pm._ensure_mpv()
        self.assertTrue(self.pm._mpv_alive,
                        "a failed %s write aborted the build" % prop)
        self.assertEqual(len(self.built), 1)

    def test_screenshot_directory(self):
        self._build_with_failing_write("screenshot_directory")

    def test_resume_playback(self):
        self._build_with_failing_write("resume_playback")


class FailedBuildTest(_ReopenCase):
    """A build that fails past `_construct_mpv` terminates the mpv it made
    and re-raises, rather than leaving it running and owned by nobody. A
    failed re-open shuts the app down: all UI goes through mpv."""

    def fail_first_bind(self):
        real = self.pm._bind_mpv_handlers
        calls = []

        def bind():
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("simulated failure binding handlers")
            return real()

        self.pm._bind_mpv_handlers = bind

    def test_the_half_built_mpv_is_terminated(self):
        pm = self.pm
        self.fail_first_bind()
        with self.assertRaises(RuntimeError), \
                self.assertLogs("window", "ERROR"):
            pm._ensure_mpv()
        failed = self.built[-1]
        pm._terminate_thread.join(10)
        self.assertTrue(failed.terminated,
                        "the half-built mpv was left running")
        self.assertFalse(pm._mpv_alive)
        # ...and nothing is wedged: the lock is free to another thread, and
        # the next re-open builds exactly one.
        got = []
        t = threading.Thread(target=lambda: got.append(
            pm._lock.acquire(timeout=2) and (pm._lock.release() or True)))
        t.start()
        t.join(5)
        self.assertEqual(got, [True], "_lock left held after a failed build")
        self.assert_shut_down()


    def test_a_caller_waiting_behind_it_gets_the_failure(self):
        """...and does not build one of its own, which is what "the next
        click retries" would otherwise have meant for every queued caller."""
        pm = self.pm
        self.fail_first_bind()

        def during(pm):
            self.assertTrue(wait_parked(self.spawn(pm._ensure_mpv)))

        self.during = during
        with self.assertLogs("window", "ERROR"):
            builder = self.spawn(pm._ensure_mpv)
            self.join_all(20)
        waiter = self.spawned[1]
        self.assertIsInstance(builder.box.get("error"), RuntimeError)
        self.assertIsInstance(waiter.box.get("error"), RuntimeError,
                              "the waiter did not get the failure")
        self.assertEqual(len(self.built), 1,
                         "the waiter built another mpv itself")
        self.assert_shut_down()

    def test_a_construct_failure_reaches_the_waiter_too(self):
        """Any failure out of the build, not only one past `_construct_mpv`:
        a waiter that missed it constructed an mpv of its own."""
        pm = self.pm
        constructs = []
        inner = pm._construct_mpv

        def construct(*a, **kw):
            constructs.append(1)
            if len(constructs) == 1:
                self.assertTrue(wait_parked(self.spawn(pm._ensure_mpv)))
                raise RuntimeError("simulated: mpv would not construct")
            return inner(*a, **kw)

        pm._construct_mpv = construct
        builder = self.spawn(pm._ensure_mpv)
        self.join_all(20)
        waiter = self.spawned[1]
        self.assertIsInstance(builder.box.get("error"), RuntimeError)
        self.assertIsInstance(waiter.box.get("error"), RuntimeError,
                              "the waiter did not get the failure: %r"
                              % waiter.box)
        self.assertEqual(len(constructs), 1,
                         "the waiter constructed another mpv itself")
        self.assertEqual(len(self.exits), 1,
                         "exit hook calls: %r" % self.exits)
        with self.assertRaises(RuntimeError):
            pm._ensure_mpv()
        self.assertEqual(len(constructs), 1,
                         "constructed another mpv while shutting down")

    def test_unwired_it_logs_and_exits_nothing(self):
        """No hook (tests, embedding): logged, and nothing else happens."""
        pm = self.pm
        pm.on_reopen_failed = None
        self.fail_first_bind()
        with self.assertRaises(RuntimeError), \
                self.assertLogs(player_module.log, "ERROR") as cm:
            pm._ensure_mpv()
        self.assertTrue(any("could not be re-created" in r
                            for r in cm.output), cm.output)


class ShutdownMidBuildTest(_ReopenCase):
    """mpv shutting down after its handlers are bound and before the build
    publishes it -- a crash, a kill, a `quit` -- is a failed build, and the
    terminate is the builder's (docs/mpv-backends.md section 8)."""

    def shut_down_during_bind(self, on_thread):
        """Fire the new handle's shutdown from inside `_bind_mpv_handlers`,
        with a waiter parked on `_lock`. Returns the state the hook saw."""
        pm = self.pm
        real_bind = pm._bind_mpv_handlers
        real_conf = pm._configure_new_mpv
        state = {"configuring": False, "terms": []}

        def conf(*a, **kw):
            state["configuring"] = True
            try:
                return real_conf(*a, **kw)
            finally:
                state["configuring"] = False

        def bind():
            real_bind()
            if "handle" in state:
                return
            state["handle"] = pm._player
            self.assertTrue(wait_parked(self.spawn(pm._ensure_mpv)))
            # As mpv delivers it: whatever handler the build bound.
            fire = lambda: [cb(None) for cb in  # noqa: E731
                            pm._player._event_callbacks["shutdown"]]
            if on_thread:
                t = threading.Thread(target=fire, daemon=True)
                t.start()
                t.join(5)
            else:
                fire()
            # Room for a terminate started by the handler to run while the
            # build is still configuring the handle.
            if pm._terminate_thread is not None:
                pm._terminate_thread.join(2)

        pm._bind_mpv_handlers = bind
        pm._configure_new_mpv = conf
        real_term = h.FakeMPV.terminate

        def count_term(fake):
            state["terms"].append((fake, state["configuring"]))
            return real_term(fake)

        with mock.patch.object(h.FakeMPV, "terminate", count_term):
            state["builder"] = self.spawn(pm._ensure_mpv)
            self.join_all(20)
            if pm._terminate_thread is not None:
                pm._terminate_thread.join(10)
        return state

    def test_the_build_and_its_waiter_get_the_failure(self):
        for on_thread in (False, True):
            with self.subTest(on_thread=on_thread):
                if on_thread:
                    self.setUp()
                pm = self.pm
                state = self.shut_down_during_bind(on_thread)
                waiter = self.spawned[1]
                self.assertIsInstance(
                    state["builder"].box.get("error"), Exception,
                    "the build published a handle that shut down mid-build:"
                    " alive=%r" % pm._mpv_alive)
                self.assertIsInstance(waiter.box.get("error"), Exception,
                                      "the waiter did not get the failure")
                self.assertFalse(pm._mpv_alive)
                self.assertEqual(len(self.built), 1)
                self.assertEqual(len(self.exits), 1,
                                 "exit hook calls: %r" % self.exits)
                mine = [t for t, _ in state["terms"]
                        if t is state["handle"]]
                self.assertEqual(len(mine), 1,
                                 "the handle was terminated %d times"
                                 % len(mine))

    def test_no_terminate_while_the_build_is_configuring(self):
        """No command after terminate: libmpv answers one with a segfault."""
        state = self.shut_down_during_bind(on_thread=False)
        mine = [c for t, c in state["terms"] if t is state["handle"]]
        self.assertEqual(mine, [False],
                         "terminates of the half-built handle, True = while "
                         "the build was still configuring it: %r" % mine)

    def test_a_window_close_mid_build_is_a_close(self):
        """Not a failed build: once bound, CLOSE_WIN is a key, queued for
        after the build, and mpv does not shut down."""
        pm = self.pm
        real_bind = pm._bind_mpv_handlers

        def bind():
            real_bind()
            pm._on_close_win()

        pm._bind_mpv_handlers = bind
        builder = self.spawn(pm._ensure_mpv)
        self.join_all(20)
        self.assertNotIn("error", builder.box, builder.box)
        self.assertTrue(pm._mpv_alive, "a window close failed the build")
        self.assertEqual(self.exits, [], "a window close exited the app")
        self.assertEqual(len(self.built), 1)
        self.assertFalse(pm.evt_queue.empty(),
                         "the close was not queued for after the build")

    def test_an_old_handles_late_shutdown_leaves_the_new_one_alone(self):
        """The outgoing mpv's shutdown arriving after the new one is built
        (its terminate outlived the join) must not fail or kill the new."""
        pm = self.pm
        pm._ensure_mpv()
        old = pm._player
        pm._handle_mpv_disconnect()     # lost; recovery is the next play
        pm._terminate_thread.join(10)
        pm._ensure_mpv()
        self.assertIsNot(pm._player, old)
        for cb in old._event_callbacks["shutdown"]:
            cb(None)
        if pm._terminate_thread is not None:
            pm._terminate_thread.join(10)
        self.assertTrue(pm._mpv_alive)
        self.assertFalse(pm._player.terminated)


class TerminateJoinTest(_ReopenCase):

    def test_lock_is_free_while_the_previous_terminate_finishes(self):
        """The tray's re-open waits out the previous mpv's terminate before
        it takes `_lock`, so nothing else that needs the lock stalls behind
        a dying mpv as well. (The play path holds `_lock` already.)"""
        pm = self.pm
        release = threading.Event()
        slow = threading.Thread(target=lambda: release.wait(10), daemon=True)
        slow.start()
        pm._terminate_thread = slow
        pm.mpvtk_active = True
        builder = self.spawn(lambda: pm.set_browse_window(True))
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            frame = sys._current_frames().get(builder.ident)
            names = []
            while frame is not None:
                names.append(frame.f_code.co_name)
                frame = frame.f_back
            if "join" in names:
                break
            time.sleep(0.002)
        else:
            self.fail("the re-open never waited for the terminate")
        got = pm._lock.acquire(timeout=1)
        if got:
            pm._lock.release()
        release.set()
        self.join_all()
        self.assertTrue(got, "_lock held across the previous terminate")
        self.assertTrue(pm._mpv_alive)
        self.assertEqual(len(self.built), 1)


class CreateLogTest(_ReopenCase):

    def test_the_create_line_names_who_asked(self):
        """Which caller it was is the finding when a window re-opens on its
        own -- and every re-open now goes through `_reopen_mpv`."""
        with self.assertLogs("window", "INFO") as cm:
            self.pm.force_window(True)
        create = [r for r in cm.output if "CREATE mpv" in r]
        self.assertEqual(len(create), 1, cm.output)
        self.assertIn(".force_window:", create[0])


class StaleRequestTest(_ReopenCase):
    """A window request that waited on `_lock` behind a cast's playback
    start, into a closed window, finds a video on screen when it gets the
    lock. The video wins (docs/mpv-backends.md section 8): stretched film,
    hidden controls and a logo over it were what applying it did."""

    FILM = types.SimpleNamespace(
        item={"Type": "Movie", "MediaType": "Video", "Id": "film"})

    def behind_a_start(self, request, succeeds=True):
        """Run `request` on a thread while this one plays a start under
        `_lock`; the request is parked on the lock before the start runs."""
        pm = self.pm
        with pm._lock:
            waiter = self.spawn(request)
            self.assertTrue(wait_parked(waiter),
                            "the request did not wait for the lock")
            pm._ensure_mpv()
            if succeeds:
                pm._video = self.FILM
                pm._player.play("film.mkv")
                pm.browse_yield()
        self.join_all()
        self.raise_spawned()
        self.assertEqual(len(self.built), 1)
        return waiter.box.get("result")

    def test_the_browse_window_leaves_the_film_alone(self):
        pm = self.pm
        result = self.behind_a_start(lambda: pm.set_browse_window(True))
        self.assertIs(result, False)
        self.assertTrue(pm._player.keepaspect,
                        "the film plays stretched: keepaspect turned off")
        self.assertNotEqual(getattr(pm._player, "background", None), "color")

    def test_the_browse_window_applies_when_the_start_failed(self):
        pm = self.pm
        self.behind_a_start(lambda: pm.set_browse_window(True),
                            succeeds=False)
        self.assertFalse(pm._player.keepaspect)
        self.assertEqual(pm._player.background, "color")

    def test_the_gateway_leaves_the_films_controls_alone(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway.playback import \
            PlaybackMixin

        pm = self.pm
        with mock.patch("jellyfin_mpv_shim.player.playerManager", pm):
            result = self.behind_a_start(
                lambda: PlaybackMixin.on_browse_enter(None))
        # The browser's enter_browse reads the decline as this value.
        self.assertIs(result, False)
        self.assertTrue(pm._player.keepaspect)
        self.assertFalse(pm._osc_suppressed,
                         "the film's controls hidden by a stale browse enter")

    def test_the_menu_window_does_not_replace_the_film(self):
        pm = self.pm
        self.behind_a_start(lambda: pm.force_window(True))
        self.assertEqual(pm._player.played, ["film.mkv"],
                         "the menu's logo replaced the film")


class VersionGateTest(unittest.TestCase):
    """Which mpv acts on a force-window change made while idle."""

    works = staticmethod(player_module.runtime_force_window_works)

    def test_041_and_newer_honour_it(self):
        for v in ("mpv v0.41.0", "mpv v0.42.0", "mpv v1.0.0",
                  "mpv v0.41.0-368-g1234567"):
            with self.subTest(v=v):
                self.assertTrue(self.works(v))

    def test_040_and_older_do_not(self):
        for v in ("mpv v0.40.0", "mpv 0.40.0-dirty", "mpv v0.35.1"):
            with self.subTest(v=v):
                self.assertFalse(self.works(v))

    def test_an_unreadable_version_is_treated_as_old(self):
        """The two ways of being wrong are not symmetric: assuming old
        costs a fallback that works everywhere, assuming new costs a
        window that will not go away."""
        for v in ("mpv UNKNOWN", "", None):
            with self.subTest(v=v):
                self.assertFalse(self.works(v))


class MinimizeReleaseTest(unittest.TestCase):
    """Minimize is "release the window". On an mpv that cannot drop
    force-window at runtime the request is silently ignored, so the release
    has to be a teardown instead — which is only what the idle timer would
    do a few minutes later anyway."""

    def _player(self, runtime_force_window):
        pm = h.build_player(player_module, test=self)
        pm._runtime_force_window = runtime_force_window
        pm._mpv_alive = True
        pm.mpvtk_active = False
        pm._video = None
        pm._loading = False
        pm._showing_browse_bg = False
        quits = []
        pm.idle_quit = lambda reason=None: quits.append(reason)
        return pm, quits

    def test_a_modern_mpv_just_drops_force_window(self):
        pm, quits = self._player(True)
        pm.set_browse_window(False)
        self.assertFalse(pm._player.force_window)
        self.assertEqual(quits, [], "tore mpv down when it did not have to")

    def test_an_old_mpv_is_torn_down_instead(self):
        pm, quits = self._player(False)
        pm.set_browse_window(False)
        self.assertEqual(len(quits), 1,
                         "the window would have stayed on screen")

    def test_nothing_is_torn_down_while_something_is_playing(self):
        """set_browse_window(False) leaves the window alone during
        playback; quitting mpv there would stop the video."""
        pm, quits = self._player(False)
        pm._video = object()
        pm.set_browse_window(False)
        self.assertEqual(quits, [])

    def test_nothing_is_torn_down_mid_load(self):
        pm, quits = self._player(False)
        pm._loading = True
        pm.set_browse_window(False)
        self.assertEqual(quits, [])

    def test_entering_browse_never_tears_down(self):
        pm, quits = self._player(False)
        pm.set_browse_window(True)
        self.assertEqual(quits, [])


if __name__ == "__main__":
    unittest.main()
