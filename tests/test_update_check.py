"""Unit tests for update-notice routing.

When a UI is running (it sets ``playerManager.notify_update``) the update
notice must go to that callback (the browser banner); otherwise it falls back
to an MPV OSD toast. These exercise the routing without any network or Tk.
"""

import unittest
from unittest import mock

import jellyfin_mpv_shim.update_check as uc
from jellyfin_mpv_shim.update_check import UpdateChecker, release_url


class _Resp:
    """Stand-in for the GitHub /releases/latest redirect."""
    def __init__(self, version):
        self.status_code = 302
        self.headers = {"location": release_url + "tag/v" + version}


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

    def test_first_check_notifies_when_update_found(self):
        # Regression: _check_updates() used to `return` inside the for loop, so
        # a found update (which `break`s) returned None and check() skipped the
        # notify on the run that discovered it -- the daily throttle then hid it
        # until the next day. The first check must notify immediately.
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            rq.get.return_value = _Resp("99.0.0")
            chk.check()
        self.assertEqual(chk.new_version, "99.0.0")
        self.assertEqual(player.ui_calls, [("99.0.0", release_url + "latest")])

    def test_no_notify_when_up_to_date(self):
        from jellyfin_mpv_shim.constants import CLIENT_VERSION
        player = FakePlayer(with_ui=True)
        chk = UpdateChecker(player)
        with mock.patch.object(uc, "requests") as rq, \
                mock.patch.object(uc.settings, "check_updates", True), \
                mock.patch.object(uc.settings, "notify_updates", True):
            rq.get.return_value = _Resp(CLIENT_VERSION)
            chk.check()
        self.assertIsNone(chk.new_version)
        self.assertEqual(player.ui_calls, [])

    def test_osd_fallback_when_ui_callback_raises(self):
        player = FakePlayer(with_ui=True)
        player.notify_update = lambda *_a: (_ for _ in ()).throw(RuntimeError())
        chk = UpdateChecker(player)
        chk.new_version = "2.9.0"
        chk.notify()  # must not raise; falls back to the OSD
        self.assertEqual(len(player.osd_calls), 1)


if __name__ == "__main__":
    unittest.main()
