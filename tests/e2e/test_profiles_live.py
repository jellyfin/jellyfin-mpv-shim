"""Slice S6, row 50: profiles and PINs, at the keyboard of the shipped app.

The integration tests drive a FakeAuthController; here the PIN is set in
Settings > Servers & Users by keys, and the gates are the app's own.

- a PIN required at startup gates the next launch: a wrong PIN is refused,
  the right one opens Home;
- switching to a locked profile asks for its PIN: a wrong one is refused
  and nothing switches, the right one does.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
from test_playback_lifecycle import _PlaybackCase  # noqa: E402

PIN = "4827"


class _ProfilesCase(_PlaybackCase):

    def user(self, name):
        reg = _flows.users(self.app.config_dir) or {}
        return next(u for u in reg.get("users", []) if u.get("name") == name)

    def row_of(self, name):
        """The Users list's row index for ``name``: its order in users.json."""
        reg = _flows.users(self.app.config_dir) or {}
        return [u.get("name") for u in reg.get("users", [])].index(name)

    def set_pin(self, name, startup=False):
        _flows.open_settings_tab(self.app, "servers")
        button = "su-pin-%d" % self.row_of(name)
        self.app.wait_for(lambda f: _app.shown(f, button), timeout=15,
                          what="%s's Set PIN" % name)
        self.app.move_to(button)
        self.app.press_until("ENTER", lambda f: _app.shown(f, "ps-new"),
                             what="the PIN dialog")
        self.app.type_into("ps-new", PIN, masked=True)
        self.app.type_into("ps-confirm", PIN, masked=True)
        if startup:
            self.app.move_to("ps-startup")
            self.app.key("ENTER")
        self.app.move_to("ps-ok")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: not _app.shown(f, "ps-new"), timeout=15,
                          what="the PIN dialog to close")

    def refused(self, frame):
        return "Incorrect PIN." in _app.texts(frame)


class AStartupPinGatesTheNextLaunchTest(_ProfilesCase):

    def test_wrong_is_refused_and_right_opens_home(self):
        me = _flows.active_profile(self.app.config_dir)
        self.set_pin(me, startup=True)
        u = self.user(me)
        self.assertTrue(u.get("require_pin_startup"),
                        "the startup requirement was not saved: %r"
                        % sorted(u))
        self.app = _flows.relaunch(self.app, self.relay)
        self.app.wait_for(lambda f: _app.shown(f, "lock-pin"), timeout=60,
                          what="the lock screen")
        self.assertFalse(_app.shown(self.app.frame(), "row-libs"))
        self.app.type_into("lock-pin", "0000", masked=True)
        self.app.key("ENTER")
        self.app.wait_for(self.refused, timeout=15, what="Incorrect PIN.")
        self.assertTrue(_app.shown(self.app.frame(), "lock-pin"),
                        "a wrong PIN got past the gate")
        self.app.clear_field("lock-pin")
        self.app.type_into("lock-pin", PIN, masked=True)
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the right PIN")
        self.assertEqual(0, self.app.quit(timeout=30))


class SwitchingToALockedProfileTest(_ProfilesCase):

    def test_wrong_pin_stays_right_pin_switches(self):
        me = _flows.active_profile(self.app.config_dir)
        _flows.add_profile(self.app, "Bob")
        self.assertTrue(_e2e.wait_for(
            lambda: any(u.get("name") == "Bob" for u in
                        (_flows.users(self.app.config_dir) or {})
                        .get("users", [])), timeout=15), "Bob was not added")
        self.set_pin("Bob")
        self.assertTrue(self.user("Bob").get("pin_hash"), "Bob has no PIN")

        switch = "su-sw-%d" % self.row_of("Bob")
        self.app.wait_for(lambda f: _app.shown(f, switch), timeout=15,
                          what="Bob's Switch")
        self.app.move_to(switch)
        self.app.press_until("ENTER", lambda f: _app.shown(f, "switch-pin"),
                             what="the PIN prompt")
        self.app.type_into("switch-pin", "0000", masked=True)
        self.app.key("ENTER")
        self.app.wait_for(self.refused, timeout=15, what="Incorrect PIN.")
        self.assertEqual(me, _flows.active_profile(self.app.config_dir),
                         "a wrong PIN switched profiles")
        self.app.clear_field("switch-pin")
        self.app.type_into("switch-pin", PIN, masked=True)
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(
            lambda: _flows.active_profile(self.app.config_dir) == "Bob",
            timeout=30), "the right PIN did not switch to Bob")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
