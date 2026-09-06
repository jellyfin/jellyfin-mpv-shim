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

import sys
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
        pm = h.build_player(player_module)
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
        pm = h.build_player(player_module)
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
        pm = h.build_player(player_module)
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
        pm = h.build_player(player_module)
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
        pm = h.build_player(player_module)
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
        pm = h.build_player(player_module)
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
