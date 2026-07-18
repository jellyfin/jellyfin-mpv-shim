"""Unit tests for update-notice routing.

When a UI is running (gui_mgr sets ``playerManager.notify_update``) the update
notice must go to that callback (the browser banner); otherwise it falls back
to an MPV OSD toast. These exercise the routing without any network or Tk.
"""

import unittest

from jellyfin_mpv_shim.update_check import UpdateChecker, release_url


class FakePlayer:
    def __init__(self, with_ui):
        self.osd_calls = []
        self.ui_calls = []
        if with_ui:
            self.notify_update = lambda version, url: self.ui_calls.append(
                (version, url))
        # else: attribute absent, mirroring a CLI player

    def show_text(self, text, duration, level):
        self.osd_calls.append((text, duration, level))


class UpdateNoticeRoutingTest(unittest.TestCase):
    def test_routes_to_ui_when_callback_present(self):
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()
        self.assertEqual(player.ui_calls, [("2.9.0", release_url + "latest")])
        self.assertEqual(player.osd_calls, [])

    def test_falls_back_to_osd_without_ui(self):
        player = FakePlayer(with_ui=False)
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()
        self.assertEqual(player.ui_calls, [])
        self.assertEqual(len(player.osd_calls), 1)
        self.assertIn("2.9.0", player.osd_calls[0][0])

    def test_osd_fallback_when_ui_callback_raises(self):
        player = FakePlayer(with_ui=True)
        player.notify_update = lambda *_a: (_ for _ in ()).throw(RuntimeError())
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()  # must not raise; falls back to the OSD
        self.assertEqual(len(player.osd_calls), 1)


if __name__ == "__main__":
    unittest.main()
