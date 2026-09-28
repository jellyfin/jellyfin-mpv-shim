"""The login form starts empty for each arrival (B8).

The form's fields lived in one dict on the browser and nothing cleared them,
so after a profile switch the new profile's login screen arrived holding
the previous profile's server, username and password -- and one Connect
signed the new profile in as the previous person. End to end:
tests/e2e/test_offline_ui.ANewProfileStartsWithAnEmptyLoginTest.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import (FakeController, FakeSource,    # noqa: E402
                                  _SyncPool)

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402

TYPED = {"server": "http://a", "user": "alice", "pass": "hunter2"}


class LoginFormResetTest(unittest.TestCase):
    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        return b

    def test_each_arrival_starts_empty(self):
        b = self._browser()
        for _ in range(3):
            b._login.update(TYPED)
            b.show_login()
            self.assertEqual({"server": "", "user": "", "pass": ""},
                             b._login)

    def test_a_successful_sign_in_forgets_the_password(self):
        """B8's second half. No screen can show it -- every way into the
        form resets it first -- so it is pinned here, in the state: the
        password is not held for one second longer than the sign-in."""
        b = self._browser()

        class _SignsIn(FakeController):
            def add_server(self, *a, **k):
                return True

        b.controller = _SignsIn()
        b.show_login()
        b._login.update(TYPED)
        b._do_login()
        self.assertEqual("", b._login["pass"])
        self.assertEqual(TYPED["user"], b._login["user"],
                         "the premise: only the password is forgotten")

    def test_a_reauth_is_seeded_with_its_own_server_and_no_password(self):
        b = self._browser()
        b._login.update(TYPED)
        b.show_login(reauth={"uuid": "u2", "name": "Away",
                             "address": "http://b", "username": "bob"})
        self.assertEqual({"server": "http://b", "user": "bob", "pass": ""},
                         b._login)


class AnAddDuringASwitchTest(unittest.TestCase):
    """Izzie, 2026-09-28: an Add Server that lands while a profile switch is
    in flight belongs to the profile that was on screen when it began --
    not the one being switched to, which is already `active`."""

    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        return b

    def test_the_password_add_is_filed_under_the_profile_on_screen(self):
        b = self._browser()
        b._switch_from = "alice"
        b.show_login()
        b._login.update(TYPED)
        b._do_login()
        self.assertEqual(["alice"], b.controller.add_owners)

    def test_with_no_switch_it_is_the_active_profile_as_before(self):
        b = self._browser()
        b.show_login()
        b._login.update(TYPED)
        b._do_login()
        self.assertEqual([None], b.controller.add_owners)


class NothingToRetryTest(unittest.TestCase):
    """Izzie, 2026-09-28: Retry on a profile with no saved login only ever
    says it still cannot reach one. So a correct PIN (or a switch) that
    finds nothing to browse signs in when the ACTIVE profile has no login
    -- even though another profile has one -- and offers Retry when it
    does."""

    def _unlock(self, own_servers):
        class _Nothing(FakeController):
            def connect_and_rebuild(self):
                return None                 # nothing answered

            def known_servers(self):        # another profile's address
                return [{"address": "http://other", "name": "Other"}]

            def list_servers(self):         # this profile's own logins
                return own_servers

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=_Nothing())
        b._pool = _SyncPool()
        b.show_locked()
        b._pin["pin"] = "1234"
        b._do_unlock()
        return b.route.get("kind")

    def test_no_login_of_its_own_signs_in(self):
        self.assertEqual("login", self._unlock([]))

    def test_its_own_unreachable_login_offers_retry(self):
        self.assertEqual("connecting", self._unlock(
            [{"uuid": "s1", "name": "Home", "connected": False}]))


if __name__ == "__main__":
    unittest.main()
