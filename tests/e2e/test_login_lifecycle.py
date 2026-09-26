"""Signing back in, against real servers -- and the uuid that must survive it.

`ClientManager.reauthenticate` exists for one reason, and its docstring says
what happens without it: *"remove it and add it again"* was the only route the
UI offered, that route mints a fresh uuid, and the uuid is what the download
catalog's `server_uuid` column and the auto-download allow-list are written in.
So re-adding a signed-out server orphaned every download made from it -- from
the user's point of view, deleted them -- while they were doing the one thing
the UI offered them.

Every test of this so far has been against a fake. What a fake cannot answer:

* **a real server issues a working token and the uuid still does not move.**
  Identity surviving a *successful* round trip through a real login is the
  whole claim, and a stand-in that returns a canned success preserves whatever
  the test told it to;
* **a wrong address is refused before the password goes out.** The method
  connects, asks the address who it is, and returns `REAUTH_WRONG_SERVER`
  *before* `client.auth.login` -- so the user's credentials are never handed
  to a machine that is about to be refused. Checking that needs two servers
  with two different ServerIds answering on two addresses, which is exactly
  what this suite already has;
* **Retry leaves you where you are.** `retry_server` and the switcher shared a
  routine once, and Retry performed a switch without its handover. It is a
  reconnect, not a navigation, and it must not disturb the saved identity.

Needs `JMS_E2E_SERVER_ALT` for the wrong-address half; it skips that class
rather than asserting the part it can reach and calling it the claim.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _accounts  # noqa: E402
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

ACCOUNT = "qa-user"


def _manager():
    """The real `clientManager`, with its config already isolated.

    Isolation first and not as a cleanup: `login` writes `cred.json`, and a
    developer running this suite must not find their own rewritten.

    **Once for the module, because it is a process-wide singleton.** Isolating
    per test gives a new config directory while the manager keeps the
    credentials from every earlier case -- seven logins, one assertion about
    "the" credential, and a failure that looks like reauthenticate minting
    uuids when it is the harness stacking them up.
    """
    global _MANAGER
    if _MANAGER is None:
        _e2e.isolate_config()
        from jellyfin_mpv_shim.clients import clientManager
        from jellyfin_mpv_shim.users import userManager

        userManager.load()
        _MANAGER = clientManager
    return _MANAGER


_MANAGER = None


def tearDownModule():
    """`login` starts a health-check thread and a websocket per client, so a
    module that leaves them running does not exit -- which reads as a hung
    leg rather than a leaked thread. Once, at the end: `stop` latches, and a
    stopped manager refuses the next login."""
    if _MANAGER is not None:
        _MANAGER.stop()


class _SignedIn(unittest.TestCase):
    """One real login, against a manager reset to nothing first."""

    def setUp(self):
        self.manager = _manager()
        # The singleton carries every earlier case's login. Cleared here
        # rather than in a cleanup so a case that dies part way still leaves
        # the next one a known state.
        for cred in list(self.manager.credentials):
            self.manager.remove_client(cred["uuid"])
        self.password = _accounts.password_for(ACCOUNT, _e2e.SERVER)
        self.assertTrue(
            self.manager.login(_e2e.SERVER, ACCOUNT, self.password),
            "could not sign in to %s as %s" % (_e2e.SERVER, ACCOUNT))
        creds = list(self.manager.credentials)
        self.assertEqual(1, len(creds),
                         "expected exactly one credential, got %r" % (creds,))
        self.cred = creds[0]
        self.uuid = self.cred["uuid"]
        self.server_id = self.cred.get("Id")

    def _credential(self, uuid):
        return next((c for c in self.manager.credentials
                     if c.get("uuid") == uuid), None)


@_e2e.require_server
class TheUuidSurvivesSigningBackInTest(_SignedIn):

    def test_reauthenticating_keeps_the_uuid(self):
        """The identity the download catalog is written in.

        A fresh uuid here is not a visible failure -- the server connects, the
        library draws -- and every download made before it becomes unreachable
        at the same moment.
        """
        ok, reason = self.manager.reauthenticate(
            self.uuid, ACCOUNT, self.password)

        self.assertTrue(ok, "reauthenticate failed: %r" % (reason,))
        self.assertIsNone(reason)
        self.assertIsNotNone(
            self._credential(self.uuid),
            "the uuid changed, so every download made from this server is "
            "orphaned: %r" % ([c.get("uuid")
                               for c in self.manager.credentials],))

    def test_it_is_still_one_server_afterwards(self):
        """The other way the identity can break: not a changed uuid but a
        second credential beside it, which is "remove and add" wearing the
        right uuid."""
        self.manager.reauthenticate(self.uuid, ACCOUNT, self.password)

        creds = list(self.manager.credentials)

        self.assertEqual(1, len(creds),
                         "signing back in left %d credentials: %r"
                         % (len(creds), [c.get("uuid") for c in creds]))
        self.assertEqual(self.server_id, creds[0].get("Id"))

    def test_the_token_is_actually_new(self):
        """The control. A method that returned True and changed nothing would
        satisfy every assertion above, and would also not have signed anyone
        back in -- which is the entire point of it."""
        before = self.cred.get("AccessToken")

        self.manager.reauthenticate(self.uuid, ACCOUNT, self.password)

        after = (self._credential(self.uuid) or {}).get("AccessToken")
        self.assertTrue(after, "no token after signing back in")
        self.assertNotEqual(before, after,
                            "the stored token did not change, so nothing was "
                            "re-authenticated")

    def test_a_wrong_password_is_refused_and_changes_nothing(self):
        """A failure must not cost the identity either: the form's whole
        purpose is to be retried."""
        ok, _reason = self.manager.reauthenticate(
            self.uuid, ACCOUNT, self.password + "-wrong")

        self.assertFalse(ok, "the server accepted a wrong password")
        self.assertIsNotNone(self._credential(self.uuid),
                             "a refused sign-in dropped the credential")


@_e2e.require_server
class TheRetryLeavesYouWhereYouAreTest(_SignedIn):

    def test_retrying_a_live_server_succeeds_without_moving_anything(self):
        """`retry_server` is a reconnect, not a navigation. It and the
        switcher shared a routine once, and Retry performed a switch without
        its handover."""
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        ok, problem = PlayerGateway().retry_server(self.uuid)

        self.assertTrue(ok, "retry failed against a live server: %r"
                        % (problem,))
        self.assertIsNone(problem)
        self.assertEqual([self.uuid],
                         [c.get("uuid") for c in self.manager.credentials])


@_e2e.require_server
class AWrongAddressIsRefusedBeforeThePasswordTest(_SignedIn):
    """Two servers, two ServerIds, one saved login -- the only configuration
    that can show this, and the one `JMS_E2E_SERVER_ALT` exists for."""

    def setUp(self):
        self.alt = (os.environ.get("JMS_E2E_SERVER_ALT") or "").rstrip("/")
        if not self.alt:
            raise unittest.SkipTest(
                "set JMS_E2E_SERVER_ALT to a second server to check that a "
                "sign-in to the wrong address is refused")
        super().setUp()
        import json
        import urllib.request

        with urllib.request.urlopen(
                self.alt + "/System/Info/Public", timeout=10) as resp:
            other = json.loads(resp.read()) or {}
        if other.get("Id") == self.server_id:
            raise unittest.SkipTest(
                "JMS_E2E_SERVER_ALT is the same server by ServerId, so it "
                "cannot stand in for the wrong one")

    def _watch_logins(self):
        """Record every `auth.login` reached on a client the manager builds.

        Wrapping the factory and recording that it *ran* observes nothing --
        it runs in both outcomes, since `reauthenticate` builds its client
        before it has anything to decide with. What has to be recorded is
        whether the returned client's `auth.login` was reached, which is the
        call that puts the password on the wire.

        The address and username only. A test that recorded the third
        argument would be a test that writes the user's password into a
        failure message.

        `self.manager` is a process-wide singleton shared by every case in
        this module (`_manager` says why), so the wrap comes off in a
        cleanup. Without that every later case in the run would go through a
        wrapped factory belonging to a class that had finished.
        """
        calls = []
        original = self.manager.client_factory

        def factory(*args, **kwargs):
            client = original(*args, **kwargs)
            inner = client.auth.login

            def login(address, username, *rest, **kw):
                calls.append((address, username))
                return inner(address, username, *rest, **kw)

            client.auth.login = login
            return client

        self.manager.client_factory = factory
        self.addCleanup(setattr, self.manager, "client_factory", original)
        return calls

    def test_the_password_is_not_sent_to_the_wrong_server(self):
        """The module docstring's claim, observed rather than inferred.

        The two cases below watch the return value and the saved credential,
        and an implementation that logged in and *then* returned
        REAUTH_WRONG_SERVER satisfies both -- having handed the user's
        password to a machine it was about to refuse. That is the whole
        reason the guard sits where it does.
        """
        logins = self._watch_logins()

        ok, reason = self.manager.reauthenticate(
            self.uuid, ACCOUNT, self.password, address=self.alt)

        self.assertFalse(ok)
        self.assertEqual(
            [], logins,
            "the password went to %s before the address was refused"
            % self.alt)

    def test_the_watch_sees_a_login_that_should_happen(self):
        """The control, and without it the case above is vacuous.

        A wrap that silently stopped intercepting -- a renamed method, an
        auth object rebuilt after the factory returns -- records nothing, and
        "nothing was recorded" is exactly what the assertion above wants to
        see. So the same wrap is pointed at the address that should succeed.
        """
        logins = self._watch_logins()

        ok, reason = self.manager.reauthenticate(
            self.uuid, ACCOUNT, self.password)

        self.assertTrue(ok, "reauthenticate failed: %r" % (reason,))
        self.assertEqual(
            [(_e2e.SERVER, ACCOUNT)], logins,
            "the wrap recorded %r for a sign-in that did happen, so it is "
            "not watching what it claims to watch" % (logins,))

    def test_quick_connect_refuses_before_the_code_is_shown(self):
        """The same promise on the passwordless half, with a stronger reason.

        Approving a Quick Connect request is something the user goes and does
        in that server's own web session -- so a code shown for a server we
        mean to refuse sends them somewhere else entirely, to do nothing.
        `reauthenticate_with_quick_connect` says exactly that in its own
        comment, and nothing asked it.

        No factory wrap here: `code_callback` IS the act of showing the code,
        so the test supplies it and asserts it was never called.
        """
        from jellyfin_mpv_shim.constants import REAUTH_WRONG_SERVER

        shown = []

        ok, reason = self.manager.reauthenticate_with_quick_connect(
            self.uuid, code_callback=shown.append,
            should_cancel=lambda: True, address=self.alt)

        self.assertFalse(ok)
        self.assertEqual(REAUTH_WRONG_SERVER, reason)
        self.assertEqual(
            [], shown,
            "a Quick Connect code for %s was shown to the user before the "
            "address was refused, sending them to approve it in a session "
            "that has nothing to do with this login" % self.alt)

    def test_quick_connect_does_show_a_code_for_the_right_server(self):
        """The control on the case above, for the same reason as the one on
        the password half: a flow that raised before ever reaching the
        callback would satisfy it while showing nothing about the guard.

        `should_cancel` returns True so the wait gives up at once -- there is
        nobody to approve the request, and the assertion is about the code
        having been offered, not about the sign-in completing.
        """
        shown = []

        self.manager.reauthenticate_with_quick_connect(
            self.uuid, code_callback=shown.append,
            should_cancel=lambda: True)

        self.assertTrue(
            shown,
            "no Quick Connect code was offered for the server the credential "
            "actually names, so the refusal above proves nothing")

    def test_it_says_wrong_server_rather_than_blaming_the_password(self):
        """The reason exists so the form can say which half is wrong. A bare
        False would send the user to change a password that was correct."""
        from jellyfin_mpv_shim.constants import REAUTH_WRONG_SERVER

        ok, reason = self.manager.reauthenticate(
            self.uuid, ACCOUNT, self.password, address=self.alt)

        self.assertFalse(ok)
        self.assertEqual(REAUTH_WRONG_SERVER, reason)

    def test_the_saved_login_is_untouched_by_the_refusal(self):
        """Nothing is re-pointed at the wrong server on the way to refusing
        it -- the credential keeps its address and its ServerId."""
        self.manager.reauthenticate(self.uuid, ACCOUNT, self.password,
                                    address=self.alt)

        cred = self._credential(self.uuid)

        self.assertIsNotNone(cred, "the refusal dropped the credential")
        self.assertEqual(self.server_id, cred.get("Id"))
        self.assertNotIn(self.alt, cred.get("address") or "")


if __name__ == "__main__":
    unittest.main()
