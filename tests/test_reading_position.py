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

import json
import os
import sys
import tempfile
import unittest

sys.argv = [sys.argv[0]]      # importing the gateway reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.gateway import deps  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.gateway.userdata import (  # noqa: E402
    UserDataMixin)
from jellyfin_mpv_shim.sync.db import (COLUMNS, STATUS_COMPLETE,  # noqa: E402
                                       SyncDB)

TICKS = 10_000_000


def row(item_id, position=0):
    record = {c: None for c in COLUMNS}
    record["item_id"] = item_id
    record["status"] = STATUS_COMPLETE
    record["type"] = "Book"
    record["name"] = "A Novel"
    record["file_path"] = "%s/book.epub" % item_id
    record["item_json"] = json.dumps({"Id": item_id, "Type": "Book"})
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
        deps.clientManager = Manager()
        return Manager.clients["srv"].jellyfin

    def offline(self):
        class Manager:
            clients = {}
        deps.clientManager = Manager()

    def stored(self, item_id=None):
        record = self.db.get(item_id or self.ITEM)
        return json.loads(record["userdata_json"] or "{}").get(
            "PlaybackPositionTicks")

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
        with no row would sit there for good."""
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
        self.addCleanup(setattr, manager, "get_client", saved)
        manager.get_client = lambda _uuid: deps.clientManager.clients["srv"]
        manager._sync_playstate()

        self.assertEqual(api.written,
                         [(self.ITEM, {"PlaybackPositionTicks": 5 * TICKS})])
        self.assertEqual(self.db.list_playstate(), [])


if __name__ == "__main__":
    unittest.main()
