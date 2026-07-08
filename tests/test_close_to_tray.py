import unittest
from unittest import mock

from jellyfin_mpv_shim.gui_mgr import UserInterface
from jellyfin_mpv_shim.conf import settings


class CloseToTrayTest(unittest.TestCase):
    """on_window_closed: default exits, close_to_tray hides to the tray (only
    when a tray is actually available)."""

    def setUp(self):
        self.ui = UserInterface()
        self.ui.r_queue = mock.Mock()
        self.ui._send_browser = mock.Mock()
        self._orig = settings.close_to_tray
        self.addCleanup(setattr, settings, "close_to_tray", self._orig)

    def test_default_closes_exits(self):
        settings.close_to_tray = False
        self.ui.tray_alive = True
        self.ui.on_window_closed(None)
        self.ui.r_queue.put.assert_called_once_with(("quit", None))
        self.ui._send_browser.assert_not_called()

    def test_close_to_tray_hides_when_tray_available(self):
        settings.close_to_tray = True
        self.ui.tray_alive = True
        self.ui.on_window_closed(None)
        self.ui._send_browser.assert_called_once_with(("hide", None))
        self.ui.r_queue.put.assert_not_called()

    def test_close_to_tray_without_tray_still_exits(self):
        settings.close_to_tray = True
        self.ui.tray_alive = False
        self.ui.on_window_closed(None)
        self.ui.r_queue.put.assert_called_once_with(("quit", None))
        self.ui._send_browser.assert_not_called()

    def test_explicit_minimize_choice_overrides_setting(self):
        # An explicit choice from the first-close prompt wins over the setting.
        settings.close_to_tray = False
        self.ui.tray_alive = True
        self.ui.on_window_closed({"minimize": True})
        self.ui._send_browser.assert_called_once_with(("hide", None))
        self.ui.r_queue.put.assert_not_called()

    def test_explicit_exit_choice_overrides_setting(self):
        settings.close_to_tray = True
        self.ui.tray_alive = True
        self.ui.on_window_closed({"minimize": False})
        self.ui.r_queue.put.assert_called_once_with(("quit", None))
        self.ui._send_browser.assert_not_called()


if __name__ == "__main__":
    unittest.main()
