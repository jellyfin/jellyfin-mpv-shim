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
                mock.patch.object(settings, "thumbnail_osc_builtin", True):
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


if __name__ == "__main__":
    unittest.main()
