"""Closing the window: exit, or hide to the tray.

Ported from the Tk browser's UserInterface to the in-window one. The two
"explicit choice" cases went with it — the Tk browser prompted on first
close and remembered the answer (close_prompt_shown); the in-window browser
never prompts, so that setting is gone too. The three behaviours below are
the ones that still exist.

The no-tray case is the one that matters: minimizing with nothing in the
tray leaves the app running with no window and no way to reach it, which
looks exactly like a crash.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.ui import UserInterface  # noqa: E402


class _Tray:
    def __init__(self, available=True):
        self.available = available


class CloseToTrayTest(unittest.TestCase):
    def setUp(self):
        self.ui = UserInterface()
        self.ui._quit = mock.Mock()
        self.browser = mock.Mock()
        self.ui._browser = self.browser
        self._orig = settings.close_to_tray
        self.addCleanup(setattr, settings, "close_to_tray", self._orig)

    def test_default_closes_exits(self):
        settings.close_to_tray = False
        self.ui._tray = _Tray()
        self.ui.on_window_closed()
        self.ui._quit.assert_called_once()
        self.browser.minimize.assert_not_called()

    def test_close_to_tray_hides_when_tray_available(self):
        settings.close_to_tray = True
        self.ui._tray = _Tray()
        self.ui.on_window_closed()
        self.browser.minimize.assert_called_once()
        self.ui._quit.assert_not_called()

    def test_close_to_tray_without_tray_still_exits(self):
        """Otherwise the app is running, invisible and unreachable."""
        settings.close_to_tray = True
        self.ui._tray = _Tray(available=False)
        self.ui.on_window_closed()
        self.ui._quit.assert_called_once()
        self.browser.minimize.assert_not_called()

    def test_no_tray_object_at_all_still_exits(self):
        settings.close_to_tray = True
        self.ui._tray = None
        self.ui.on_window_closed()
        self.ui._quit.assert_called_once()

    def test_hiding_re_arms_the_pin_gate(self):
        """Unlocking covers this appearance of the window, not the rest of
        the process's life — re-raising from the tray must re-prompt."""
        settings.close_to_tray = True
        self.ui._tray = _Tray()
        self.ui.on_window_closed()
        self.browser.maybe_relock.assert_called_once()


if __name__ == "__main__":
    unittest.main()
