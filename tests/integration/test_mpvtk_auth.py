"""User switching and the PIN gate, in a REAL mpv window.

The Tk browser has 12 integration tests covering this (test_browser_ui.py
:468-600) and they die with it. The unit tests added in
tests/test_mpvtk_ui_wiring.py cover the logic, but not the path that
actually matters here: a PIN is typed into a renderer-owned textbox, and
whether those keystrokes reach Python at all is a property of renderer.lua's
focus/textbox handling, not of the browser.

So these drive the real thing — click to focus, type via the renderer's key
path, submit — and assert on what the browser then did. A regression in
focus routing, in the commit/submit protocol, or in the lock gate itself
shows up here and nowhere else.

Deliberately paranoid about the gate specifically: a lock screen that can be
bypassed is the worst failure this UI has, and it is invisible to a unit
test that calls _do_unlock() directly.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

from test_mpvtk_browser import _make_source, _spawn_handle  # noqa: E402


class FakeAuthController:
    """Just the surface the auth screens use."""

    def __init__(self, users=None, pin="1234", locked_at_startup=False):
        self.users = users or [
            {"id": "u1", "name": "Izzie", "locked": False, "active": True},
        ]
        self.pin = pin
        self._locked_at_startup = locked_at_startup
        self.unlock_attempts = []
        self.switch_attempts = []
        self.source = None

    # -- what the browser calls --
    def list_users(self):
        return [dict(u) for u in self.users]

    def needs_unlock(self):
        return self._locked_at_startup

    def unlock(self, pin):
        self.unlock_attempts.append(pin)
        return pin == self.pin

    def connect_and_rebuild(self):
        return self.source

    def known_servers(self):
        return []

    def switch_user(self, user_id, pin=None):
        self.switch_attempts.append((user_id, pin))
        user = next((u for u in self.users if u["id"] == user_id), None)
        if user and user.get("locked") and pin != self.pin:
            return False
        return self.source

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda *a, **k: None


@h.require_real_mpv
class MpvtkAuthBase(unittest.TestCase):
    CONTROLLER_KW = {}

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-auth-itest-")))
        self.ctl = FakeAuthController(**self.CONTROLLER_KW)
        self.source = _make_source()
        self.ctl.source = self.source
        self.browser = MpvtkBrowser(self.app, self.source, strips=strips,
                                    controller=self.ctl)
        self.browser.server = "srv1"
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.assertTrue(self.app.ready.wait(15),
                        "renderer never became ready in the attached mpv")

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            self.browser.shutdown(free_bitmaps=False)
            try:
                self.handle.terminate()
            except Exception:
                pass

    # -- helpers ---------------------------------------------------------

    def _wait(self, pred, why, timeout=6.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.15)
        self.fail(why)

    def _wait_rendered(self, node_id, timeout=6.0):
        """Wait until `node_id` is hit-testable, i.e. the scene carrying it
        has actually reached the renderer. Clicking earlier races the push."""
        def present():
            return any(n.get("id") == node_id
                       for n in (self.app._nodes or []))
        self._wait(present, "%s never rendered" % node_id, timeout)

    def _type_into(self, node_id, text):
        """Focus a textbox by clicking it and type through the renderer's
        real key path — not by calling on_change, which would prove nothing
        about whether keystrokes reach the field."""
        self._wait_rendered(node_id)
        self.app.debug(cmd="click", id=node_id)
        self._wait(lambda: (self.app.debug_state() or {}).get("focus")
                   == node_id, "clicking %s did not focus it" % node_id)
        self.app.debug(cmd="text", s=text)


class TestTheStartupPinGate(MpvtkAuthBase):
    CONTROLLER_KW = {"locked_at_startup": True}

    def test_a_typed_pin_reaches_the_verifier(self):
        self.browser.show_locked()
        self._type_into("lock-pin", "1234")
        self._wait_rendered("lock-unlock")
        self.app.debug(cmd="click", id="lock-unlock")
        self._wait(lambda: self.ctl.unlock_attempts,
                   "the typed PIN never reached the controller")
        self.assertEqual(self.ctl.unlock_attempts[-1], "1234",
                         "the field's contents did not survive the round trip")

    def test_a_correct_pin_opens_the_library(self):
        self.browser.show_locked()
        self._type_into("lock-pin", "1234")
        self.app.debug(cmd="click", id="lock-unlock")
        self._wait(lambda: self.browser.route["kind"] != "locked",
                   "a correct PIN did not unlock")
        self.assertFalse(self.browser._locked)

    def test_a_wrong_pin_keeps_the_gate_closed(self):
        self.browser.show_locked()
        self._type_into("lock-pin", "9999")
        self.app.debug(cmd="click", id="lock-unlock")
        self._wait(lambda: self.browser._pin_error,
                   "a wrong PIN produced no error")
        self.assertEqual(self.browser.route["kind"], "locked",
                         "a wrong PIN got past the gate")

    def test_enter_submits_the_pin(self):
        """The field wires on_submit; if the renderer stopped delivering
        ENTER to a focused textbox the only way in would be the button."""
        self.browser.show_locked()
        self._type_into("lock-pin", "1234")
        self.app.debug(cmd="key", name="ENTER")
        self._wait(lambda: self.ctl.unlock_attempts,
                   "ENTER in the PIN field did nothing")

    def test_the_lock_screen_hides_the_library_chrome(self):
        """The gate is worthless if the nav bar behind it still works."""
        self.browser.show_locked()
        self._wait_rendered("lock-pin")
        ids = {n.get("id") for n in (self.app._nodes or [])}
        for nid in ("nav-home", "nav-search", "nav-settings"):
            self.assertNotIn(nid, ids,
                             "%s is reachable from the lock screen" % nid)


TWO_USERS = [
    {"id": "u1", "name": "Izzie", "locked": False, "active": True},
    {"id": "u2", "name": "Kids", "locked": False, "active": False},
]

LOCKED_OTHER = [
    {"id": "u1", "name": "Izzie", "locked": False, "active": True},
    {"id": "u2", "name": "Parent", "locked": True, "active": False},
]


class TestTheUserSwitcher(MpvtkAuthBase):
    CONTROLLER_KW = {"users": TWO_USERS}

    def test_the_switcher_renders_with_two_users(self):
        self.browser.navigate({"kind": "home", "server": "srv1"})
        self._wait_rendered("nav-user")

    def test_switching_to_an_unlocked_user_needs_no_pin(self):
        self.browser.navigate({"kind": "home", "server": "srv1"})
        self._wait_rendered("nav-user")
        self.browser._switch_user(dict(TWO_USERS[1]))
        self._wait(lambda: self.ctl.switch_attempts,
                   "the switch was never requested")
        user_id, pin = self.ctl.switch_attempts[-1]
        self.assertEqual(user_id, "u2")
        self.assertIsNone(pin)


class TestSwitchingToALockedUser(MpvtkAuthBase):
    CONTROLLER_KW = {"users": LOCKED_OTHER}

    def test_it_prompts_before_switching(self):
        self.browser.navigate({"kind": "home", "server": "srv1"})
        self._wait_rendered("nav-user")
        self.browser._switch_user(dict(LOCKED_OTHER[1]))
        self._wait_rendered("switch-pin")
        self._wait(lambda: (self.app.debug_state() or {}).get("modal_open"),
                   "the PIN prompt is not a modal — the UI behind it is live")
        self.assertEqual(self.ctl.switch_attempts, [],
                         "switched to a locked user without asking for a PIN")

    def test_a_correct_pin_carries_through_to_the_switch(self):
        self.browser.navigate({"kind": "home", "server": "srv1"})
        self._wait_rendered("nav-user")
        self.browser._switch_user(dict(LOCKED_OTHER[1]))
        self._type_into("switch-pin", "1234")
        self._wait_rendered("switch-ok")
        self.app.debug(cmd="click", id="switch-ok")
        self._wait(lambda: self.ctl.switch_attempts,
                   "the switch was never requested")
        self.assertEqual(self.ctl.switch_attempts[-1], ("u2", "1234"))

    def test_a_wrong_pin_does_not_switch(self):
        self.browser.navigate({"kind": "home", "server": "srv1"})
        self._wait_rendered("nav-user")
        self.browser._switch_user(dict(LOCKED_OTHER[1]))
        self._type_into("switch-pin", "0000")
        self._wait_rendered("switch-ok")
        self.app.debug(cmd="click", id="switch-ok")
        self._wait(lambda: self.ctl.switch_attempts,
                   "the switch was never attempted")
        self.assertEqual(self.ctl.switch_attempts[-1], ("u2", "0000"))
        # Refused: the dialog stays up rather than silently doing nothing.
        self._wait(lambda: self.browser._dialog is not None,
                   "a refused PIN closed the dialog anyway")


if __name__ == "__main__":
    unittest.main()
