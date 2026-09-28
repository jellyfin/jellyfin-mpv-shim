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
import time
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


class SwitchSpamOnASlowServerTest(_ProfilesCase):
    """Row 50's first race: two profiles, each signed in, and a switch made
    while the server hangs (the relay stalled) -- then straight back. The
    last choice wins once the server answers: Alice active, her Home drawn,
    not a spinner and not Bob's screen arriving late."""

    def test_the_last_switch_wins(self):
        me = _flows.active_profile(self.app.config_dir)
        _flows.add_profile(self.app, "Bob")
        self.assertTrue(_e2e.wait_for(
            lambda: any(u.get("name") == "Bob" for u in
                        (_flows.users(self.app.config_dir) or {})
                        .get("users", [])), timeout=15), "Bob was not added")
        _flows.switch_profile(self.app, "Bob")
        _flows.login(self.app, self.relay, account="qa-nopassword")
        _flows.switch_profile(self.app, me)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="%s's Home" % me)

        names = [u.get("name") for u in
                 _flows.users(self.app.config_dir)["users"]]
        self.relay.stall()
        self.assertTrue(self.relay.probe_silent())
        _flows.pick(self.app, "nav-user", names.index("Bob"))
        self.assertTrue(_e2e.wait_for(
            lambda: _flows.active_profile(self.app.config_dir) == "Bob",
            timeout=30), "the premise: the switch to Bob never happened")
        # users.json moves first; the switcher is drawn a moment later, and
        # a pick made before that lands in a scene being replaced.
        self.app.wait_for(
            lambda f: _flows.selected(f, "nav-user") == names.index("Bob"),
            timeout=30, what="the switcher showing Bob while his server hangs")
        _flows.pick(self.app, "nav-user", names.index(me))
        self.relay.restore()
        self.assertTrue(_e2e.wait_for(
            lambda: _flows.active_profile(self.app.config_dir) == me,
            timeout=60), "the last switch did not win (active: %r)"
            % _flows.active_profile(self.app.config_dir))
        f = self.app.wait_for(lambda f: _app.shown(f, "row-libs"),
                              timeout=90, what="%s's Home" % me)
        self.assertFalse(_app.shown(f, "login-server"),
                         "a sign-in form arrived over %s's Home" % me)
        self.assertEqual(0, self.app.quit(timeout=40))


@_e2e.require_server
class QuickConnectTwiceTest(unittest.TestCase):
    """Row 50's "Quick Connect twice": started, cancelled, started again.
    Approving the FIRST code must not sign in -- it was cancelled -- and
    the second code stays on screen; approving the second signs in."""

    def setUp(self):
        import re
        import _relay
        self.re = re
        self.session = _e2e.Session()
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        import test_playback_lifecycle as _pl
        self.app = _app.App(backend=_pl._backend())
        self.addCleanup(lambda: self.app.close())
        self.app.start()
        self.app.wait_for(lambda f: _app.shown(f, "login-server"),
                          timeout=60, what="the login screen")
        self.app.type_into("login-server", self.relay.address)

    def code(self, frame):
        return next((s for s in _app.texts(frame)
                     if self.re.fullmatch(r"\d{6}", s.strip())), None)

    def start(self, other_than=None):
        self.app.move_to("login-qc")
        self.app.key("ENTER")
        f = self.app.wait_for(
            lambda f: self.code(f) and self.code(f) != other_than,
            timeout=30, what="a Quick Connect code")
        return self.code(f)

    def approve(self, code):
        self.session._request("/QuickConnect/Authorize?code=%s" % code,
                              "POST")

    def test_the_cancelled_code_does_not_sign_in(self):
        first = self.start()
        self.app.move_to("login-qc-cancel")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: not self.code(f), timeout=10,
                          what="the first code gone after Cancel")
        second = self.start(other_than=first)
        self.approve(first)
        deadline = time.monotonic() + 12        # the poll runs every few s
        while time.monotonic() < deadline:
            f = self.app.frame()
            self.assertFalse(_app.shown(f, "row-libs"),
                             "the CANCELLED Quick Connect code signed in")
            self.assertEqual(second, self.code(f),
                             "the second code left the screen")
            time.sleep(0.5)
        self.approve(second)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after approving the second code")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
