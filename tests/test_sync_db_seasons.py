"""Season-level download state in the sync catalog.

A Season is never itself a downloads row — SyncManager.download expands it
into its episodes — so the catalog needs a query that rolls episodes back up
to their season. Without one, ``_is_downloaded`` had no branch that could
ever return True for a Season, and a fully downloaded season showed
"Download" forever, never got the tile badge, and could not be removed.
"""

import os
import sys
import tempfile
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    SyncDB, STATUS_COMPLETE, STATUS_PENDING)


def episode(item_id, season_id, series_id="sh1", status=STATUS_COMPLETE):
    return {
        "item_id": item_id, "server_uuid": "srv1", "server_id": "s",
        "name": item_id, "type": "Episode", "status": status,
        "series_id": series_id, "series_name": "Show",
        "season_id": season_id, "parent_index": 1, "index_number": 1,
    }


class TestDownloadedSeasonIds(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="jms-seasondb-")
        self.db = SyncDB(os.path.join(self.tmp, "sync.db"))
        self.addCleanup(self.db.close)

    def test_a_completed_episode_makes_its_season_downloaded(self):
        self.db.upsert(episode("e1", "sea1"))
        self.assertEqual(self.db.downloaded_season_ids(), {"sea1"})

    def test_a_pending_episode_does_not(self):
        self.db.upsert(episode("e1", "sea1", status=STATUS_PENDING))
        self.assertEqual(self.db.downloaded_season_ids(), set())

    def test_seasons_are_reported_once_each(self):
        self.db.upsert(episode("e1", "sea1"))
        self.db.upsert(episode("e2", "sea1"))
        self.db.upsert(episode("e3", "sea2"))
        self.assertEqual(self.db.downloaded_season_ids(), {"sea1", "sea2"})

    def test_a_row_with_no_season_is_ignored_rather_than_yielding_none(self):
        """A movie has no season_id. NULL must not come back as a member of
        the set, or `item["Id"] in seasons` could match on nothing."""
        row = episode("m1", None)
        row["type"] = "Movie"
        row["series_id"] = None
        self.db.upsert(row)
        self.assertEqual(self.db.downloaded_season_ids(), set())

    def test_it_agrees_with_the_series_level_query(self):
        self.db.upsert(episode("e1", "sea1", series_id="sh1"))
        self.assertEqual(self.db.downloaded_series_ids(), {"sh1"})
        self.assertEqual(self.db.downloaded_season_ids(), {"sea1"})

    def test_an_empty_catalog_is_an_empty_set(self):
        self.assertEqual(self.db.downloaded_season_ids(), set())


if __name__ == "__main__":
    unittest.main()
