"""Getting back a server that stopped connecting.

Before this there was one way out of a signed-out or unreachable server, and
it was to **remove it and add it again** -- which mints a new uuid, and the
uuid is what the download catalog's `server_uuid` column and the
auto-download allow-list are written in. So the only offered repair silently
disowned every download made from that server, at the moment the user was
doing the one thing the UI suggested.

Three layers, and the split is deliberate:

* `ClientManager` tells "did not answer" from "would not accept the login",
  and re-authenticates without changing the identity.
* The gateway hands both facts to the browser.
* The browser lists the server anyway, and offers the two buttons.

The middle layer is barely more than plumbing, so it is tested through the
other two rather than on its own.
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
import threading
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

import jellyfin_mpv_shim.clients as clients  # noqa: E402
from jellyfin_mpv_shim.constants import (  # noqa: E402
    CONNECT_BUSY, CONNECT_SIGNED_OUT, CONNECT_UNREACHABLE, REAUTH_WRONG_SERVER)
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeController,
    FakeSource,
    _SyncPool,
    build_scene,
    ids,
)


# --------------------------------------------------------------- clients


class FakeAPI:
    """Only `get_public_info`, which is the whole of the diagnosis."""

    def __init__(self, answers=True):
        self.answers = answers
        self.asked = []

    def get_public_info(self, address, session):
        self.asked.append(address)
        if isinstance(self.answers, Exception):
            raise self.answers
        return {"Id": "srv"} if self.answers else None


class FakeAuth:
    def __init__(self, api):
        self.API = api
        self.session = object()


class FakeJellyfinClient:
    def __init__(self, api, state):
        self.auth = FakeAuth(api)
        self._state = state
        self.stopped = False
        self.callback = None
        self.callback_ws = None

    def authenticate(self, creds, discover=False):
        return {"State": self._state}

    def stop(self):
        self.stopped = True


class WhyAServerDidNotConnectTest(unittest.TestCase):
    """The apiclient cannot be asked: `connect_to_server` answers
    `Unavailable` both for a socket that refused and for a token the server
    rejected -- the revoked-token branch falls into the same return as a
    network error. One of those is waited out and the other needs a password,
    so the two are told apart here."""

    def _manager(self, api, on_lan=None):
        # The name lookup a successful connect makes is stubbed, not left
        # to run: `http://h` resolves fast here and might not on a box whose
        # resolver appends a search domain, and a unit suite that waits on
        # DNS is a unit suite that fails somewhere else.
        self._resolve = mock.patch.object(
            clients, "resolved_host_is_private", lambda address: on_lan)
        self._resolve.start()
        self.addCleanup(self._resolve.stop)
        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._stop_event = threading.Event()
        mgr._client_lock = threading.RLock()
        mgr._switch_lock = threading.RLock()
        mgr._connecting = set()
        mgr._removed_uuids = set()
        mgr._connect_failures = {}
        mgr._server_on_lan = {}
        mgr._lan_probe = {}
        mgr._lan_seq = 0
        mgr._user_generation = 0
        mgr.clients = {}
        mgr.usernames = {}
        mgr.credentials = []
        # Not about the concurrency: run the locality probe inline. See
        # TheLocalityLookupDoesNotHoldTheConnectTest for that half.
        mgr._spawn = lambda target, name, args=(): target(*args)
        mgr.client_factory = lambda: FakeJellyfinClient(
            api, clients.CONNECTION_STATE["Unavailable"])
        # The websocket wiring is a different subject with its own tests; a
        # fake that reached it would be measuring `setup_client`.
        mgr.setup_client = lambda client, server, do_retries=True: None
        return mgr

    def test_a_server_that_answers_is_a_login_problem(self):
        api = FakeAPI(answers=True)
        mgr = self._manager(api)
        self.assertFalse(mgr.connect_client(
            {"uuid": "u1", "address": "http://h", "Id": "s"}))
        self.assertEqual(mgr.connection_problem("u1"), CONNECT_SIGNED_OUT)
        self.assertEqual(api.asked, ["http://h"])

    def test_a_server_that_does_not_answer_is_a_network_problem(self):
        mgr = self._manager(FakeAPI(answers=False))
        self.assertFalse(mgr.connect_client(
            {"uuid": "u1", "address": "http://h", "Id": "s"}))
        self.assertEqual(mgr.connection_problem("u1"), CONNECT_UNREACHABLE)

    def test_a_probe_that_raises_is_read_as_unreachable(self):
        """Wrong in the direction that offers Retry rather than demanding a
        password the user may not need to type."""
        mgr = self._manager(FakeAPI(answers=RuntimeError("no route")))
        self.assertFalse(mgr.connect_client(
            {"uuid": "u1", "address": "http://h", "Id": "s"}))
        self.assertEqual(mgr.connection_problem("u1"), CONNECT_UNREACHABLE)

    def test_connecting_clears_the_verdict(self):
        """Over several attempts, because a stale verdict is exactly what a
        remembered failure becomes: a server that comes back must stop
        offering Sign In Again, and it must stay stopped."""
        api = FakeAPI(answers=True)
        mgr = self._manager(api)
        server = {"uuid": "u1", "address": "http://h", "Id": "s"}
        mgr.connect_client(server)
        self.assertEqual(mgr.connection_problem("u1"), CONNECT_SIGNED_OUT)

        mgr.client_factory = lambda: FakeJellyfinClient(
            api, clients.CONNECTION_STATE["SignedIn"])
        mgr.clients.pop("u1", None)
        self.assertTrue(mgr.connect_client(server))
        for _ in range(3):
            self.assertIsNone(mgr.connection_problem("u1"),
                              "a connected server still reports a problem")

    def test_a_connect_already_in_flight_is_not_a_failure(self):
        """CR10. The health check is already connecting when the user presses
        Retry: the reservation refuses the second attempt and recorded nothing,
        so `connection_problem` answered None and the dialog fell through to
        "%s did not answer. It may be switched off, or this machine may not be
        able to reach it right now." -- about a server being connected to at
        that moment.

        Driven through the reservation rather than by setting `_connecting`, so
        what is asserted is the state a real second caller sees.
        """
        api = FakeAPI(answers=True)
        mgr = self._manager(api)
        server = {"uuid": "u1", "address": "http://h", "Id": "s"}
        seen = []
        first = mgr.client_factory

        def reenter():
            # From inside the first connect, where `_connecting` holds u1.
            if not seen:
                seen.append((mgr.connect_client(server),
                             mgr.connection_problem("u1")))
            return first()

        mgr.client_factory = reenter
        mgr.connect_client(server)
        self.assertEqual(seen, [(False, CONNECT_BUSY)])

    def test_and_it_is_forgotten_the_moment_that_connect_finishes(self):
        """The other direction: `_connecting` is live state, not a stored
        verdict, so a connect that ends leaves nothing behind. Stored, an
        abandoned connect would have left every later Retry saying "wait"."""
        mgr = self._manager(FakeAPI(answers=False))
        server = {"uuid": "u1", "address": "http://h", "Id": "s"}
        mgr.connect_client(server)
        self.assertEqual(mgr.connection_problem("u1"), CONNECT_UNREACHABLE)
        self.assertEqual(mgr._connecting, set())

    def test_a_connected_server_never_reports_a_problem(self):
        """The registry outranks the remembered failure. They are written by
        different threads -- the health check reconnects on its own schedule
        -- so a UI reading only the failure map would offer Sign In Again
        under a server it is browsing."""
        mgr = self._manager(FakeAPI(answers=True))
        mgr._connect_failures["u1"] = CONNECT_SIGNED_OUT
        mgr.clients["u1"] = object()
        self.assertIsNone(mgr.connection_problem("u1"))


class ReauthenticationKeepsTheIdentityTest(unittest.TestCase):
    """**The point of the whole feature.** `login()` mints a fresh
    `uuid.uuid4()`, so signing back in by removing and re-adding produced a
    server the download catalog had never heard of."""

    def _manager(self, token=True):
        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._stop_event = threading.Event()
        mgr._client_lock = threading.RLock()
        mgr._switch_lock = threading.RLock()
        mgr._connecting = set()
        mgr._removed_uuids = set()
        mgr._connect_failures = {}
        mgr._user_generation = 0
        mgr.clients = {}
        mgr.usernames = {}
        mgr.credentials = [{"uuid": "keep-me", "Id": "s1",
                            "address": "http://h:8096", "username": "izzie"}]
        mgr.saved = []
        mgr.save_credentials = lambda: mgr.saved.append(
            [dict(c) for c in mgr.credentials])
        mgr.connect_client = lambda server, do_retries=True: True
        mgr.disconnected = []
        mgr._disconnect_client = lambda uuid=None, server=None, \
            expected_client=None: mgr.disconnected.append(uuid) or True

        class Auth:
            def __init__(self):
                self.credentials = self
                self.logged_in = []
                # WHICH server answered. Hardcoding it made every re-auth
                # look like the same server coming back, which is the one
                # thing a re-auth must establish rather than assume: the
                # address is editable, so the answer can be a different
                # server entirely.
                self.server_id = "s1"

            def get_credentials(self):
                return {"Servers": [{"Id": self.server_id,
                                     "address": "http://h:8096",
                                     "Name": "Home"}]}

            def connect_to_address(self, address):
                return None

            def login(self, address, username, password):
                self.logged_in.append((address, username, password))
                return {"AccessToken": "t"} if token else {}

        self.auth = Auth()

        class Client:
            auth = self.auth

            def stop(self):
                pass

        mgr.client_factory = lambda: Client()
        # Kept, so a test can drive `_finalize_login` directly rather than
        # only through `reauthenticate`.
        self.client = Client()
        return mgr

    def test_the_uuid_survives(self):
        mgr = self._manager()
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "pw"),
                         (True, None))
        self.assertEqual([c["uuid"] for c in mgr.credentials], ["keep-me"],
                         "re-authenticating changed the server's identity, "
                         "which orphans its downloads")

    def test_it_replaces_rather_than_adds(self):
        mgr = self._manager()
        mgr.reauthenticate("keep-me", "izzie", "pw")
        self.assertEqual(len(mgr.credentials), 1,
                         "the server is now saved twice")

    def test_a_refused_login_changes_nothing(self):
        mgr = self._manager(token=False)
        before = [dict(c) for c in mgr.credentials]
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "wrong"),
                         (False, None))
        self.assertEqual(mgr.credentials, before)

    def test_an_unknown_uuid_is_refused_without_a_network_call(self):
        mgr = self._manager()
        self.assertEqual(mgr.reauthenticate("nope", "izzie", "pw"),
                         (False, None))
        self.assertEqual(self.auth.logged_in, [],
                         "sent a password to a server we do not have")

    def _real_user_manager(self, users, active):
        """The real ``UserManager`` with its list set, not a stand-in.

        The credential handoff runs through `append_credentials_for`, whose
        replace-by-uuid rule is the half a hand-written fake would drop --
        and dropping it is what makes an "it was filed correctly" assertion
        pass while the credential is filed twice.
        """
        from jellyfin_mpv_shim import users as users_mod

        um = users_mod.UserManager()
        um.users = users
        um.active_id = active
        um.save = lambda: None
        original = clients.userManager
        clients.userManager = um
        self.addCleanup(setattr, clients, "userManager", original)
        return um

    def test_a_different_server_is_refused_the_uuid(self):
        """The uuid is what the download catalog's `server_uuid` column and
        the auto-download allow-list are written in, and the re-auth form
        lets the address be edited -- deliberately, because a server that
        moved is what it is for. So typing a *different* server's address
        handed that server the old uuid, and every download from the old
        one was then attributed to a server that never had it.
        """
        mgr = self._manager()
        self.auth.server_id = "s2"
        before = [dict(c) for c in mgr.credentials]
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "pw"),
                         (False, REAUTH_WRONG_SERVER))
        self.assertEqual(self.auth.logged_in, [],
                         "sent the password to the wrong server before "
                         "refusing it")
        self.assertEqual(mgr.credentials, before,
                         "another server was given this server's identity")

    def test_a_credential_with_no_recorded_id_is_not_refused(self):
        """Nothing may be locked out by a check it cannot satisfy.

        A credential saved before the server `Id` was kept has nothing to
        compare against, and refusing there would leave the user no way back
        in except remove-and-re-add -- which mints a new uuid, i.e. the
        exact loss this feature exists to prevent. It is allowed, and the
        replacement records the `Id`, so the state self-heals after one
        re-auth rather than staying unverifiable forever.
        """
        mgr = self._manager()
        mgr.credentials = [{"uuid": "keep-me", "address": "http://h:8096",
                            "username": "izzie"}]
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "pw"),
                         (True, None))
        self.assertEqual([c["uuid"] for c in mgr.credentials], ["keep-me"])
        self.assertEqual(mgr.credentials[0].get("Id"), "s1",
                         "the next re-auth is still unverifiable")

    def test_a_server_removed_mid_re_auth_is_not_refused_by_the_check(self):
        """The other way the prior credential can be missing, and it must
        not be the check that turns it into a refusal.

        A login is slow, and the server can be removed from the Servers tab
        while one is in flight. `_finalize_login` already decides what that
        means -- it discards the removal tombstone, because an explicit
        login supersedes an earlier removal -- so the check has nothing to
        compare against and says so by allowing.
        """
        mgr = self._manager()
        mgr.credentials = []
        self.assertTrue(mgr._finalize_login(self.client, "izzie",
                                            replacing_uuid="keep-me"))
        self.assertEqual([c["uuid"] for c in mgr.credentials], ["keep-me"])

    def test_a_re_auth_landing_after_a_user_switch_is_not_refused(self):
        """The regression that got the first attempt reverted.

        A login is slow enough to span a user switch, and when it does the
        credential is filed back under the user who started it. The check
        therefore has to look in *that* user's list: the reverted repair
        searched the active user's, found nothing, and refused a legitimate
        re-auth on a path that worked before it.
        """
        um = self._real_user_manager(
            users=[{"id": "owner", "credentials": [
                        {"uuid": "keep-me", "Id": "s1",
                         "address": "http://h:8096"}]},
                   {"id": "other", "credentials": []}],
            active="other")
        mgr = self._manager()
        # What the switch actually leaves behind: `_adopt_active_user`
        # repoints the live list at the user now active, so the credential
        # being re-authenticated is NOT in it. Leaving it there is a fixture
        # that hides the whole defect -- the reverted repair searched this
        # list and passed.
        mgr.credentials = []
        self.assertTrue(mgr._finalize_login(self.client, "izzie",
                                            owner_id="owner",
                                            replacing_uuid="keep-me"))
        self.assertEqual(
            [c["uuid"] for c in um.get("owner")["credentials"]], ["keep-me"])
        self.assertEqual(um.get("other")["credentials"], [],
                         "filed under whoever happened to be active")

    def test_a_different_server_is_refused_after_a_switch_too(self):
        """The same comparison on the other branch. Both lists, one rule --
        a check applied to one of two paths is this repository's recurring
        defect shape, and the handoff branch is the one that is easy to
        forget because it is the rare one.
        """
        um = self._real_user_manager(
            users=[{"id": "owner", "credentials": [
                        {"uuid": "keep-me", "Id": "s1",
                         "address": "http://h:8096"}]},
                   {"id": "other", "credentials": []}],
            active="other")
        mgr = self._manager()
        mgr.credentials = []      # as above: the switch repointed the list
        self.auth.server_id = "s2"
        self.assertFalse(mgr._finalize_login(self.client, "izzie",
                                             owner_id="owner",
                                             replacing_uuid="keep-me"))
        self.assertEqual(
            [c.get("Id") for c in um.get("owner")["credentials"]], ["s1"],
            "another server was given this server's identity")

    def test_quick_connect_refuses_before_showing_the_code(self):
        """The passwordless half, and the ordering is the point.

        Approving a Quick Connect request is something the user goes and does
        in that server's own web session, so a code for a request we mean to
        refuse sends them off to another machine to accomplish nothing.
        """
        mgr = self._manager()
        self.auth.server_id = "s2"
        shown = []
        mgr.quick_connect_initiate = lambda address: (self.client, "sec",
                                                      "ABC123")
        self.assertEqual(
            mgr.reauthenticate_with_quick_connect(
                "keep-me", code_callback=shown.append),
            (False, REAUTH_WRONG_SERVER))
        self.assertEqual(shown, [], "asked for approval of a refused request")

    def test_a_moved_server_may_be_given_a_new_address(self):
        """The address is editable on the form, because a server that moved
        is a real reason for the saved login to stop working -- and the
        identity still has to survive that."""
        mgr = self._manager()
        mgr.reauthenticate("keep-me", "izzie", "pw",
                           address="http://newhost:8096")
        self.assertEqual(self.auth.logged_in[0][0], "http://newhost:8096")
        self.assertEqual([c["uuid"] for c in mgr.credentials], ["keep-me"])


# --------------------------------------------------------------- browser


class RecoveryController(FakeController):
    """A controller with one connected server and one that is not."""

    def __init__(self, problem=CONNECT_UNREACHABLE, retry_succeeds=False):
        super().__init__()
        self.problem = problem
        self.retry_succeeds = retry_succeeds
        self.retried = []
        self.reauthed = []
        self.qc_reauthed = []

    def list_servers(self):
        return [{"uuid": "srv1", "name": "Home", "address": "http://h",
                 "username": "izzie", "connected": True, "problem": None},
                {"uuid": "srv2", "name": "Away", "address": "http://a",
                 "username": "guest", "connected": False,
                 "problem": self.problem}]

    def switcher_servers(self):
        return self.list_servers()

    def retry_server(self, uuid):
        self.retried.append(uuid)
        if self.retry_succeeds:
            return True, None
        return False, self.problem

    def rebuild_source(self):
        source = FakeSource()
        source.servers = lambda: [{"uuid": "srv1", "name": "Home"},
                                  {"uuid": "srv2", "name": "Away"}]
        return source

    #: Set to a REAUTH_* constant to model a refusal.
    reauth_reason = None

    def reauthenticate(self, uuid, username, password, address=None):
        self.reauthed.append((uuid, username, password, address))
        if self.reauth_reason:
            return False, self.reauth_reason
        return True, None

    def reauthenticate_quick_connect(self, uuid, code_callback=None,
                                     should_cancel=None, address=None):
        self.qc_reauthed.append((uuid, address))
        if self.reauth_reason:
            # The real one refuses before the code is shown; a stand-in that
            # showed one anyway would make that ordering untestable.
            return False, self.reauth_reason
        if code_callback is not None:
            code_callback("ABC123")
        return True, None


def _browser(ctl):
    b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
    b._pool = _SyncPool()
    b.server = "srv1"
    return b


def _settle(b):
    t = b._long_thread
    if t is not None:
        t.join(5)


class TheSwitcherListsWhatIsSavedTest(unittest.TestCase):
    """A server that did not answer is not in the source, so it simply
    vanished from the top bar -- the machine looked like it had one server
    and there was nothing to press to get the other back."""

    @staticmethod
    def _switcher(b):
        """The switcher's entries. Read off the node rather than from the
        drawn text: a closed dropdown draws only its current selection, so
        every other entry is invisible to a text sweep and an assertion made
        that way passes whatever the list holds."""
        nodes, _h = build_scene(b, size=(1600, 900))
        for n in nodes:
            if n.get("id") == "nav-server":
                return n.get("items") or []
        return None

    def test_a_server_that_is_not_connected_is_still_offered(self):
        items = self._switcher(_browser(RecoveryController()))
        self.assertIsNotNone(items, "no server switcher, so the second "
                                    "server is unreachable from the UI "
                                    "entirely")
        self.assertTrue(any("Away" in t for t in items),
                        "the offline server is missing from the switcher: "
                        "%r" % items)

    def test_it_says_the_server_is_offline(self):
        items = self._switcher(_browser(RecoveryController()))
        self.assertTrue(any("Away" in t and "offline" in t.lower()
                            for t in items),
                        "an entry that looks available and then refuses is "
                        "worse than one that says so first: %r" % items)

    def test_a_connected_entry_is_not_marked(self):
        items = self._switcher(_browser(RecoveryController()))
        self.assertIn("Home", items,
                      "every entry was marked offline: %r" % items)

    def test_the_offline_library_does_not_list_saved_servers(self):
        """There the source *is* the answer to every server being away, the
        offline banner owns the retry, and an entry that cannot be browsed
        is worse than none."""
        ctl = RecoveryController()
        b = _browser(ctl)
        b.set_offline(True)
        items = self._switcher(b) or []
        self.assertFalse(any("offline)" in t for t in items),
                         "the offline library offered a server switcher: "
                         "%r" % items)


class PickingAnOfflineServerTriesToGetItBackTest(unittest.TestCase):

    def test_it_reconnects_instead_of_navigating(self):
        ctl = RecoveryController(retry_succeeds=False)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertEqual(ctl.retried, ["srv2"])
        self.assertEqual(b.server, "srv1",
                         "navigated to a server the source does not hold, "
                         "which draws an empty library and explains nothing")

    def test_a_successful_reconnect_switches_to_it(self):
        ctl = RecoveryController(retry_succeeds=True)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertEqual(b.server, "srv2")
        self.assertIsNone(b._dialog, "raised a failure dialog after success")

    def test_a_failed_reconnect_offers_both_ways_out(self):
        ctl = RecoveryController(retry_succeeds=False)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertIsNotNone(b._dialog, "the switch failed silently")
        nodes, _h = build_scene(b)
        self.assertIn("srvfail-retry", ids(nodes))
        self.assertIn("srvfail-reauth", ids(nodes))

    def test_both_ways_out_are_offered_whichever_the_verdict_is(self):
        """The probe can only say whether the address answered, and it is
        wrong in both directions often enough to matter -- a proxy that is up
        in front of a server that is not reads as signed out. So the verdict
        chooses the wording, never which buttons exist."""
        for problem in (CONNECT_SIGNED_OUT, CONNECT_UNREACHABLE, None):
            with self.subTest(problem=problem):
                b = _browser(RecoveryController(problem=problem))
                b._switch_server("srv2")
                _settle(b)
                nodes, _h = build_scene(b)
                self.assertIn("srvfail-retry", ids(nodes))
                self.assertIn("srvfail-reauth", ids(nodes))

    def test_a_successful_reconnect_leaves_the_old_syncplay_group(self):
        """A group belongs to the server it was joined on. The direct switch
        has always left it; the reconnect path arrived without that and left
        it standing, with no way to reach it from this UI -- the same rule
        applied at one of two sites."""
        ctl = RecoveryController(retry_succeeds=True)
        left = []
        ctl.sync_active = lambda: True
        ctl.sync_leave = lambda srv: left.append(srv)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertEqual(left, ["srv1"])

    def test_a_failed_reconnect_stays_in_the_group(self):
        """The other half: leaving the group for a switch that did not
        happen is worse than the thing being avoided."""
        ctl = RecoveryController(retry_succeeds=False)
        left = []
        ctl.sync_active = lambda: True
        ctl.sync_leave = lambda srv: left.append(srv)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertEqual(left, [])

    def test_a_connect_already_in_flight_does_not_report_a_failure(self):
        """CR10 at the door the user sees. Something else is already connecting
        -- the health check, a websocket reconnect -- so this is the same state
        as pressing Retry twice, which this screen already has a line for. The
        failure dialog would be about a failure that has not happened, and its
        text ("It may be switched off") is the opposite of what is going on.
        """
        ctl = RecoveryController(problem=CONNECT_BUSY, retry_succeeds=False)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertIsNone(b._dialog, "reported a failure for a connect that "
                                     "is still in progress")
        self.assertEqual(b.status, "Already reconnecting.")
        self.assertEqual(b.server, "srv1")

    def test_a_reconnect_whose_switch_fails_is_not_a_switch(self):
        """CR2. `on_success` is the *switch's* handover -- leave the SyncPlay
        group on the server being left, remember the new one -- and it fired
        whenever the *connect* succeeded. When `rebuild_source` raises,
        `set_source` is skipped and `self.server` never moves, so the user was
        dropped out of their group and `last_server` persisted a server the UI
        is not showing, for a switch that did not happen.

        "The success path" in the comment above `arrived` means the switch,
        not the connect.
        """
        ctl = RecoveryController(retry_succeeds=True)

        def boom():
            raise RuntimeError("the source could not be rebuilt")

        ctl.rebuild_source = boom
        left = []
        ctl.sync_active = lambda: True
        ctl.sync_leave = lambda srv: left.append(srv)
        b = _browser(ctl)
        b._switch_server("srv2")
        _settle(b)
        self.assertEqual(b.server, "srv1", "switched with no source to show")
        self.assertEqual(left, [],
                         "left the SyncPlay group for a switch that did not "
                         "happen")
        self.assertNotIn("set_last_server",
                         [name for name, _a in ctl.transport],
                         "persisted a server the UI is not showing")

    def test_a_connected_server_is_switched_to_directly(self):
        """The control: the reconnect is only for servers the source does not
        hold, or every ordinary switch would take a network round trip."""
        ctl = RecoveryController()
        b = _browser(ctl)
        source = FakeSource()
        source.servers = lambda: [{"uuid": "srv1", "name": "Home"},
                                  {"uuid": "srv2", "name": "Away"}]
        b.source = source
        b._switch_server("srv2")
        _settle(b)
        self.assertEqual(ctl.retried, [])
        self.assertEqual(b.server, "srv2")


class TheServersTabOffersARepairTest(unittest.TestCase):

    def _settings(self, ctl):
        b = _browser(ctl)
        b.open_settings("servers")
        return b

    def test_an_offline_row_has_retry_and_sign_in_again(self):
        b = self._settings(RecoveryController())
        nodes, _h = build_scene(b, size=(1600, 900))
        node_ids = ids(nodes)
        self.assertIn("sv-retry-1", node_ids)
        self.assertIn("sv-reauth-1", node_ids)

    def test_a_connected_row_has_neither(self):
        b = self._settings(RecoveryController())
        node_ids = ids(build_scene(b, size=(1600, 900))[0])
        self.assertNotIn("sv-retry-0", node_ids)
        self.assertNotIn("sv-reauth-0", node_ids)

    def test_the_verdict_puts_the_likely_button_first(self):
        """The emphasis this app has. There is no primary-button styling, so
        order is what says which one to press -- and both stay reachable,
        because the verdict is a probe's guess and not a fact.

        Asserted on where the buttons were **laid out**, not on the order of
        `ids()`, which is a set: an ordering assertion made over one of those
        is reading iteration order and passes whatever the row builds."""
        for problem, first in ((CONNECT_SIGNED_OUT, "sv-reauth-1"),
                               (CONNECT_UNREACHABLE, "sv-retry-1"),
                               (None, "sv-retry-1")):
            with self.subTest(problem=problem):
                b = self._settings(RecoveryController(problem=problem))
                nodes, _h = build_scene(b, size=(1600, 900))
                at = {n["id"]: n["x"] for n in nodes
                      if n.get("id") in ("sv-retry-1", "sv-reauth-1")}
                self.assertEqual(len(at), 2,
                                 "a way out went missing: %r" % sorted(at))
                self.assertLess(at[first],
                                at["sv-retry-1" if first == "sv-reauth-1"
                                   else "sv-reauth-1"],
                                "%s is not the leading button" % first)

    def test_a_signed_out_server_says_so_rather_than_offline(self):
        """Three states, not two. "Offline" under a server that answered and
        refused the login sends the user to check their network."""
        b = self._settings(RecoveryController(problem=CONNECT_SIGNED_OUT))
        labels = [n.get("text") or ""
                  for n in build_scene(b, size=(1600, 900))[0]
                  if n["t"] == "text"]
        self.assertIn("Signed out", labels)

    def test_retry_asks_the_controller(self):
        ctl = RecoveryController(retry_succeeds=True)
        b = self._settings(ctl)
        _n, h = build_scene(b, size=(1600, 900))
        h["sv-retry-1"]["click"]()
        _settle(b)
        self.assertEqual(ctl.retried, ["srv2"])

    def test_sign_in_again_opens_the_form_for_that_server(self):
        b = self._settings(RecoveryController())
        _n, h = build_scene(b, size=(1600, 900))
        h["sv-reauth-1"]["click"]()
        self.assertEqual(b.route["kind"], "login")
        self.assertEqual((b.route.get("_reauth") or {}).get("uuid"), "srv2")
        self.assertEqual(b._login["server"], "http://a",
                         "the form did not pre-fill the server's address")
        self.assertEqual(b._login["user"], "guest")
        self.assertEqual(b._login["pass"], "",
                         "carried a password over from another login")


class ManyServersController(RecoveryController):
    """N saved servers, all connected, counting what the screen asks it."""

    def __init__(self, count):
        super().__init__()
        self.count = count
        self.registry_asks = 0

    def list_servers(self):
        return [{"uuid": "srv%d" % i, "name": "S%d" % i,
                 "address": "http://h%d" % i, "username": "izzie",
                 "connected": True, "problem": None}
                for i in range(self.count)]

    switcher_servers = list_servers

    def auto_download_on(self, server_uuid):
        self.registry_asks += 1
        return False

    def auto_download_logins(self):
        self.registry_asks += 1
        return set()


class TheServersScreenAsksTheRegistryOncePerBuildTest(unittest.TestCase):
    """**The registry lock is not a render-path lock.**

    `_auto_dl_on` ran per credential per repaint and reaches
    `UserManager.auto_download_accounts()`, which takes `UserManager._lock` --
    the lock `save()` holds across two durable file writes and two directory
    fsyncs, and the lock `server_id_for`/`actor_for` are documented as
    deliberately never taking, for exactly this reason. Ticking the checkbox
    calls `save()`, so on a slow or network-backed config directory the next
    frame blocked the render thread for the whole of it, once per server row.

    **Counted rather than thresholded.** The assertion is that the count does
    not grow with the number of rows, which needs no magic number and survives
    the screen gaining an unrelated question. That the answer is still the
    account's is `TheRegistryAnswersEveryLoginInOneLockTest` in
    tests/test_auto_download_accounts.py, one layer down, together with the
    lock itself -- which cannot be counted from here, because the controller
    is a stand-in and never reaches the registry.

    The finding named `general.py:_auto_dl_note` as the shape to copy. There is
    no such method: it is `_auto_dl_scope_name`, and it read `list_servers()`
    once and then asked per server -- so the model to copy had the same defect,
    and that screen is the second site rather than the precedent.
    """

    def _asks(self, open_tab, servers):
        ctl = ManyServersController(servers)
        b = _browser(ctl)
        open_tab(b)
        ctl.registry_asks = 0          # count the repaint, not the open
        build_scene(b, size=(1600, 900))
        return ctl.registry_asks

    @staticmethod
    def _servers_tab(b):
        b.open_settings("servers")

    @staticmethod
    def _general_tab(b):
        b.open_settings("general")

    def test_the_servers_tab_does_not_scale_with_the_rows(self):
        self.assertEqual(self._asks(self._servers_tab, 2),
                         self._asks(self._servers_tab, 8),
                         "the registry is asked once per server row")

    def test_the_general_tabs_note_does_not_either(self):
        """The second site, exercised directly rather than through a scene.

        The note is built only for the `auto_download_enable` row, which this
        harness's settings screen does not draw -- so counting it through
        `build_scene` counts zero either way and asserts nothing. Calling the
        builder is what poses the question.
        """
        counts = []
        for servers in (2, 8):
            ctl = ManyServersController(servers)
            b = _browser(ctl)
            ctl.registry_asks = 0
            b._auto_dl_scope_name()
            counts.append(ctl.registry_asks)
        self.assertNotEqual([0, 0], counts,
                            "the note never asked the registry at all, so "
                            "this measures nothing")
        self.assertEqual(counts[0], counts[1],
                         "the registry is asked once per server row")


class RetryReconnectsAndDoesNotSwitchTest(unittest.TestCase):
    """**Retry is not a server switch.** Ruled 2026-09-19.

    `reconnect_server` always ended in `set_source(source, server_uuid=uuid)`,
    which moves `self.server` and resets the nav stack. The top-bar switcher
    calls it with an `on_success` carrying the switch's handover -- leave the
    SyncPlay group on the server being left, remember the new one -- and both
    Retry buttons passed none. So pressing Retry on a second server from
    Settings performed a switch without its handover: the group stayed joined
    on the old server with no way to reach it from the UI, the user was thrown
    out of Settings onto the other server's Home, and the persisted
    last-server still named the old one, so the next launch opened there.

    Why the obvious single fix is wrong, and why this is not simply "run the
    handover in `reconnect_server`": `_switch_server` guards
    `if uuid == self.server: return` before its handover and `reconnect_server`
    has no such guard, and Retry is rendered for **any** unconnected server
    including the one being browsed -- so that fix would call `sync_leave` on
    the server just reconnected. The last test here is that case.
    """

    def _settings(self, ctl):
        b = _browser(ctl)
        b.open_settings("servers")
        return b

    def test_the_browsed_server_does_not_move(self):
        b = self._settings(RecoveryController(retry_succeeds=True))
        _n, h = build_scene(b, size=(1600, 900))
        h["sv-retry-1"]["click"]()
        _settle(b)
        self.assertEqual("srv1", b.server,
                         "Retry on another server switched the one being "
                         "browsed")

    def test_and_the_user_is_still_in_settings(self):
        b = self._settings(RecoveryController(retry_succeeds=True))
        before = list(b.nav_stack)
        _n, h = build_scene(b, size=(1600, 900))
        h["sv-retry-1"]["click"]()
        _settle(b)
        self.assertEqual("settings", b.route.get("kind"),
                         "Retry threw the user out of Settings onto a Home "
                         "screen")
        self.assertEqual(before, list(b.nav_stack),
                         "the nav stack was reset under the user")

    def test_and_no_syncplay_group_is_left(self):
        """The half with no way back. A group belongs to the server it was
        joined on and this UI only talks to the selected one, so leaving that
        server means leaving the group -- which is why the switch carries the
        handover. A reconnect that is not a switch must not perform half of
        it, and must not perform the other half either.
        """
        b = self._settings(RecoveryController(retry_succeeds=True))
        with mock.patch.object(b, "_leave_syncplay_on") as left:
            _n, h = build_scene(b, size=(1600, 900))
            h["sv-retry-1"]["click"]()
            _settle(b)
        left.assert_not_called()

    def test_retry_on_the_server_being_browsed_leaves_its_group_alone(self):
        """The case that rules out putting the handover inside
        `reconnect_server`. Retry is offered for an unconnected server and the
        one being browsed can be unconnected -- a server that dropped while
        you were reading it -- so the uuid reconnected and the uuid browsed
        are the same, and a handover here would leave the group on the very
        server just recovered.
        """
        ctl = RecoveryController(retry_succeeds=True)
        b = self._settings(ctl)
        b.server = "srv2"                   # browsing the one that dropped
        with mock.patch.object(b, "_leave_syncplay_on") as left:
            _n, h = build_scene(b, size=(1600, 900))
            h["sv-retry-1"]["click"]()
            _settle(b)
        left.assert_not_called()
        self.assertEqual(["srv2"], ctl.retried,
                         "the reconnect itself did not happen")


class TheReauthFormReplacesRatherThanAddsTest(unittest.TestCase):

    def _form(self, ctl=None):
        ctl = ctl or RecoveryController()
        b = _browser(ctl)
        b.show_login(reauth={"uuid": "srv2", "name": "Away",
                             "address": "http://a", "username": "guest"})
        return b, ctl

    def test_submitting_re_authenticates_the_saved_server(self):
        b, ctl = self._form()
        b._login["pass"] = "hunter2"
        _n, h = build_scene(b, size=(1600, 900))
        h["login-connect"]["click"]()
        self.assertEqual(ctl.reauthed,
                         [("srv2", "guest", "hunter2", "http://a")])
        self.assertEqual([c for c in getattr(ctl, "transport", [])
                          if c[0] == "add_server"], [],
                         "added a second server instead of replacing one")

    def test_quick_connect_re_authenticates_too(self):
        """The passwordless half. Left on the add-a-server call it would have
        minted a new uuid from the one screen a user reaches *because* their
        saved login stopped working."""
        b, ctl = self._form()
        _n, h = build_scene(b, size=(1600, 900))
        h["login-qc"]["click"]()
        self.assertEqual(ctl.qc_reauthed, [("srv2", "http://a")])

    def test_a_wrong_server_says_so_rather_than_blaming_the_password(self):
        """The message is the half that reaches the user.

        A refusal used to arrive as the same line a wrong password gets, so
        the one person it exists for -- someone whose password is right and
        whose address is not -- would have retyped the password instead of
        looking at the field that is actually wrong.
        """
        ctl = RecoveryController()
        ctl.reauth_reason = REAUTH_WRONG_SERVER
        b, _ctl = self._form(ctl)
        b._login["pass"] = "hunter2"
        _n, h = build_scene(b, size=(1600, 900))
        h["login-connect"]["click"]()
        self.assertIn("different server", b._login_error or "")

    def test_an_ordinary_failure_still_says_check_your_details(self):
        """The control. Only a reason worth acting on differently earns its
        own string, or the specific one stops meaning anything."""
        ctl = RecoveryController()
        ctl.reauth_reason = "something_else"
        b, _ctl = self._form(ctl)
        b._login["pass"] = "wrong"
        _n, h = build_scene(b, size=(1600, 900))
        h["login-connect"]["click"]()
        self.assertNotIn("different server", b._login_error or "")
        self.assertIn("check your details", (b._login_error or "").lower())

    def test_quick_connect_reports_a_wrong_server_too(self):
        ctl = RecoveryController()
        ctl.reauth_reason = REAUTH_WRONG_SERVER
        b, _ctl = self._form(ctl)
        _n, h = build_scene(b, size=(1600, 900))
        h["login-qc"]["click"]()
        self.assertIn("different server", b._login_error or "")

    def test_it_lands_on_the_server_it_just_rescued(self):
        """A re-auth adds no server, so "which one is new" is empty by
        construction and the fallback lands on whichever was browsed last --
        i.e. not the one the user just signed into on purpose."""
        b, ctl = self._form()
        b._login["pass"] = "pw"
        _n, h = build_scene(b, size=(1600, 900))
        h["login-connect"]["click"]()
        self.assertEqual(b.server, "srv2")

    def test_the_form_names_the_server(self):
        """It is reachable for either server with the fields pre-filled, so a
        user who cannot see which one has to guess whose password to type."""
        b, _ctl = self._form()
        labels = [n.get("text") or ""
                  for n in build_scene(b, size=(1600, 900))[0]
                  if n["t"] == "text"]
        self.assertTrue(any("Away" in t for t in labels), labels)

    def test_it_does_not_offer_other_servers_to_switch_to(self):
        """Every "Previously added servers" entry fills in a *different*
        address, which is the one edit that turns this form back into "add a
        server" without saying so."""
        class Known(RecoveryController):
            def known_servers(self):
                return [{"address": "http://old.example",
                         "name": "Old Server"}]

        b, _ctl = self._form(Known())
        self.assertNotIn("login-known-0",
                         ids(build_scene(b, size=(1600, 900))[0]))

    def test_the_ordinary_add_server_form_is_unchanged(self):
        """The control on all of the above: without a reauth the same screen
        must still add a server, and still offer the known list."""
        class Known(RecoveryController):
            def known_servers(self):
                return [{"address": "http://old.example",
                         "name": "Old Server"}]

        ctl = Known()
        b = _browser(ctl)
        b.show_login()
        b._login.update({"server": "good", "user": "u", "pass": "p"})
        _n, h = build_scene(b, size=(1600, 900))
        self.assertIn("login-known-0", ids(_n))
        h["login-connect"]["click"]()
        self.assertEqual(ctl.reauthed, [])
        self.assertEqual([c for c in getattr(ctl, "transport", [])
                          if c[0] == "add_server"],
                         [("add_server", ("good", "u", "p"))])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TheSwitcherSaysWhereEachServerIsTest(unittest.TestCase):
    """Every entry carries an icon, and that is not only decoration.

    A dropdown indents *every* row as soon as any item has one
    (`draw_list`'s `indent` in renderer.lua), so marking only the broken
    entries spent that width on all of them and bought nothing with it --
    the names ellipsized and the space beside them was blank. Giving each
    server a glyph for where it lives is what makes the gutter earn its
    keep, and says something worth knowing while it is there.
    """

    @staticmethod
    def _icons(b, node_id="nav-server"):
        nodes, _h = build_scene(b, size=(1600, 900))
        for n in nodes:
            if n.get("id") == node_id:
                return n.get("icons")
        return None

    @staticmethod
    def _glyph(name):
        from jellyfin_mpv_shim.mpvtk.vector import icon_ass
        return icon_ass(name)

    def test_a_local_server_is_marked_as_local(self):
        ctl = RecoveryController()
        ctl.list_servers = lambda: [
            {"uuid": "srv1", "name": "Home", "address": "http://192.168.1.5",
             "username": "izzie", "connected": True, "problem": None},
            {"uuid": "srv2", "name": "Away",
             "address": "https://jf.example.com", "username": "guest",
             "connected": True, "problem": None}]
        ctl.switcher_servers = ctl.list_servers
        icons = self._icons(_browser(ctl))
        self.assertEqual(icons, [self._glyph("lan"), self._glyph("cloud")])

    def test_an_offline_server_is_marked_as_offline(self):
        icons = self._icons(_browser(RecoveryController()))
        self.assertEqual(icons[1], self._glyph("cloud_off"))

    def test_no_entry_is_left_blank(self):
        """The one that pays for the gutter. An empty slot here is width
        every other row is charged for."""
        icons = self._icons(_browser(RecoveryController()))
        self.assertTrue(all(icons), "a switcher entry has no icon, so the "
                                    "list indents for a column one of its "
                                    "rows leaves empty: %r" % icons)

    def test_the_user_picker_marks_every_entry_too(self):
        b = _browser(RecoveryController())
        b.controller.list_users = lambda: [
            {"id": "u1", "name": "(default)", "active": True,
             "locked": False},
            {"id": "u2", "name": "testuser", "active": False,
             "locked": True}]
        icons = self._icons(b, "nav-user")
        self.assertTrue(icons and all(icons),
                        "the user picker indents for an icon column and "
                        "leaves a slot in it empty: %r" % (icons,))


class ThePickersOpenWiderThanTheyAreTest(unittest.TestCase):
    """The closed control shares the top bar with Search and the nav
    buttons; the open list has the whole window. Names that have to be
    shortened on the bar can still be read in full where the choice is
    actually made."""

    @staticmethod
    def _node(b, node_id):
        nodes, _h = build_scene(b, size=(1600, 900))
        return next((n for n in nodes if n.get("id") == node_id), None)

    def _with_long_names(self):
        ctl = RecoveryController()
        ctl.list_servers = lambda: [
            {"uuid": "srv1", "connected": True, "problem": None,
             "address": "http://192.168.1.5", "username": "izzie",
             "name": "Living Room Media Server (upstairs)"},
            {"uuid": "srv2", "connected": True, "problem": None,
             "address": "https://jf.example.com", "username": "guest",
             "name": "Parents' House Jellyfin Server"}]
        ctl.switcher_servers = ctl.list_servers
        return _browser(ctl)

    def test_the_server_list_is_wider_than_the_server_box(self):
        node = self._node(self._with_long_names(), "nav-server")
        self.assertIsNotNone(node)
        self.assertGreater(node["pw"], node["w"],
                           "the open list is no wider than the control, so "
                           "a name too long for the top bar cannot be read "
                           "anywhere")

    def test_the_user_list_is_wider_than_the_user_box(self):
        b = self._with_long_names()
        b.controller.list_users = lambda: [
            {"id": "u1", "name": "(default)", "active": True},
            {"id": "u2", "name": "a-rather-long-account-name",
             "active": False}]
        node = self._node(b, "nav-user")
        self.assertIsNotNone(node)
        self.assertGreater(node["pw"], node["w"])

    def test_but_short_names_do_not_stretch_it(self):
        """A ceiling, not a width. Otherwise every install with two servers
        called "Home" and "Work" gets a popup the width of the bar."""
        node = self._node(_browser(RecoveryController()), "nav-server")
        self.assertLess(node["pw"], 460)


class WhereAServerLivesTest(unittest.TestCase):
    """Local or remote, read off the address without resolving it.

    Syntactic on purpose: a DNS lookup to choose an icon would be a blocking
    call on the render path, and it would still be wrong under split-horizon
    DNS. The cost of being wrong is a slightly wrong picture beside a name
    the user can read anyway.
    """

    @staticmethod
    def _icon(address, **kw):
        from jellyfin_mpv_shim.mpvtk_browser.components import server_icon
        return server_icon(dict(kw, address=address))

    def test_the_private_ranges_are_local(self):
        for addr in ("http://192.168.1.5:8096", "http://10.0.0.9",
                     "http://172.16.4.1:8096", "http://127.0.0.1:8096",
                     "http://[fd00::1]:8096", "http://[::1]:8096"):
            with self.subTest(addr=addr):
                self.assertEqual(self._icon(addr), "lan")

    def test_a_public_address_is_not(self):
        for addr in ("https://jf.example.com", "http://93.184.216.34:8096",
                     "https://media.example.co.uk/jellyfin"):
            with self.subTest(addr=addr):
                self.assertEqual(self._icon(addr), "cloud")

    def test_a_name_only_a_lan_can_resolve_is_local(self):
        """mDNS, NetBIOS and the router's own DNS. A bare hostname with no
        dot is not a name the public internet can answer, which is what
        makes it a LAN name."""
        for addr in ("http://mediaserver:8096", "https://jelly.local:8920",
                     "http://nas.lan", "http://box.internal:8096"):
            with self.subTest(addr=addr):
                self.assertEqual(self._icon(addr), "lan")

    def test_carrier_grade_nat_is_read_as_remote(self):
        """100.64/10 is a private overlay as often as it is an ISP's shared
        address space, so it gets the answer that claims less."""
        self.assertEqual(self._icon("http://100.64.3.2:8096"), "cloud")

    def test_a_server_that_is_down_says_that_instead(self):
        """The state outranks the place: which network it is on is not the
        thing the user needs to know about a server that will not answer."""
        self.assertEqual(self._icon("http://192.168.1.5", connected=False),
                         "cloud_off")

    def test_nothing_to_read_is_not_a_crash(self):
        """The offline source's pseudo-server has no address, and a
        hand-edited credential can hold anything at all."""
        for addr in (None, "", "not a url", "http://", "::::"):
            with self.subTest(addr=addr):
                self.assertIn(self._icon(addr), ("lan", "cloud"))


class TheLocalityLookupDoesNotHoldTheConnectTest(unittest.TestCase):
    """`getaddrinfo` takes no timeout and cannot be given one, so asking it
    on the connect path lets a stalled resolver hold a client that is
    already registered and usable, keep the uuid's in-flight reservation
    occupied, and delay every later address in the same chain. All to
    choose between a `lan` and a `cloud` icon.

    Every test here blocks the resolver on an `Event` and asserts on state,
    never on the clock: a timing assertion would be the flake the first
    attempt at this was reverted for.
    """

    def _manager(self):
        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._stop_event = threading.Event()
        mgr._client_lock = threading.RLock()
        mgr._switch_lock = threading.RLock()
        mgr._connecting = set()
        mgr._removed_uuids = set()
        mgr._connect_failures = {}
        mgr._server_on_lan = {}
        mgr._lan_probe = {}
        mgr._lan_seq = 0
        mgr._user_generation = 0
        mgr.clients = {}
        mgr.usernames = {}
        mgr.credentials = []
        mgr.client_factory = lambda: FakeJellyfinClient(
            FakeAPI(answers=True), clients.CONNECTION_STATE["SignedIn"])
        mgr.setup_client = lambda client, server, do_retries=True: None
        return mgr

    def _stalled_resolver(self, answer=True, on_call=None, stall=2):
        """A resolver parked exactly where a real stalled one parks.

        The wait is bounded so that a synchronous implementation fails an
        assertion rather than hanging the suite -- an unbounded block would
        make the regression look like an infrastructure problem.

        ``on_call`` runs *while* it is parked, which is the only vantage
        point some of this has: with the lookup on the connect path the main
        thread is inside `connect_client` for the whole stall and can
        observe nothing.
        """
        started, release, calls = threading.Event(), threading.Event(), []

        def resolver(address):
            calls.append(address)
            if on_call is not None:
                on_call()
            started.set()
            release.wait(stall)
            return answer

        patch = mock.patch.object(clients, "resolved_host_is_private",
                                  resolver)
        patch.start()
        self.addCleanup(patch.stop)
        self.addCleanup(release.set)
        return started, release, calls

    @staticmethod
    def _connect(mgr, address="https://iwalton.com"):
        return mgr.connect_client({"uuid": "u1", "Id": "s",
                                   "address": address})

    def test_the_connect_returns_while_the_resolver_is_still_stalled(self):
        mgr = self._manager()
        started, release, _calls = self._stalled_resolver()
        self.assertTrue(self._connect(mgr))
        self.assertTrue(started.wait(5), "the lookup never ran at all")
        self.assertIsNone(mgr.server_is_local("u1"),
                          "the connect waited for the resolver: the verdict "
                          "was already written when it returned")
        release.set()

    def test_the_uuid_is_free_again_while_the_lookup_runs(self):
        """The reservation is the part that stalls *other* connects: while a
        uuid sits in `_connecting`, every other connector for that server is
        turned away.

        Driven from a worker so the durable state can be read while the
        resolver is still parked, and the two budgets are deliberately
        lopsided: the resolver is held far longer than the connect is given
        to return, so a lookup back on the connect path fails this rather
        than finishing inside the wait and passing.
        """
        mgr = self._manager()
        started, release, _calls = self._stalled_resolver(stall=30)
        done = threading.Event()
        threading.Thread(target=lambda: (self._connect(mgr), done.set()),
                         daemon=True).start()
        self.assertTrue(started.wait(5), "the lookup never ran at all")
        self.assertTrue(done.wait(2),
                        "connect_client did not return until the name lookup "
                        "did")
        with mgr._client_lock:
            free = "u1" not in mgr._connecting
        release.set()
        self.assertTrue(free,
                        "a stalled name lookup kept the server's in-flight "
                        "reservation occupied")

    def test_a_second_connect_does_not_start_a_second_lookup(self):
        """Bounded, not merely moved. N reconnects against a stuck resolver
        used to leave N live threads."""
        mgr = self._manager()
        started, release, calls = self._stalled_resolver()
        self._connect(mgr)
        self.assertTrue(started.wait(5))
        mgr.clients.pop("u1", None)     # so the connect is not short-circuited
        self._connect(mgr)
        self.assertEqual(len(calls), 1,
                         "a reconnect started a second lookup for the same "
                         "server and address")
        release.set()

    def test_the_answer_still_lands(self):
        """The control: moving the lookup must not lose it."""
        mgr = self._manager()
        started, release, _calls = self._stalled_resolver(answer=True)
        self._connect(mgr)
        self.assertTrue(started.wait(5))
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with mgr._client_lock:
                if "u1" not in mgr._lan_probe:
                    break
            time.sleep(0.005)
        self.assertIs(mgr.server_is_local("u1"), True,
                      "the verdict never arrived after the resolver answered")


class ALateLocalityAnswerKnowsItIsLateTest(unittest.TestCase):
    """The write decision on its own, with no threads in it.

    Which connection asked is the question a moved lookup has to be able to
    answer: the first attempt checked only the uuid and the user generation,
    so a slow answer from an older connect could overwrite a newer one.
    """

    def _manager(self):
        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._client_lock = threading.RLock()
        mgr._server_on_lan = {}
        mgr._lan_probe = {}
        mgr._lan_seq = 0
        # `connection_problem` reads this one: a connect in flight is answered
        # from live state rather than from the verdict ledger.
        mgr._connecting = set()
        mgr.clients = {"u1": object()}
        return mgr

    def test_an_older_connects_answer_is_dropped(self):
        mgr = self._manager()
        with mgr._client_lock:
            mgr._lan_probe["u1"] = (7, "https://moved-here")
        mgr._finish_lan_probe("u1", "https://was-here", 6, True)
        self.assertIsNone(mgr.server_is_local("u1"),
                          "a superseded lookup wrote the verdict for an "
                          "address this server no longer has")
        mgr._finish_lan_probe("u1", "https://moved-here", 7, False)
        self.assertIs(mgr.server_is_local("u1"), False)

    def test_an_answer_for_a_server_that_went_away_is_dropped(self):
        """Stop, removal and a user switch all take the client out of the
        registry, so one check covers the three."""
        mgr = self._manager()
        mgr.clients.clear()
        with mgr._client_lock:
            mgr._lan_probe["u1"] = (1, "https://h")
        mgr._finish_lan_probe("u1", "https://h", 1, True)
        self.assertIsNone(mgr.server_is_local("u1"))

    def test_removing_a_server_forgets_everything_about_it(self):
        """Every per-uuid answer went with the credential, or it outlives the
        server it was about. A re-add does get a fresh uuid, which would hide
        that -- but it is a property of somewhere else."""
        mgr = self._manager()
        mgr._removed_uuids = set()
        mgr._connect_failures = {"u1": CONNECT_SIGNED_OUT}
        mgr._switch_lock = threading.RLock()
        mgr.credentials = [{"uuid": "u1"}]
        mgr.usernames = {}
        mgr.save_credentials = lambda: None
        # Through the real `_disconnect_client`, which touches the client it
        # removes -- a bare sentinel would take the teardown down an error
        # path and never reach the state this is about.
        mgr.clients["u1"] = FakeJellyfinClient(
            FakeAPI(answers=True), clients.CONNECTION_STATE["SignedIn"])
        with mgr._client_lock:
            mgr._server_on_lan["u1"] = True
            mgr._lan_probe["u1"] = (1, "https://h")
        mgr.remove_client("u1")
        self.assertIsNone(mgr.server_is_local("u1"))
        self.assertNotIn("u1", mgr._lan_probe)
        self.assertIsNone(mgr.connection_problem("u1"))


class WhereAServerLivesIsResolvedNotSpelledTest(unittest.TestCase):
    """The one the URL cannot answer.

    A self-hoster's own domain resolves to a LAN address at home and a
    public one from anywhere else, and the address they typed is identical
    either way. So the verdict is taken from a name lookup when the connect
    succeeds -- the moment it is both cheap (the name was resolved seconds
    ago by the connect itself) and true of where this machine actually is.
    """

    def _manager(self, resolves_to):
        self._resolve = mock.patch.object(
            clients, "resolved_host_is_private", lambda address: resolves_to)
        self._resolve.start()
        self.addCleanup(self._resolve.stop)
        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._stop_event = threading.Event()
        mgr._client_lock = threading.RLock()
        mgr._switch_lock = threading.RLock()
        mgr._connecting = set()
        mgr._removed_uuids = set()
        mgr._connect_failures = {}
        mgr._server_on_lan = {}
        mgr._lan_probe = {}
        mgr._lan_seq = 0
        mgr._user_generation = 0
        mgr.clients = {}
        mgr.usernames = {}
        mgr.credentials = []
        mgr.client_factory = lambda: FakeJellyfinClient(
            FakeAPI(answers=True), clients.CONNECTION_STATE["SignedIn"])
        mgr.setup_client = lambda client, server, do_retries=True: None
        # These are about the verdict, not about the concurrency: run the
        # locality probe inline rather than racing the scheduler for it.
        # `TheLocalityLookupDoesNotHoldTheConnectTest` uses the real thread.
        mgr._spawn = lambda target, name, args=(): target(*args)
        return mgr

    @staticmethod
    def _connect(mgr):
        return mgr.connect_client({"uuid": "u1", "Id": "s",
                                   "address": "https://iwalton.com"})

    def test_a_domain_pointing_at_the_lan_is_local(self):
        """Split horizon, which is the whole reason this is not read off the
        URL: `iwalton.com` resolving to 192.168.3.133 is a LAN server with a
        public-looking name."""
        mgr = self._manager(True)
        self.assertTrue(self._connect(mgr))
        self.assertIs(mgr.server_is_local("u1"), True)

    def test_the_same_domain_from_elsewhere_is_not(self):
        mgr = self._manager(False)
        self.assertTrue(self._connect(mgr))
        self.assertIs(mgr.server_is_local("u1"), False)

    def test_a_name_that_will_not_resolve_records_nothing(self):
        """Three answers, not two. "We asked and it is remote" and "we could
        not ask" want different fallbacks, and collapsing them would tell a
        LAN user their server is on the internet whenever DNS hiccuped."""
        mgr = self._manager(None)
        self.assertTrue(self._connect(mgr))
        self.assertIsNone(mgr.server_is_local("u1"))

    def test_a_server_nothing_has_connected_to_is_unknown(self):
        self.assertIsNone(self._manager(True).server_is_local("u1"))

    def test_moving_between_networks_changes_the_answer(self):
        """The laptop case, and why this is recorded per connect rather than
        once. It must not walk: three reconnects at home after one away
        still say local."""
        mgr = self._manager(False)
        self._connect(mgr)
        self.assertIs(mgr.server_is_local("u1"), False)
        self._resolve.stop()
        self._resolve = mock.patch.object(
            clients, "resolved_host_is_private", lambda address: True)
        self._resolve.start()
        for _ in range(3):
            mgr.clients.pop("u1", None)
            self.assertTrue(self._connect(mgr))
            self.assertIs(mgr.server_is_local("u1"), True)

    def test_the_resolved_verdict_outranks_the_spelling(self):
        """Through `server_icon`, which is where the two meet."""
        from jellyfin_mpv_shim.mpvtk_browser.components import server_icon
        spelled_remote = {"address": "https://iwalton.com"}
        self.assertEqual(server_icon(spelled_remote), "cloud")
        self.assertEqual(server_icon(dict(spelled_remote, local=True)), "lan")
        self.assertEqual(
            server_icon({"address": "http://192.168.1.5", "local": False}),
            "cloud", "a resolved verdict was overruled by the URL")

    def test_but_an_unknown_verdict_falls_back_to_it(self):
        from jellyfin_mpv_shim.mpvtk_browser.components import server_icon
        self.assertEqual(
            server_icon({"address": "http://192.168.1.5", "local": None}),
            "lan")


class TheNameLookupBehindThatVerdictTest(unittest.TestCase):
    """`utils.resolved_host_is_private`, with the resolver stubbed.

    Stubbed because the assertion is about what this makes of an answer, and
    a test that asked the real DNS would be asserting about the network the
    suite happens to run on -- green at home, red on a build box, and
    silently right for the wrong reason in both places.
    """

    @staticmethod
    def _ask(address, answers):
        """``answers`` is a list of IPs, or an exception to raise."""
        from jellyfin_mpv_shim import utils

        def fake(host, port, *a, **kw):
            if isinstance(answers, Exception):
                raise answers
            return [(None, None, None, "", (ip, 0)) for ip in answers]

        with mock.patch.object(utils.socket, "getaddrinfo", fake):
            return utils.resolved_host_is_private(address)

    def test_a_public_name_on_a_private_address_is_private(self):
        """Split horizon, at the layer that decides it."""
        self.assertIs(self._ask("https://iwalton.com", ["192.168.3.133"]),
                      True)

    def test_and_the_same_name_on_a_public_one_is_not(self):
        self.assertIs(self._ask("https://iwalton.com", ["93.184.216.34"]),
                      False)

    def test_one_private_answer_among_several_is_enough(self):
        """A dual-stack name can hand back a public IPv6 address and a LAN
        IPv4 one, and the connection being described may have used either.
        Order must not decide it."""
        for ips in (["2606:4700::1", "192.168.3.133"],
                    ["192.168.3.133", "2606:4700::1"]):
            with self.subTest(ips=ips):
                self.assertIs(self._ask("https://h", ips), True)

    def test_a_resolver_that_fails_says_it_does_not_know(self):
        self.assertIsNone(self._ask("https://h", OSError("no such host")))

    def test_a_name_that_is_not_a_name_says_the_same(self):
        for address in (None, "", "::::"):
            with self.subTest(address=address):
                self.assertIsNone(self._ask(address, ["192.168.1.1"]))

    def test_it_does_not_reach_out_to_a_third_party(self):
        """`is_local_domain` asks checkip.amazonaws.com to settle hairpin
        NAT, because it is deciding a bitrate. This decides an icon, and an
        outbound request to somebody else's service is not a thing to do for
        one -- nor on the connect path, where it would be per server."""
        from jellyfin_mpv_shim import utils

        def boom(*a, **kw):
            raise AssertionError("made an HTTP request to classify a server")

        with mock.patch.object(utils.requests, "get", boom):
            self.assertIs(self._ask("https://h", ["10.0.0.4"]), True)
            self.assertIs(self._ask("https://h", ["93.184.216.34"]), False)


class AVerdictDoesNotOutliveItsProfileTest(unittest.TestCase):
    """CR9's live route.

    `_connect_failures` is keyed by credential uuid and nothing clears it on a
    profile switch, while `_removed_uuids` -- two lines away, under the same
    lock -- is cleared for exactly this reason. Switch away and back and the
    profile you return to has an empty registry with `connect_all` still in
    flight, so every one of its servers reports the verdict from before you
    left: the Servers tab shows "Signed out", with Sign In Again emphasised,
    for a server nothing has tried yet.

    The route the reviewer named is the dead one. Remove-and-re-add mints a
    fresh `uuid.uuid4()`, because the `force_unique` branch that reused the
    server's own Id as the uuid is passed by nothing -- which is the same
    dead branch this commit removes.
    """

    def _manager(self, users, active="a"):
        from jellyfin_mpv_shim import users as users_mod

        um = users_mod.UserManager()
        um.users = users
        um.active_id = active
        um.save = lambda: None
        original = clients.userManager
        clients.userManager = um
        self.addCleanup(setattr, clients, "userManager", original)

        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._stop_event = threading.Event()
        mgr._client_lock = threading.RLock()
        mgr._switch_lock = threading.RLock()
        mgr._switching = threading.Event()
        mgr._connecting = set()
        mgr._removed_uuids = set()
        mgr._connect_failures = {}
        mgr._server_on_lan = {}
        mgr._lan_probe = {}
        mgr._lan_seq = 0
        mgr._user_generation = 0
        mgr.clients = {}
        mgr.usernames = {}
        mgr.save_credentials = lambda: None
        # The connect is the subject of other tests; here what matters is the
        # state the switch leaves *before* one lands, which is the window the
        # Servers tab draws in.
        mgr.connected = []
        mgr.connect_all = lambda: mgr.connected.append(um.active_id)
        mgr._adopt_active_user()
        return mgr

    def _users(self):
        # `device_id` because a real profile has one and `_adopt_active_user`
        # reads it; `name` for the device name it derives.
        return [{"id": "a", "name": "A", "device_id": "dev-a",
                 "credentials": [
                     {"uuid": "u1", "Id": "s1", "address": "http://h"}]},
                {"id": "b", "name": "B", "device_id": "dev-b",
                 "credentials": [
                     {"uuid": "u2", "Id": "s2", "address": "http://k"}]}]

    def test_the_profile_you_come_back_to_has_no_verdict_waiting(self):
        mgr = self._manager(self._users())
        mgr._connect_failures["u1"] = CONNECT_SIGNED_OUT
        self.assertTrue(mgr.switch_user("b"))
        self.assertTrue(mgr.switch_user("a"))
        self.assertIsNone(
            mgr.connection_problem("u1"),
            "a server nothing has tried yet reports the verdict it had "
            "before the profile switch")

    def test_removing_one_server_leaves_the_others_answers_alone(self):
        """The scoped half of `_forget_server_state`, which the switch's
        clear-everything would satisfy without. Two servers, the other one
        inserted first, because a helper that ignored its argument would pass
        against a fixture holding only the server being removed."""
        mgr = self._manager(self._users())
        mgr.credentials = [{"uuid": "keep", "Id": "s9", "address": "http://o"},
                           {"uuid": "u1", "Id": "s1", "address": "http://h"}]
        mgr._connect_failures.update({"keep": CONNECT_SIGNED_OUT,
                                      "u1": CONNECT_UNREACHABLE})
        mgr._server_on_lan.update({"keep": True, "u1": False})
        mgr._disconnect_client = lambda uuid=None, server=None, \
            expected_client=None: False
        mgr.remove_client("u1")
        self.assertEqual(mgr.connection_problem("keep"), CONNECT_SIGNED_OUT)
        self.assertIs(mgr.server_is_local("keep"), True)
        self.assertIsNone(mgr.connection_problem("u1"))

    def test_and_the_other_profiles_locality_answer_goes_too(self):
        """The same lifetime, for the same reason: `_server_on_lan` is keyed
        by uuid and read by the row that draws the home/away icon, and an
        in-flight `_lan_probe` entry is what de-duplicates the next lookup."""
        mgr = self._manager(self._users())
        mgr._server_on_lan["u1"] = True
        mgr._lan_probe["u1"] = (1, "http://h")
        mgr.switch_user("b")
        self.assertEqual(mgr._server_on_lan, {})
        self.assertEqual(mgr._lan_probe, {})

    def test_a_connect_that_lands_after_the_switch_still_records_its_verdict(self):
        """The control. Clearing on the switch must not be mistaken for
        clearing whenever anything happens -- the ledger is what tells the
        user to type a password rather than press Retry, and a switch does
        not make a signed-out server signed in."""
        mgr = self._manager(self._users())
        mgr.switch_user("b")
        mgr._connect_failures["u2"] = CONNECT_SIGNED_OUT
        self.assertEqual(mgr.connection_problem("u2"), CONNECT_SIGNED_OUT)


class TheCliSignsBackInWithoutChangingTheIdentityTest(unittest.TestCase):
    """CX4. `--username/--password` against a server already saved was the
    last remove-then-add in the tree.

    `_update_account` deleted the credential and saved that, *then* tried the
    password: a typo destroyed a working login, and a success minted a fresh
    uuid -- the key the download catalog's `server_uuid` column and the
    auto-download allow-list are written in. The browser has done this properly
    since `reauthenticate` existed, so this was the N-1 site of a rule the
    repository already had.

    Reuses the re-auth fixture, because "the CLI does what the browser does" is
    the whole claim.
    """

    _manager = ReauthenticationKeepsTheIdentityTest._manager

    def test_a_wrong_password_leaves_the_saved_login_alone(self):
        mgr = self._manager(token=False)
        self.assertFalse(mgr._update_account("http://h:8096", "izzie", "typo"))
        self.assertEqual([c["uuid"] for c in mgr.credentials], ["keep-me"],
                         "a mistyped password deleted the working credential")
        self.assertEqual(mgr.credentials[0].get("address"), "http://h:8096")

    def test_a_successful_update_keeps_the_uuid(self):
        mgr = self._manager()
        self.assertTrue(mgr._update_account("http://h:8096", "izzie", "pw"))
        self.assertEqual([c["uuid"] for c in mgr.credentials], ["keep-me"],
                         "the CLI re-login orphaned every download from this "
                         "server")

    def test_an_address_that_answers_as_another_server_is_refused(self):
        """What routing through `reauthenticate` brings with it: the identity
        check happens before the password goes out, so a server we are going to
        refuse is never handed the user's credentials."""
        mgr = self._manager()
        self.auth.server_id = "s2"
        self.assertFalse(mgr._update_account("http://h:8096", "izzie", "pw"))
        self.assertEqual(self.auth.logged_in, [])
        self.assertEqual([c.get("Id") for c in mgr.credentials], ["s1"])

    def test_an_account_we_do_not_have_is_not_an_update(self):
        """The control. `cli_connect` only calls this when it found a
        credential itself, so this branch is the defensive one -- and it has to
        stay False, or an account nobody has would be reported as updated."""
        mgr = self._manager()
        self.assertFalse(mgr._update_account("http://elsewhere", "izzie", "pw"))


class AClientWeDoNotKeepIsStoppedTest(unittest.TestCase):
    """CR11. Every login-ish method builds a `JellyfinClient` to get a token,
    and the client that ends up in the registry is a **different** one:
    `_finalize_login` finishes by calling `connect_client`, which builds its
    own. So the one built here is never the one kept, on any path -- and
    dropping it leaks its `requests` session and whatever the exchange started.

    These are the paths a user *retries*: a wrong address, a mistyped password,
    a Quick Connect they walked away from. `_finalize_login`'s own early return
    used to reason that there was "nothing to tear down"; that was the belief,
    and it was wrong about the session.
    """

    def _manager(self, token=True, server_id="s1", qc_enabled=True,
                 connects=False):
        built = self.built = []

        class API:
            @staticmethod
            def quick_connect_enabled(address, session):
                return qc_enabled

            @staticmethod
            def quick_connect_initiate(address, session):
                return {"Secret": "sec", "Code": "ABC123"}

            @staticmethod
            def quick_connect_state(address, secret, session):
                return {"Authenticated": False}

            @staticmethod
            def get_public_info(address, session):
                return None                    # unreachable, for the verdict

        class Auth:
            def __init__(self):
                self.API = API()
                self.session = object()
                self.credentials = self
                self.logged_in = []

            def get_credentials(self):
                return {"Servers": [{"Id": server_id,
                                     "address": "http://h:8096",
                                     "Name": "Home"}]}

            def connect_to_address(self, address):
                return None

            def login(self, address, username, password):
                self.logged_in.append(password)
                return {"AccessToken": "t"} if token else {}

            def login_with_quick_connect(self, address, secret):
                return ({"AccessToken": "t", "User": {"Name": "izzie"}}
                        if token else {})

        class Client:
            def __init__(self):
                self.auth = Auth()
                self.stopped = 0
                built.append(self)

            def stop(self):
                self.stopped += 1

            def authenticate(self, creds, discover=False):
                state = "SignedIn" if connects else "Unavailable"
                return {"State": clients.CONNECTION_STATE[state]}

        mgr = clients.ClientManager.__new__(clients.ClientManager)
        mgr._stop_event = threading.Event()
        mgr._client_lock = threading.RLock()
        mgr._switch_lock = threading.RLock()
        mgr._switching = threading.Event()
        mgr._connecting = set()
        mgr._removed_uuids = set()
        mgr._connect_failures = {}
        mgr._server_on_lan = {}
        mgr._lan_probe = {}
        mgr._lan_seq = 0
        mgr._user_generation = 0
        mgr.clients = {}
        mgr.usernames = {}
        mgr.credentials = [{"uuid": "keep-me", "Id": "s1",
                            "address": "http://h:8096", "username": "izzie"}]
        mgr.save_credentials = lambda: None
        mgr.setup_client = lambda client, server, do_retries=True: None
        mgr._spawn = lambda target, name, args=(): None
        mgr.client_factory = Client
        # The registry's client comes from here, which is the whole reason the
        # one built above is disposable. Stubbed so these tests are about the
        # tear-down and not about the connect.
        mgr.connect_client = lambda server, do_retries=True: True
        um = mock.patch.object(clients, "userManager")
        self.um = um.start()
        self.um.active_id = "local"
        self.addCleanup(um.stop)
        return mgr

    def _only_client(self):
        self.assertEqual(len(self.built), 1, "built %d clients"
                         % len(self.built))
        return self.built[0]

    def test_a_refused_password_on_a_new_server(self):
        mgr = self._manager(token=False)
        self.assertFalse(mgr.login("http://h:8096", "izzie", "typo"))
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_successful_login_too(self):
        """The registered client is `connect_client`'s, so this one is spent
        the moment its token has been read out of it."""
        mgr = self._manager()
        self.assertTrue(mgr.login("http://h:8096", "izzie", "pw"))
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_re_auth_refused_for_naming_another_server(self):
        mgr = self._manager(server_id="s2")
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "pw"),
                         (False, REAUTH_WRONG_SERVER))
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_re_auth_with_the_wrong_password(self):
        mgr = self._manager(token=False)
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "typo"),
                         (False, None))
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_successful_re_auth(self):
        mgr = self._manager()
        self.assertEqual(mgr.reauthenticate("keep-me", "izzie", "pw"),
                         (True, None))
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_quick_connect_the_server_will_not_start(self):
        """The client is built before the two questions that can refuse, and
        the caller never sees it -- the refusal is an exception."""
        mgr = self._manager(qc_enabled=False)
        with self.assertRaises(clients.QuickConnectError):
            mgr.quick_connect_initiate("http://h:8096")
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_quick_connect_the_user_walked_away_from(self):
        mgr = self._manager()
        client, secret, code = mgr.quick_connect_initiate("http://h:8096")
        self.assertFalse(mgr.quick_connect_wait(client, secret,
                                                should_cancel=lambda: True))
        self.assertEqual(client.stopped, 1)

    def test_a_quick_connect_re_auth_refused_for_naming_another_server(self):
        mgr = self._manager(server_id="s2")
        shown = []
        self.assertEqual(
            mgr.reauthenticate_with_quick_connect("keep-me",
                                                  code_callback=shown.append),
            (False, REAUTH_WRONG_SERVER))
        self.assertEqual(shown, [])
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_connect_that_did_not_sign_in(self):
        """`connect_client`'s own client, which it stops on the
        already-superseded path and did not stop here."""
        mgr = self._manager()
        del mgr.connect_client            # the real one, this time
        self.assertFalse(mgr.connect_client(
            {"uuid": "u1", "address": "http://h:8096", "Id": "s1"}))
        self.assertEqual(mgr.connection_problem("u1"), CONNECT_UNREACHABLE)
        self.assertEqual(self._only_client().stopped, 1)

    def test_a_client_that_was_registered_is_not_stopped(self):
        """The control, and the one that would make every assertion above
        vacuous if it failed: the client `connect_client` keeps must stay
        alive."""
        mgr = self._manager(connects=True)
        del mgr.connect_client
        self.assertTrue(mgr.connect_client(
            {"uuid": "u1", "address": "http://h:8096", "Id": "s1"}))
        self.assertEqual(self._only_client().stopped, 0)
        self.assertIn("u1", mgr.clients)
