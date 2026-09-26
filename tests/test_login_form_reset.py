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

    def test_a_reauth_is_seeded_with_its_own_server_and_no_password(self):
        b = self._browser()
        b._login.update(TYPED)
        b.show_login(reauth={"uuid": "u2", "name": "Away",
                             "address": "http://b", "username": "bob"})
        self.assertEqual({"server": "http://b", "user": "bob", "pass": ""},
                         b._login)


if __name__ == "__main__":
    unittest.main()
