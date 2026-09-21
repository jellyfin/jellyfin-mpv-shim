"""Refresh metadata (#764) — the browser's first administrator-only action.

`POST /Items/{id}/Refresh` has no per-library grant and no user-level
permission that opens it: administrators only, which is where jellyfin-web
draws the line too. So this is the first gate here that asks the *account*
rather than the item.

Three properties, and the middle one is the uncomfortable one:

* **it is gated on `IsAdministrator`**, through the same
  policy → source → actions chain every other permission uses;
* **it fails open**, like every gate in `user_policy` -- and unlike the
  others, failing open here shows a button an ordinary account's server will
  certainly refuse. Accepted because `IsAdministrator` is in every policy on
  every server that has the endpoint, so the branch needs a failed fetch *and*
  a non-admin; written down in `docs/PERMISSION_GAPS.md` rather than left for
  somebody to find;
* **it asks for a full refresh that replaces nothing.** The strongest option
  discards everything the server holds, hand-edited fields included, and a
  context menu with no confirmation is not where that belongs.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest

from jellyfin_mpv_shim import user_policy
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import FakeController, FakeSource, _SyncPool

MOVIE = {"Id": "x", "Type": "Movie", "Name": "A Film"}


class _Source(FakeSource):
    """A source that answers the admin question, because the real one does.

    `FakeSource` inherits `can_refresh_metadata` from nothing, so without this
    the gate would take its "no source method" path and every test below would
    pass against a client with no gate at all.
    """

    def __init__(self, admin=True):
        super().__init__()
        self.admin = admin
        self.refreshed = []

    def can_refresh_metadata(self, server_uuid):
        return self.admin


class RefreshMenuEntryTest(unittest.TestCase):
    def _browser(self, admin=True, offline=False):
        b = MpvtkBrowser(app=None, source=_Source(admin),
                         controller=FakeController())
        b.nav_stack = [{"kind": "grid", "server": "srv"}]
        b._pool = _SyncPool()
        b._offline = offline
        return b

    def _entries(self, item, **kw):
        return [e[2] for e in self._browser(**kw)._tile_menu_entries(item)]

    def test_an_admin_gets_it(self):
        self.assertIn("refresh", self._entries(MOVIE))

    def test_an_ordinary_user_does_not(self):
        self.assertNotIn("refresh", self._entries(MOVIE, admin=False))

    def test_offline_does_not_offer_it(self):
        """There is no server to re-scan, and the catalog's copy of the DTO is
        not something a refresh reaches."""
        self.assertNotIn("refresh", self._entries(MOVIE, offline=True))

    def test_a_container_gets_it(self):
        """The case it exists for: an episode whose metadata never landed is
        usually fixed from the series, and the call is recursive."""
        for kind in ("Series", "Season", "MusicAlbum", "BoxSet", "Book"):
            self.assertIn("refresh", self._entries({"Id": "x", "Type": kind}),
                          "%s offers no refresh" % kind)

    def test_live_tv_does_not(self):
        """A channel and a programme are not items with metadata to re-read,
        and web excludes them for the same reason."""
        for kind in ("TvChannel", "Program"):
            self.assertNotIn("refresh",
                             self._entries({"Id": "x", "Type": kind}),
                             "%s offers a refresh" % kind)

    def test_a_recording_in_progress_does_not(self):
        """The file is still being written; asking the server to re-read a
        moving target is not a useful thing to offer."""
        self.assertNotIn("refresh", self._entries(
            {"Id": "x", "Type": "Episode", "TimerId": "t1",
             "Status": "InProgress"}))

    def test_it_sits_above_delete(self):
        """Not beside it. This is the entry an administrator reaches for when
        something arrived with no metadata, and it must not be one row away
        from the one that destroys the file."""
        entries = self._entries({**MOVIE, "CanDelete": True})

        self.assertLess(entries.index("refresh"), entries.index("deleteitem"))


class TheGateFailsOpenTest(unittest.TestCase):
    """The exception this tree tolerates, asserted so that changing it is a
    decision rather than an accident."""

    def test_an_administrator_is_allowed(self):
        client = _FakeClient({"IsAdministrator": True})

        self.assertTrue(user_policy.may_refresh_metadata(client))

    def test_a_non_administrator_is_not(self):
        client = _FakeClient({"IsAdministrator": False})

        self.assertFalse(user_policy.may_refresh_metadata(client))

    def test_a_policy_we_could_not_read_is_allowed(self):
        """Fail open: the same rule the whole module states. Unreachable in
        practice -- `IsAdministrator` is in every policy on every server with
        this endpoint -- and the cost of the other choice is an administrator
        whose fetch failed losing the entry."""
        self.assertTrue(user_policy.may_refresh_metadata(_FakeClient(None)))
        self.assertTrue(user_policy.may_refresh_metadata(None))


class _FakeClient:
    """A client whose `Users/Me` answers with this policy, or fails.

    The field this models is the one the test is named after: `policy_for`
    caches on the client and treats an exception as `{}`, so a stand-in that
    always answered a dict could not reach the fail-open branch at all.
    """

    def __init__(self, policy):
        self._policy = policy

        class _JF:
            @staticmethod
            def get_user():
                if policy is None:
                    raise RuntimeError("no answer")
                return {"Policy": dict(policy)}

        self.jellyfin = _JF()


class RefreshCallTest(unittest.TestCase):
    def test_the_action_asks_the_gateway(self):
        calls = []

        class _Ctl:
            @staticmethod
            def refresh_item(server, item_id):
                calls.append((server, item_id))

        b = MpvtkBrowser(app=None, source=_Source(), controller=_Ctl())
        b.nav_stack = [{"kind": "grid", "server": "srv"}]
        b._pool = _SyncPool()

        b._actions.refresh_metadata(MOVIE, "srv")

        self.assertEqual([("srv", "x")], calls)

    def test_it_replaces_nothing(self):
        """The parameters, because they are the difference between "fetch what
        is missing" and "throw away everything the server has"."""
        from jellyfin_mpv_shim.mpvtk_browser.gateway.editing import EditingMixin

        sent = {}

        class _JF:
            @staticmethod
            def items(path, action="GET", params=None, **kw):
                sent.update({"path": path, "action": action,
                             "params": dict(params or {})})

        gateway = EditingMixin.__new__(EditingMixin)
        gateway._edit = lambda _server, fn: fn(_JF())

        gateway.refresh_item("srv", "x")

        self.assertEqual("/x/Refresh", sent["path"])
        self.assertEqual("POST", sent["action"])
        self.assertIs(False, sent["params"]["ReplaceAllMetadata"])
        self.assertIs(False, sent["params"]["ReplaceAllImages"])
        self.assertEqual("FullRefresh", sent["params"]["MetadataRefreshMode"])
        # `Recursive` is not a parameter of this endpoint on either supported
        # major -- `ItemRefreshController.RefreshItem` takes the four above
        # plus `regenerateTrickplay`, at 12.0 and at 10.11.0 -- and Jellyfin
        # answers an unrecognised name exactly as it answers its absence. So
        # sending it proved nothing and documented a mechanism that does not
        # exist. The recursion happens anyway; see `refresh_item`.
        self.assertNotIn("Recursive", sent["params"])


if __name__ == "__main__":
    unittest.main()
