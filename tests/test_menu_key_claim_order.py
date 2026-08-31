"""The OSD menu must claim its arrows on the mpv that will show it.

`show_menu` claimed the menu keys first and only then called
`force_window(True)` -- which is what notices `not self._mpv_alive` and calls
`_init_mpv()`. So after an idle-quit the `define-section`/`enable-section`
went to the OUTGOING handle and the replacement got no `jms_menu` section: the
menu appeared and its arrows did nothing, until it was closed and reopened.

Reachable through a Jellyfin remote's `GoToSettings` in CLI/classic-OSC mode
after mpv has idle-quit (`event_handler` -> `menu_action("settings")` ->
`toggle_settings_menu` -> `show_menu`). The current tray does not expose this
legacy menu action, which is why it is narrow rather than unreachable.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.menu import OSDMenu  # noqa: E402


class MenuKeysAreClaimedOnTheLiveHandleTest(unittest.TestCase):

    def _menu(self, order, aborted=True):
        pm = mock.Mock()
        pm.playback_is_aborted.return_value = aborted
        pm.is_playing.return_value = False
        pm.force_window.side_effect = lambda *_a: order.append("force_window")
        pm.claim_menu_keys.side_effect = lambda *_a: order.append("claim")
        # The bridge gates the SyncPlay row; None means "no HUD", which
        # show_menu already handles.
        pm.osc_bridge = None
        # Unpacked in __init__; a bare Mock is not iterable into three.
        pm.get_osd_settings.return_value = ("#000000", 40, "outline")
        menu = OSDMenu(pm, mock.Mock())
        menu.refresh_menu = lambda *a, **k: None
        return menu, pm

    def test_the_window_is_established_before_the_keys_are_claimed(self):
        order = []
        menu, _pm = self._menu(order)
        menu.show_menu()
        self.assertIn("force_window", order)
        self.assertIn("claim", order)
        self.assertLess(
            order.index("force_window"), order.index("claim"),
            "the menu keys were claimed before force_window could re-create "
            "a dead mpv, so the section went to the outgoing handle and the "
            "menu's arrows do nothing")

    def test_the_keys_are_still_claimed_when_the_window_is_already_up(self):
        """The control: with mpv alive `force_window` is not called at all,
        and the claim must still happen or the menu is never navigable."""
        order = []
        menu, pm = self._menu(order, aborted=False)
        menu.show_menu()
        self.assertNotIn("force_window", order)
        self.assertEqual(order, ["claim"])
        pm.claim_menu_keys.assert_called_once_with(True)


if __name__ == "__main__":
    unittest.main()
