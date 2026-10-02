"""Where a page turn's position goes, and what happens when the server is
not there to take it.

A downloaded book is the one thing that can be *read* with the server away,
and until this existed an offline page turn was written nowhere at all: the
reader called `set_position`, that failed with no client, and the number was
dropped. Reopening the book offline started it again from page one, and
nothing was ever sent on reconnect.

Tested against a real catalog rather than a stand-in, because two of the
three rules here are rules about the catalog's own semantics -- verbatim
locally, advance-only on the wire -- and a fake would have been written to
agree with whichever one I had in mind.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the gateway reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.gateway import deps  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.gateway.userdata import (  # noqa: E402
    UserDataMixin)
from jellyfin_mpv_shim.users import userManager  # noqa: E402
from jellyfin_mpv_shim.sync.db import (NO_ACTOR, COLUMNS,  # noqa: E402
                                       STATUS_COMPLETE,
                                       SyncDB)

TICKS = 10_000_000


#: The Jellyfin ServerId these fixtures' rows belong to. Set deliberately:
#: a downloads row with no content server is an ORPHAN, and the orphan path
#: is a distinct contract (docs/offline-sync.md section 1).
#: A fixture that omits this silently tests the orphan path under another
#: name -- which is what every row in this file used to do.
CONTENT_SERVER = "SRV"


def row(item_id, position=0):
    record = {c: None for c in COLUMNS}
    record["item_id"] = item_id
    record["status"] = STATUS_COMPLETE
    record["type"] = "Book"
    record["name"] = "A Novel"
    record["file_path"] = "%s/book.epub" % item_id
    record["content_server_id"] = CONTENT_SERVER
    record["item_json"] = json.dumps({"Id": item_id, "Type": "Book",
                                      "ServerId": CONTENT_SERVER})
    record["userdata_json"] = json.dumps(
        {"PlaybackPositionTicks": position} if position else {})
    return record


class FakeJellyfin:
    def __init__(self, ok=True):
        self.ok = ok
        self.written = []

    def update_userdata_for_item(self, item_id, payload):
        if not self.ok:
            raise RuntimeError("no server")
        self.written.append((item_id, payload))


class FakeClient:
    def __init__(self, ok=True):
        self.jellyfin = FakeJellyfin(ok)


class Gateway(UserDataMixin):
    """The mixin on its own. It reaches deps.clientManager and
    syncManager.db and nothing else, which is what makes this possible."""

    @staticmethod
    def _act(fn):
        raise AssertionError("no player action belongs on this path")


class ReadingPositionTest(unittest.TestCase):
    ITEM = "bk1"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = SyncDB(os.path.join(self.tmp.name, "catalog.db"))
        self.addCleanup(self.db.close)
        self.db.upsert(row(self.ITEM))
        # A credential behind the "srv" login, so the reader's cursor has
        # somebody to belong to. A book's place is a person's, and the
        # replay queue refuses an entry nobody can be named for -- there is
        # no account to send it as, so it could never be drained.
        for attr, value in (
                ("users", [{"id": "local", "credentials": [
                    {"uuid": "srv", "Id": self.SERVER_ID,
                     "UserId": self.USER_ID}]}]),
                ("active_id", "local")):
            patch = mock.patch.object(userManager, attr, value)
            self.addCleanup(patch.stop)
            patch.start()
        # Seed where the reader now reads. `row()` still spells the cursor
        # as `userdata_json` because that is the column an older build
        # wrote; the live one is the per-actor table.
        self.db.set_reading_position(self.ITEM, 0,
                                     actor=(CONTENT_SERVER, self.USER_ID))

        from jellyfin_mpv_shim.sync import manager as manager_module

        self._saved_db = getattr(manager_module.syncManager, "db", None)
        manager_module.syncManager.db = self.db
        self.addCleanup(setattr, manager_module.syncManager, "db",
                        self._saved_db)

        self._saved_clients = deps.clientManager
        self.addCleanup(setattr, deps, "clientManager", self._saved_clients)
        self.gateway = Gateway()

    def online(self, ok=True):
        class Manager:
            clients = {"srv": FakeClient(ok)}

            @staticmethod
            def get_clients():
                return dict(Manager.clients)
        deps.clientManager = Manager()
        return Manager.clients["srv"].jellyfin

    def offline(self):
        class Manager:
            clients = {}
        deps.clientManager = Manager()

    SERVER_ID = CONTENT_SERVER
    USER_ID = "U1"

    def stored(self, item_id=None):
        """The reading cursor as the catalog holds it for this reader.

        Moved out of `downloads.userdata_json` into the per-actor table: a
        book's place in it belongs to the person reading, and two accounts
        sharing one downloaded book used to share one bookmark. These
        fixtures name no credential, so the actor is the unattributed
        sentinel."""
        return self.db.userdata(item_id or self.ITEM,
                                actor=(CONTENT_SERVER, self.USER_ID))["position_ticks"] or None

    # -- online ------------------------------------------------------------

    def test_online_the_server_gets_it(self):
        api = self.online()
        self.assertTrue(
            self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS))
        self.assertEqual(api.written,
                         [(self.ITEM, {"PlaybackPositionTicks": 5 * TICKS})])

    def test_online_the_catalog_gets_it_too(self):
        """So the Downloads screen and a later offline re-open agree with
        what the server was told."""
        self.online()
        self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS)
        self.assertEqual(self.stored(), 5 * TICKS)

    def test_a_successful_write_queues_nothing(self):
        """The queue is advance-only, so an entry left behind after a write
        the server took would be replayed later and undo a page turn that
        went backwards."""
        self.online()
        self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS)
        self.assertEqual(self.db.list_playstate(), [])

    # -- offline -----------------------------------------------------------

    def test_offline_the_position_survives_in_the_catalog(self):
        """The bug this exists for: an offline page turn was written
        nowhere, so reopening the book offline started it from page one."""
        self.offline()
        self.assertFalse(
            self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS))
        self.assertEqual(self.stored(), 5 * TICKS)

    def test_offline_the_position_is_queued_for_the_server(self):
        self.offline()
        self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS)
        pending = self.db.list_playstate()
        self.assertEqual([(p["item_id"], p["position_ticks"])
                          for p in pending], [(self.ITEM, 5 * TICKS)])

    def test_a_refusing_server_queues_as_well_as_an_absent_one(self):
        """A 500 or a dropped connection is offline as far as this is
        concerned, and it is the case a `clients` check cannot see."""
        self.online(ok=False)
        self.assertFalse(
            self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS))
        self.assertTrue(self.db.list_playstate())

    def test_nothing_is_queued_for_a_book_that_is_not_downloaded(self):
        """The queue is keyed on the catalog, so an entry for something
        with no row would sit there for good.

        **The one row in this module that is deliberately not homed**, and it
        is not an orphan -- it is the NO_ROW case. `_server_of` answers
        `NO_ACTOR` both for a row whose content server is unknown and for no
        row at all, so the local write below still lands, filed under
        `(@none, <this reader>)`, creating userdata for an item the catalog
        has never heard of. Nothing here asserts that today; it is measured
        and handed to batch 2, whose contract requires the two cases to stop
        collapsing. docs/offline-sync.md section 1.
        """
        self.offline()
        self.gateway.record_reading_position("srv", "not-downloaded", TICKS)
        self.assertEqual(self.db.list_playstate(), [])

    # -- the two semantics -------------------------------------------------

    def test_the_local_position_may_go_backwards(self):
        """A cursor, not a high-water mark. Turning back a chapter is an
        ordinary thing to do, and `update_userdata`'s advance-only rule --
        right for a progress report -- would pin the reader at the furthest
        page ever reached."""
        self.offline()
        self.gateway.record_reading_position("srv", self.ITEM, 9 * TICKS)
        self.gateway.record_reading_position("srv", self.ITEM, 2 * TICKS)
        self.assertEqual(self.stored(), 2 * TICKS)

    def test_the_queued_position_may_not(self):
        """What is *sent* stays advance-only, so a client that has been
        offline cannot rewind the place another device reached."""
        self.offline()
        self.gateway.record_reading_position("srv", self.ITEM, 9 * TICKS)
        self.gateway.record_reading_position("srv", self.ITEM, 2 * TICKS)
        pending = self.db.list_playstate()
        self.assertEqual([p["position_ticks"] for p in pending], [9 * TICKS])

    def test_a_reconnect_sends_what_was_read_offline(self):
        """End to end through the real replay, which is the thing the queue
        exists for."""
        from jellyfin_mpv_shim.sync import manager as manager_module

        self.offline()
        self.gateway.record_reading_position("srv", self.ITEM, 5 * TICKS)

        api = self.online()
        api.get_userdata_for_item = lambda _id: {"PlaybackPositionTicks": 0}
        manager = manager_module.syncManager
        saved = manager.get_client
        saved_all = manager.get_clients
        self.addCleanup(setattr, manager, "get_client", saved)
        self.addCleanup(setattr, manager, "get_clients", saved_all)
        manager.get_client = lambda _uuid: deps.clientManager.clients["srv"]
        # The replay picks its route by *person* now, not by the login the
        # entry was queued under, so it needs the whole registry: one person
        # can hold several logins for one server and the one that comes back
        # is often not the one they were signed in as offline.
        manager.get_clients = lambda: dict(deps.clientManager.clients)
        manager._sync_playstate()

        self.assertEqual(api.written,
                         [(self.ITEM, {"PlaybackPositionTicks": 5 * TICKS})])
        self.assertEqual(self.db.list_playstate(), [])


if __name__ == "__main__":
    unittest.main()
