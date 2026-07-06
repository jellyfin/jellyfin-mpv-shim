import json
import logging
import os
import tempfile
import unittest

from jellyfin_mpv_shim.sync.db import (COLUMNS, SyncDB, STATUS_COMPLETE,
                                       STATUS_PENDING)
from jellyfin_mpv_shim.library_browser.repository import OfflineLibrarySource


def make_row(item_id, **overrides):
    row = {c: None for c in COLUMNS}
    row["item_id"] = item_id
    row.update(overrides)
    return row


class PendingQueueOrderingTest(unittest.TestCase):
    """The pending queue must drain in enqueue order, not catalog order, so a
    not-yet-resolvable item early in the catalog sort can't block the queue."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SyncDB(os.path.join(self.tmp.name, "cat.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_pending_ordered_by_added_at_not_catalog(self):
        # Catalog order (series_name, index) would sort "b" (Alpha) before "a"
        # (Zeta); enqueue order (added_at) must win for the pending list.
        self.db.upsert(make_row("a", series_name="Zeta", index_number=1,
                                 status=STATUS_PENDING, added_at=1))
        self.db.upsert(make_row("b", series_name="Alpha", index_number=2,
                                 status=STATUS_PENDING, added_at=2))
        ids = [r["item_id"] for r in self.db.list(status=STATUS_PENDING)]
        self.assertEqual(ids, ["a", "b"])

    def test_pending_falls_back_to_rowid_when_added_at_null(self):
        # Older rows may have no added_at; insert order (rowid) must still be the
        # tiebreaker rather than catalog order.
        self.db.upsert(make_row("a", series_name="Zeta", status=STATUS_PENDING))
        self.db.upsert(make_row("b", series_name="Alpha", status=STATUS_PENDING))
        ids = [r["item_id"] for r in self.db.list(status=STATUS_PENDING)]
        self.assertEqual(ids, ["a", "b"])

    def test_non_pending_list_still_catalog_ordered(self):
        # The ordering change must be scoped to the pending path only.
        self.db.upsert(make_row("a", series_name="Zeta", status=STATUS_COMPLETE,
                                 added_at=1))
        self.db.upsert(make_row("b", series_name="Alpha", status=STATUS_COMPLETE,
                                 added_at=2))
        ids = [r["item_id"] for r in self.db.list(status=STATUS_COMPLETE)]
        self.assertEqual(ids, ["b", "a"])  # Alpha before Zeta


class QueryErrorSurfacingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = SyncDB(os.path.join(self.tmp.name, "cat.db"))

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_query_error_returns_empty_and_logs_warning(self):
        # A genuine sqlite error must be loud (warning), not silently swallowed
        # at debug — but callers still get [] so they don't crash.
        with self.assertLogs("sync.db", level="WARNING") as cm:
            result = self.db._query("SELECT * FROM does_not_exist")
        self.assertEqual(result, [])
        self.assertTrue(any("Catalog query failed" in m for m in cm.output))


class OfflineReloadTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.catalog = os.path.join(self.root, "catalog.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _add_complete(self, db, item_id, name):
        db.upsert(make_row(
            item_id, type="Movie", name=name, status=STATUS_COMPLETE,
            file_path="%s/file.mkv" % item_id,
            item_json=json.dumps({"Id": item_id, "Name": name, "Type": "Movie"})))

    def test_reload_picks_up_new_downloads(self):
        writer = SyncDB(self.catalog)
        self._add_complete(writer, "m1", "First")
        source = OfflineLibrarySource(self.catalog)
        self.assertEqual({i["Id"] for i in source._items}, {"m1"})

        # A download finishes while the offline source is live.
        self._add_complete(writer, "m2", "Second")
        writer.close()
        source.reload()
        self.assertEqual({i["Id"] for i in source._items}, {"m1", "m2"})
        titles = {r["title"] for r in source.get_home_rows("offline")}
        self.assertIn("Downloaded Movies", titles)

    def test_missing_catalog_is_empty_not_crash(self):
        source = OfflineLibrarySource(os.path.join(self.root, "nope.db"))
        self.assertEqual(source._items, [])

    def test_corrupt_catalog_degrades_to_empty(self):
        with open(self.catalog, "wb") as fh:
            fh.write(b"this is not a sqlite database")
        # Construction (which calls reload) must not raise on a bad catalog.
        source = OfflineLibrarySource(self.catalog)
        self.assertEqual(source._items, [])


if __name__ == "__main__":
    unittest.main()
