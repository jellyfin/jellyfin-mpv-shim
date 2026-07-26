import json
import logging
import os
import tempfile
import unittest

from jellyfin_mpv_shim.sync.db import (COLUMNS, SyncDB, STATUS_COMPLETE,
                                       STATUS_PENDING)
from jellyfin_mpv_shim.mpvtk_browser.repository import OfflineLibrarySource


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


class OfflineResumePositionTest(unittest.TestCase):
    """Regression: the periodic offline position record was written to
    downloads.userdata_json but the browser built items from the item_json
    snapshot frozen at download time — so a relaunch always started playback
    from the beginning. The live userdata must overlay the snapshot."""

    RUNTIME = 600 * 10_000_000  # 10 minutes in ticks

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.catalog = os.path.join(self.tmp.name, "catalog.db")
        db = SyncDB(self.catalog)
        db.upsert(make_row(
            "ep1", type="Movie", name="Movie", status=STATUS_COMPLETE,
            file_path="ep1/file.mkv", runtime_ticks=self.RUNTIME,
            item_json=json.dumps({
                "Id": "ep1", "Name": "Movie", "Type": "Movie",
                "RunTimeTicks": self.RUNTIME,
                "UserData": {"Played": False, "PlaybackPositionTicks": 0},
            })))
        # The 30s periodic record during offline playback.
        db.update_userdata("ep1", position_ticks=self.RUNTIME // 2)
        db.close()
        self.addCleanup(self.tmp.cleanup)

    def test_reload_overlays_live_userdata(self):
        source = OfflineLibrarySource(self.catalog)
        (item,) = source._snap.items
        self.assertEqual(item["UserData"]["PlaybackPositionTicks"],
                         self.RUNTIME // 2)
        # Derived for the tile progress bar (offline writes never set it).
        self.assertAlmostEqual(item["UserData"]["PlayedPercentage"], 50.0)

    def test_get_item_overlays_live_userdata(self):
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep1")
        self.assertEqual(item["UserData"]["PlaybackPositionTicks"],
                         self.RUNTIME // 2)

    def test_finish_clears_resume_point(self):
        # Watching to the end (or marking watched) must clear the resume
        # point like the server does, not leave "resume from the very end".
        db = SyncDB(self.catalog)
        db.update_userdata("ep1", played=True,
                           position_ticks=self.RUNTIME)
        db.close()
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep1")
        self.assertTrue(item["UserData"]["Played"])
        self.assertEqual(item["UserData"]["PlaybackPositionTicks"], 0)

    def test_post_finish_stop_report_does_not_resurrect_resume(self):
        # Close-after-finish: the mpv shutdown path re-reports the last known
        # position (~the full duration) with finished=False AFTER the finish
        # cleared the resume point. The near-end guard must not let it
        # re-create "Resume from <the very end>" on the watched item.
        db = SyncDB(self.catalog)
        db.update_userdata("ep1", played=True, position_ticks=self.RUNTIME)
        db.update_userdata("ep1", position_ticks=self.RUNTIME)  # stop report
        db.close()
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep1")
        self.assertTrue(item["UserData"]["Played"])
        self.assertEqual(item["UserData"]["PlaybackPositionTicks"], 0)

    def test_rewatch_of_watched_item_still_records_resume(self):
        # The near-end guard must not block a genuine mid-file rewatch resume
        # point on an already-watched item (server semantics allow both).
        db = SyncDB(self.catalog)
        db.update_userdata("ep1", played=True, position_ticks=self.RUNTIME)
        db.update_userdata("ep1", position_ticks=self.RUNTIME // 4)
        db.close()
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep1")
        self.assertTrue(item["UserData"]["Played"])
        self.assertEqual(item["UserData"]["PlaybackPositionTicks"],
                         self.RUNTIME // 4)

    def test_seeded_played_percentage_is_recomputed(self):
        # Download-time seeding copies the server's full UserData (including
        # a stale PlayedPercentage) into userdata_json; the browser must
        # derive the percentage from the live position, not the seed.
        db = SyncDB(self.catalog)
        db.upsert(make_row(
            "ep3", type="Movie", name="Seeded", status=STATUS_COMPLETE,
            file_path="ep3/file.mkv", runtime_ticks=self.RUNTIME,
            item_json=json.dumps({
                "Id": "ep3", "Name": "Seeded", "Type": "Movie",
                "RunTimeTicks": self.RUNTIME,
                "UserData": {"PlayedPercentage": 20.0,
                             "PlaybackPositionTicks": self.RUNTIME // 5},
            }),
            userdata_json=json.dumps({
                "PlayedPercentage": 20.0,
                "PlaybackPositionTicks": self.RUNTIME // 5,
            })))
        db.update_userdata("ep3", position_ticks=self.RUNTIME // 2)
        db.close()
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep3")
        self.assertAlmostEqual(item["UserData"]["PlayedPercentage"], 50.0)

    def test_watched_item_has_no_partial_progress_bar(self):
        # A finished item (position cleared) must not keep a stale percentage
        # from the snapshot or the seed — watched shows a badge, not a bar.
        db = SyncDB(self.catalog)
        db.upsert(make_row(
            "ep4", type="Movie", name="Watched", status=STATUS_COMPLETE,
            file_path="ep4/file.mkv", runtime_ticks=self.RUNTIME,
            item_json=json.dumps({
                "Id": "ep4", "Name": "Watched", "Type": "Movie",
                "RunTimeTicks": self.RUNTIME,
                "UserData": {"PlayedPercentage": 42.0},
            }),
            userdata_json=json.dumps({"PlayedPercentage": 42.0})))
        db.update_userdata("ep4", played=True,
                           position_ticks=self.RUNTIME)
        db.close()
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep4")
        self.assertTrue(item["UserData"]["Played"])
        self.assertNotIn("PlayedPercentage", item["UserData"])

    def test_snapshot_alone_still_works(self):
        # Rows with no live userdata (never played offline) keep the snapshot.
        db = SyncDB(self.catalog)
        db.upsert(make_row(
            "ep2", type="Movie", name="Other", status=STATUS_COMPLETE,
            file_path="ep2/file.mkv",
            item_json=json.dumps({
                "Id": "ep2", "Name": "Other", "Type": "Movie",
                "UserData": {"Played": True},
            })))
        db.close()
        source = OfflineLibrarySource(self.catalog)
        item = source.get_item("offline", "ep2")
        self.assertTrue(item["UserData"]["Played"])


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
        self.assertEqual({i["Id"] for i in source._snap.items}, {"m1"})

        # A download finishes while the offline source is live.
        self._add_complete(writer, "m2", "Second")
        writer.close()
        source.reload()
        self.assertEqual({i["Id"] for i in source._snap.items}, {"m1", "m2"})
        titles = {r["title"] for r in source.get_home_rows("offline")}
        self.assertIn("Downloaded Movies", titles)

    def test_missing_catalog_is_empty_not_crash(self):
        source = OfflineLibrarySource(os.path.join(self.root, "nope.db"))
        self.assertEqual(source._snap.items, [])

    def test_corrupt_catalog_degrades_to_empty(self):
        with open(self.catalog, "wb") as fh:
            fh.write(b"this is not a sqlite database")
        # Construction (which calls reload) must not raise on a bad catalog.
        source = OfflineLibrarySource(self.catalog)
        self.assertEqual(source._snap.items, [])


if __name__ == "__main__":
    unittest.main()
