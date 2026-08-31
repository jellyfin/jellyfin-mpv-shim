"""The startup PIN: the lock screen is the only page until it is answered.

Parental control, not a security boundary -- it stops a kid on an HTPC
opening an R-rated film, and the tests do not pretend otherwise: anyone who
can attach input can usually read `cred.json`. What it must do is hold against
the ordinary ways into the library, and that is a claim about EVERY door:

    remote GoHome / GoToSettings          app.py on_nav_command
    a phone's DisplayContent              app.py _display_item
    tray Configure Servers / Console      settings/__init__.py open_settings
    a server connecting or reconnecting   app.py set_source
    a tile / any view calling navigate()  app.py

Three of those were open. `set_source` cleared `_locked` outright, and
`on_nav_command("home")` never looked at it -- and because nothing put the
flag back, `show_locked`'s idempotence guard then made every later
`maybe_relock()` a no-op, so the gate could not be shown again for the life of
the process. Settings still refused, so it *looked* like it was holding.

A half-enforced gate is worse than none, because the parent believes the box
is locked. If a new route or entry point appears, the catch-all at the bottom
is what fails.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


class LockedBase(unittest.TestCase):
    LOCKED = True

    def _browser(self, **ctl):
        c = FakeController()
        for k, v in ctl.items():
            setattr(c, k, v)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=c)
        b._pool = _SyncPool()
        b.server = "srv1"
        self.ctl = c
        if self.LOCKED:
            b.show_locked()
        else:
            b.navigate({"kind": "home", "server": "srv1"}, reset=True)
        return b


class TestTheDoorsAreShut(LockedBase):

    def test_it_starts_on_the_lock_screen(self):
        b = self._browser()
        self.assertEqual(b.route["kind"], "locked")
        self.assertTrue(b._locked)

    def test_a_remote_go_home_is_refused(self):
        """`on_nav_command` checked `headless` and never `_locked`, so a
        GeneralCommand/GoHome from any remote-control client walked straight
        past the gate onto a live library."""
        b = self._browser()
        b.on_nav_command("home")
        self.assertEqual(b.route["kind"], "locked",
                         "a remote's GoHome walked past the PIN gate")

    def test_a_remote_settings_is_refused(self):
        b = self._browser()
        b.on_nav_command("settings")
        self.assertEqual(b.route["kind"], "locked")

    def test_a_phone_showing_an_item_is_refused(self):
        b = self._browser()
        b._display_item("srv1", "m1")
        self.assertEqual(b.route["kind"], "locked")

    def test_the_tray_settings_entry_is_refused(self):
        b = self._browser()
        b.open_settings()
        self.assertEqual(b.route["kind"], "locked")

    def test_navigating_to_a_library_page_is_refused(self):
        b = self._browser()
        b.navigate({"kind": "home", "server": "srv1"})
        self.assertEqual(b.route["kind"], "locked")


class TestConnectingDoesNotUnlock(LockedBase):
    """The half that was not a navigation at all.

    Connections are deliberately NOT deferred until unlock -- the gate is
    about what is on screen, not about the network. So a server coming up
    mid-lock is expected; what it must not do is take the gate down with it.
    """

    def test_a_connect_does_not_clear_the_lock(self):
        b = self._browser()
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertTrue(b._locked, "connecting cleared the PIN gate")
        self.assertEqual(b.route["kind"], "locked",
                         "connecting dropped a locked box on the library")

    def test_a_reconnect_does_not_either(self):
        """The periodic health check reconnects on its own schedule and
        notifies through here, so this fires with no user action at all."""
        b = self._browser()
        b.set_source(FakeSource(), server_uuid="srv1", keep_place=True)
        self.assertTrue(b._locked)
        self.assertEqual(b.route["kind"], "locked")

    def test_the_gate_can_still_be_shown_again_afterwards(self):
        """The compounding half of the bug. `_locked` was left False, so
        `show_locked`'s idempotence guard turned every later `maybe_relock`
        into a no-op -- one connect disarmed the gate permanently."""
        b = self._browser()
        b.set_source(FakeSource(), server_uuid="srv1")
        b._locked = False           # as an unlock would leave it
        b.navigate({"kind": "home", "server": "srv1"})
        b.show_locked()
        self.assertEqual(b.route["kind"], "locked",
                         "the gate could not be raised again")


class TestUnlockingStillWorks(LockedBase):
    """The other direction: a gate that never opens is also broken."""

    def test_after_unlocking_the_library_is_reachable(self):
        b = self._browser()
        b._locked = False
        b.navigate({"kind": "home", "server": "srv1"})
        self.assertEqual(b.route["kind"], "home")

    def test_a_source_arriving_after_unlock_lands_on_the_library(self):
        b = self._browser()
        b._locked = False
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.route["kind"], "home")


class TestWithoutTheGateNothingChanges(LockedBase):
    LOCKED = False

    def test_the_library_is_reachable(self):
        b = self._browser()
        b.navigate({"kind": "settings", "server": "srv1"})
        self.assertEqual(b.route["kind"], "settings")

    def test_a_remote_still_opens_home(self):
        b = self._browser()
        b.navigate({"kind": "settings", "server": "srv1"})
        b.on_nav_command("home")
        self.assertEqual(b.route["kind"], "home")


class TestNoRouteEscapesTheGate(unittest.TestCase):
    """The catch-all, in the shape `TestNoRouteEscapesTheLockdown` uses for
    headless. Every route kind the browser declares must either be on the
    locked allow-list or be refused. A route added later is refused by
    default, so the failure mode is a locked box and never an open one."""

    @staticmethod
    def _every_kind(b):
        from jellyfin_mpv_shim.mpvtk_browser.pages import PAGES

        return sorted(set(b._routes()) | set(PAGES))

    def test_every_declared_route_is_either_allowed_or_refused(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.show_locked()

        leaked = []
        for kind in self._every_kind(b):
            if kind in b.LOCKED_ROUTES:
                continue
            b._locked = True
            b.nav_stack = [{"kind": "locked"}]
            b.navigate({"kind": kind, "server": "srv1", "parent_id": "lib1",
                        "item_id": "m1", "person_id": "p1"})
            if b.route["kind"] != "locked":
                leaked.append(kind)
        self.assertEqual(leaked, [],
                         "these routes are reachable behind the PIN gate: %s"
                         % leaked)

    def test_the_allow_list_is_only_what_the_gate_needs(self):
        """Guards the other direction: something added for convenience would
        silently widen the gate."""
        self.assertEqual(set(MpvtkBrowser.LOCKED_ROUTES), {"locked"})


if __name__ == "__main__":
    unittest.main()


class TestSwitchingUserOffTheLockScreen(LockedBase):
    """The other way off the gate, and it must not be a dead end.

    `_render_locked` offers a user switcher precisely so a locked user cannot
    lock the whole client out. `set_source` no longer clears `_locked` -- a
    server connecting must not open the gate -- so the switch has to clear it
    itself. Without that the switch lands back on the lock screen, and if the
    new user has no PIN then `verify_pin` answers False for every entry
    (users.py: no `pin_hash`, no match), so nothing can unlock it again for
    the life of the process.

    Driven through the REAL `_do_switch_user`. Two earlier versions of this
    test were worthless: one hand-set `_locked = False` before calling
    `set_source`, i.e. performed the fix itself; the other read the method's
    source for `self._locked = False`, which the `source is None` branch
    already contains earlier in the text. Both passed with the fix reverted.
    """

    def test_a_successful_switch_leaves_the_gate_open(self):
        b = self._browser()
        self.assertTrue(b._locked)
        self.assertEqual(b.route["kind"], "locked")

        b.controller.switch_user = lambda user_id, pin: FakeSource()
        b._do_switch_user({"id": "u2"}, "1234")

        self.assertFalse(
            b._locked,
            "the switch landed back on the lock screen; if the new user has "
            "no PIN nothing can ever unlock it again")
        self.assertNotEqual(b.route["kind"], "locked")

    def test_a_refused_switch_leaves_the_gate_shut(self):
        """The control: a wrong PIN must NOT open the gate."""
        b = self._browser()
        b.controller.switch_user = lambda user_id, pin: False
        bad = []
        b._do_switch_user({"id": "u2"}, "0000", on_bad_pin=lambda: bad.append(1))

        self.assertTrue(b._locked, "a refused switch opened the gate")
        self.assertEqual(b.route["kind"], "locked")
        self.assertEqual(bad, [1])
