"""What each kind of user account does to login and to the library.

stdjflib ships twelve accounts, each one reaching "a client path that is
otherwise tedious to set up by hand" — and the shim has never been run
against any of them by anything automatic. These are contract tests: no mpv,
no window, just the seam the browser is handed (`LibrarySource`) and the login
path above it.

**Two of the gaps these tests turned up are now closed** (`user_policy.py`)
and asserted below: SyncPlay is not offered to a user the server refuses it
to, and the Record affordances are not offered without
`EnableLiveTvManagement`. `EnableContentDownloading` is still unread, and
Live TV *browsing* was already gated by whether the server put a Live TV
view in `/Views` (`repository.get_libraries`). See
`docs/PERMISSION_GAPS.md`.

Two things that are *not* tested here, because measurement said they are not
true (both in `docs/E2E_PLAN.md`):

* `qa-noplayback` can play, and no client can change that. Jellyfin's video
  endpoints are `AllowAnonymous`, so the item id is effectively the
  credential; `EnableMediaPlayback: False` does not stop the server serving a
  `static=true` URL, and there is no refusal for the client to handle.
* `qa-onesession` does not evict. The server refuses the *second* login with
  403 and leaves the first working, which is the opposite way round from the
  account's description.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402


@_e2e.require_server
class RestrictedLibrariesTest(unittest.TestCase):
    """`qa-restricted` — "the rest must be *absent*, not merely unplayable".

    Absence is the whole point: a client that lists a library it cannot open
    has failed this account even though nothing errored.
    """

    ALLOWED = {"Movies", "Shows"}

    def setUp(self):
        self.session = _e2e.Session("qa-restricted")
        self.addCleanup(self.session.stop)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)

    def test_only_the_permitted_libraries_are_visible(self):
        names = {lib["Name"] for lib in
                 self.source.get_libraries(_e2e.SOURCE_UUID)}
        # Live TV is a view rather than a library and is granted separately,
        # so it is allowed to be here and is not the subject of this test.
        names.discard("Live TV")
        self.assertEqual(
            names, self.ALLOWED,
            "a restricted user sees libraries they have no access to")

    def test_the_home_screen_offers_nothing_from_a_forbidden_library(self):
        """The rows are built from the libraries, so this is the same rule one
        layer up — and the layer a user actually looks at."""
        allowed_ids = {lib["Id"] for lib
                       in self.source.get_libraries(_e2e.SOURCE_UUID)}
        rows = self.source.get_home_rows(_e2e.SOURCE_UUID)
        self.assertTrue(rows, "a restricted user got no home rows at all")
        for row in rows:
            parent = row.get("parent_id")
            if parent is not None:
                self.assertIn(
                    parent, allowed_ids,
                    "home row %r is built from a library this user cannot "
                    "see" % row.get("title"))

    def test_a_full_user_sees_more(self):
        """Guards the test above from passing because everything is empty."""
        other = _e2e.Session("qa-user")
        self.addCleanup(other.stop)
        source = other.library_source()
        self.addCleanup(source.stop)
        self.assertGreater(
            len(source.get_libraries(_e2e.SOURCE_UUID)),
            len(self.source.get_libraries(_e2e.SOURCE_UUID)),
            "qa-restricted sees as much as qa-user, so the restriction is "
            "not in force and the assertions above prove nothing")


@_e2e.require_server
class LiveTvAccessTest(unittest.TestCase):
    """`qa-kid` has Live TV revoked, which is the gate `has_live_tv` reads.

    `repository.get_libraries` derives it from the presence of the Live TV
    view rather than fetching the policy, on the reasoning that the server
    adds that view only when the user may use Live TV *and* a tuner exists.
    This is that reasoning, checked against a server where exactly one of two
    users has the right.
    """

    def _source(self, account):
        session = _e2e.Session(account)
        self.addCleanup(session.stop)
        source = session.library_source()
        self.addCleanup(source.stop)
        source.get_libraries(_e2e.SOURCE_UUID)      # what populates the flag
        return source

    def test_a_user_with_live_tv_is_offered_it(self):
        self.assertTrue(
            self._source("qa-user").has_live_tv(_e2e.SOURCE_UUID),
            "Live TV is configured on this server but qa-user was not "
            "offered it")

    def test_a_user_without_live_tv_is_not_offered_it(self):
        source = self._source("qa-kid")
        self.assertFalse(
            source.has_live_tv(_e2e.SOURCE_UUID),
            "qa-kid has EnableLiveTvAccess False but was offered Live TV")
        self.assertNotIn(
            "Live TV",
            {lib["Name"] for lib in source.get_libraries(_e2e.SOURCE_UUID)})


@_e2e.require_server
class LoginTest(unittest.TestCase):
    """The login paths that are awkward to reach by hand."""

    def test_an_account_with_no_password_can_sign_in(self):
        """`qa-nopassword` — "a login flow that assumes a password field is
        filled breaks here". Common in home setups."""
        session = _e2e.Session("qa-nopassword", password="")
        self.addCleanup(session.stop)
        self.assertTrue(session.token)
        self.assertTrue(session.api.get_views()["Items"],
                        "signed in with no password but cannot browse")

    def test_a_disabled_account_is_refused(self):
        """`qa-disabled` — must fail cleanly rather than hang or land the
        client half signed in. The server answers 403."""
        self.assertTrue(_e2e.login_refused("qa-disabled"))

    def test_a_wrong_password_is_refused(self):
        self.assertTrue(_e2e.login_refused("qa-user", "not-the-password"))

    def test_a_hidden_account_is_absent_from_the_list_but_can_sign_in(self):
        """`qa-hidden` — "a client that only offers the public user list
        cannot reach this account at all", which is why the shim asks for a
        username rather than only offering the list."""
        public = _e2e.public_users()
        self.assertTrue(public, "no public users at all")
        self.assertIn("qa-user", public,
                      "the public list is empty of accounts that should be in "
                      "it, so its absence proves nothing below")
        self.assertNotIn("qa-hidden", public)

        session = _e2e.Session("qa-hidden")
        self.addCleanup(session.stop)
        self.assertTrue(session.token,
                        "a hidden account could not sign in by name")

    def test_a_second_session_is_refused_and_the_first_survives(self):
        """`qa-onesession` caps concurrent sessions at one.

        The account's description says the second login must evict the first.
        Measured, the server does the opposite: it refuses the newcomer with
        403 and leaves the incumbent working. Either way the client must not
        end up half signed in, which is what this pins.

        The cap counts what the server still believes is connected, so this
        starts by deleting the account's Device records as admin. Without
        that, a session left behind by a crashed run refuses the *first*
        login here and the failure looks like the cap working rather than
        like litter — which is exactly how it first failed.
        """
        admin = _e2e.Session("qa-admin")
        self.addCleanup(admin.stop)
        admin.purge_devices("qa-onesession")
        self.addCleanup(admin.purge_devices, "qa-onesession")

        first = _e2e.Session("qa-onesession", device_id="jms-e2e-1session-a")
        self.addCleanup(first.stop)
        self.assertTrue(first.api.get_views()["Items"])

        # A genuinely different device, which is the scenario the cap is about.
        self.assertTrue(
            _e2e.login_refused("qa-onesession",
                               device_id="jms-e2e-1session-b"),
            "a second concurrent session was allowed, so this server does "
            "not cap them and the assertion below proves nothing")
        self.assertTrue(
            first.api.get_views()["Items"],
            "the incumbent session stopped working when a second login was "
            "refused")


@_e2e.require_server
class SyncPlayPermissionTest(unittest.TestCase):
    """`qa-nosyncplay` — "SyncPlay refused, so the client's SyncPlay entry
    points must go".

    The policy is read from the server here, not fabricated: the point of
    doing this end to end is that `SyncPlayAccess` really does come back on
    `/Users/Me` for a real account, spelled the way the code expects. A unit
    test with a hand-written policy dict proves the branch, not the field
    name.
    """

    def _client(self, account):
        session = _e2e.Session(account)
        self.addCleanup(session.stop)
        source = session.library_source()
        self.addCleanup(source.stop)
        return source

    def test_the_refused_account_is_refused(self):
        from jellyfin_mpv_shim import user_policy

        source = self._client("qa-nosyncplay")
        access = source.syncplay_access(_e2e.SOURCE_UUID)
        self.assertEqual(
            access, user_policy.NO_SYNCPLAY,
            "qa-nosyncplay came back as %r — either the account no longer "
            "has SyncPlay revoked, or the field is not where the client "
            "looks for it" % access)

    def test_an_ordinary_account_still_has_it(self):
        """The control. Without this the test above passes just as well if
        the client answers "no" to everybody."""
        from jellyfin_mpv_shim import user_policy

        source = self._client("qa-user")
        self.assertEqual(source.syncplay_access(_e2e.SOURCE_UUID),
                         user_policy.CREATE_AND_JOIN)


@_e2e.require_server
class LiveTvManagementPermissionTest(unittest.TestCase):
    """`EnableLiveTvManagement` is a *third* Live TV permission, separate
    from the access one — and the one that made this suite unable to
    schedule a timer as any account on the server until stdjflib was fixed.

    A user can therefore browse the guide perfectly well and have every
    Record button answer 403.
    """

    def _source(self, account):
        session = _e2e.Session(account)
        self.addCleanup(session.stop)
        source = session.library_source()
        self.addCleanup(source.stop)
        return source

    def test_the_account_that_may_record_may(self):
        self.assertTrue(
            self._source("qa-user").can_manage_live_tv(_e2e.SOURCE_UUID),
            "qa-user was granted EnableLiveTvManagement by stdjflib and the "
            "client does not see it")

    def test_an_account_without_it_is_refused(self):
        """`qa-restricted` is an ordinary account that was never granted it.

        Which is the default for every account created on a modern server:
        `AddDefaultPermissions` leaves it False, and only an install
        migrated from an older one has it on by default.
        """
        source = self._source("qa-restricted")
        self.assertFalse(
            source.can_manage_live_tv(_e2e.SOURCE_UUID),
            "qa-restricted can manage Live TV recordings, so this server no "
            "longer has an account without the permission and the gate is "
            "untested")


if __name__ == "__main__":
    unittest.main()
