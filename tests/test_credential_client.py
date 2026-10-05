"""clientManager.credential_client -- D2's client, built from a saved
credential alone.

Izzie, 2026-09-26: "it doesn't need a websocket connection or posted session
capabilities, just a credential." So what is pinned is: every profile is
searched (the person who queued a download is, by definition, not the active
profile); only the exact (ServerId, UserId) pair matches; the session starts
without a websocket; and it is not registered where the rest of the app looks
for clients.
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
from unittest import mock

sys.argv = [sys.argv[0]]

import jellyfin_mpv_shim.clients as clients  # noqa: E402


class _Client:
    def __init__(self):
        self.auth_args = None
        self.started = []
        self.logged_in = False
        self.config = mock.Mock()
        self.config.auth = lambda *a: setattr(self, "auth_args", a)

    def start(self, websocket=True, keep_alive=True):
        self.started.append(websocket)


PROFILES = [
    {"id": "p-alice", "name": "Alice", "credentials": [
        {"uuid": "login-a", "Id": "SRV", "UserId": "alice",
         "AccessToken": "tok-a", "address": "http://srv"}]},
    {"id": "p-bob", "name": "Bob", "credentials": [
        {"uuid": "login-b", "Id": "SRV", "UserId": "bob",
         "AccessToken": "tok-b", "address": "http://srv"},
        {"uuid": "login-c", "Id": "OTHER", "UserId": "alice",
         "AccessToken": "tok-c", "address": "http://other"}]},
]


class CredentialClientTest(unittest.TestCase):
    def setUp(self):
        from jellyfin_mpv_shim.users import userManager
        for attr, value in (("users", PROFILES), ("active_id", "p-bob")):
            p = mock.patch.object(userManager, attr, value)
            p.start()
            self.addCleanup(p.stop)
        self.cm = clients.ClientManager.__new__(clients.ClientManager)
        self.cm.clients = {}
        self.made = []

        def factory():
            c = _Client()
            self.made.append(c)
            return c

        self.cm.client_factory = factory

    def test_a_login_is_named_by_the_profile_that_holds_it(self):
        """What the Downloads screen says a waiting download waits for."""
        from jellyfin_mpv_shim.users import userManager
        self.assertEqual("Alice", userManager.profile_name_for("SRV", "alice"))
        self.assertEqual("Bob", userManager.profile_name_for("OTHER", "alice"))
        self.assertIsNone(userManager.profile_name_for("SRV", "carol"))

    def test_an_inactive_profiles_credential_is_found_and_used(self):
        uuid, client = self.cm.credential_client("SRV", "alice")
        self.assertEqual("login-a", uuid)
        self.assertEqual(("http://srv", "alice", "tok-a"),
                         client.auth_args[:3])

    def test_no_websocket_and_nothing_registered(self):
        _uuid, client = self.cm.credential_client("SRV", "alice")
        self.assertEqual([False], client.started)
        self.assertEqual({}, self.cm.clients,
                         "a credential client leaked into the app's clients")

    def test_only_the_exact_person_on_the_exact_server(self):
        self.assertEqual("login-c",
                         self.cm.credential_client("OTHER", "alice")[0])
        self.assertIsNone(self.cm.credential_client("SRV", "carol"))
        self.assertIsNone(self.cm.credential_client("NOPE", "alice"))
        self.assertEqual(1, len(self.made),
                         "a client was built for a person with no credential")


if __name__ == "__main__":
    unittest.main()
