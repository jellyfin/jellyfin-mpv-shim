"""Offline playlist support: download provenance/ownership in the sync manager,
the catalog's playlist tables, and the offline browser's playlist + video views.

No network: the manager's client/api are fakes and enqueue only writes catalog
rows (the download worker isn't started), so ownership and membership are
exercised directly.
"""

import json
import os
import shutil
import tempfile
import unittest

from jellyfin_mpv_shim.sync.manager import SyncManager
from jellyfin_mpv_shim.sync.db import SyncDB, COLUMNS, STATUS_COMPLETE, STATUS_PENDING
from jellyfin_mpv_shim.mpvtk_browser.repository import OfflineLibrarySource


def make_row(item_id, **overrides):
    row = {c: None for c in COLUMNS}
    row["item_id"] = item_id
    row["status"] = STATUS_COMPLETE
    row["type"] = "Movie"
    row["name"] = item_id
    row["file_path"] = "%s/file.mkv" % item_id
    row["item_json"] = json.dumps({"Id": item_id, "Name": item_id,
                                   "Type": overrides.get("type", "Movie")})
    row.update(overrides)
    if "item_json" not in overrides:
        row["item_json"] = json.dumps({"Id": item_id, "Name": row["name"],
                                       "Type": row["type"]})
    return row


class FakeConfig:
    def __init__(self):
        self.data = {"auth.server-id": "srv"}


class FakeJellyfin:
    def __init__(self, playlist_items, playlist_name="My Playlist"):
        self._items = playlist_items
        self._name = playlist_name

    def get_playlist_items(self, playlist_id, fields=None):
        return {"Items": list(self._items)}

    def get_item(self, item_id, **kw):
        return {"Id": item_id, "Name": self._name, "Type": "Playlist"}


class FakeClient:
    def __init__(self, jf):
        self.jellyfin = jf
        self.config = FakeConfig()


def make_manager(root, jf):
    m = SyncManager()
    m.root = root
    m.db = SyncDB(os.path.join(root, "catalog.db"))
    m.get_client = lambda uuid: FakeClient(jf)
    m._notify_change = lambda: None
    return m


def pl_item(item_id, item_type="Movie", played=False, size=100):
    return {"Id": item_id, "Name": item_id, "Type": item_type,
            "MediaType": "Video", "MediaSources": [{"Id": "ms", "Size": size,
                                                    "Container": "mkv"}],
            "UserData": {"Played": played}}


class TmpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class PlaylistOwnershipTest(TmpTest):
    def test_fresh_playlist_owns_all_items(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b"), pl_item("c")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL"), {"a", "b", "c"})
        self.assertEqual(m.db.playlist_ownership(),
                         {"a": "PL", "b": "PL", "c": "PL"})
        m.db.close()

    def test_preexisting_download_is_not_owned(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        # "a" was already downloaded another way before the playlist.
        m.db.upsert(make_row("a", status=STATUS_COMPLETE))
        m.enqueue("uuid", "PL", "Playlist")
        # a stays unowned (keeps its own grouping); only b is owned.
        self.assertEqual(m.db.playlist_owned_ids("PL"), {"b"})
        # ...but a is still a *member* (it's in the playlist and downloaded).
        rows = m.db.playlist_item_rows("PL")
        # b is only pending here, so complete-only rows == just a.
        self.assertEqual({r["item_id"] for r in rows}, {"a"})
        self.assertEqual(m.db.playlist_ownership(), {"b": "PL"})
        m.db.close()

    def test_redownload_preserves_prior_ownership(self):
        jf = FakeJellyfin([pl_item("a")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")            # a owned (pending)
        m.db.update("a", status=STATUS_COMPLETE)       # finishes downloading
        m.enqueue("uuid", "PL", "Playlist")            # re-download
        # a now pre-exists, but was already owned by PL -> ownership sticks.
        self.assertEqual(m.db.playlist_owned_ids("PL"), {"a"})
        m.db.close()

    def test_duplicate_item_in_playlist_recorded_once(self):
        # Jellyfin allows the same item twice in a playlist; membership is keyed
        # by item_id, so it must not blow up the UNIQUE constraint.
        jf = FakeJellyfin([pl_item("a"), pl_item("b"), pl_item("a")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL"), {"a", "b"})
        m.db.close()

    def test_audio_playlist_is_recorded(self):
        # Music playlists download as a unit: Audio is a supported playlist
        # type, so its tracks are recorded as playlist members.
        jf = FakeJellyfin([{"Id": "s1", "Type": "Audio", "MediaType": "Audio",
                            "MediaSources": [{"Id": "ms", "Container": "flac",
                                              "Size": 1}],
                            "UserData": {}}])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL"), {"s1"})
        m.db.close()

    def test_unsupported_playlist_records_nothing(self):
        # A playlist of only unsupported types (e.g. MusicVideo, not in
        # PLAYLIST_SUPPORTED_TYPES) expands to nothing, so no playlist is made.
        jf = FakeJellyfin([{"Id": "s1", "Type": "MusicVideo",
                            "MediaSources": [{"Id": "ms", "Size": 1}],
                            "UserData": {}}])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL"), set())
        self.assertEqual(m.db.list_playlists(), [])
        m.db.close()


class PlaylistDeleteTest(TmpTest):
    def test_delete_playlist_removes_owned_keeps_preexisting(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.db.upsert(make_row("a", status=STATUS_COMPLETE))  # pre-existing
        m.enqueue("uuid", "PL", "Playlist")                 # b owned (pending)
        m.db.update("b", status=STATUS_COMPLETE)
        m._delete_playlist("PL")
        # Owned b is deleted; pre-existing a survives; playlist record gone.
        self.assertIsNone(m.db.get("b"))
        self.assertIsNotNone(m.db.get("a"))
        self.assertEqual(m.db.list_playlists(), [])
        self.assertEqual(m.db.playlist_owned_ids("PL"), set())
        m.db.close()

    def test_delete_item_cascades_membership(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        m.db.delete("a")
        self.assertEqual(m.db.playlist_owned_ids("PL"), {"b"})
        m.db.close()


class ListPlaylistsTest(TmpTest):
    def test_only_playlists_with_complete_items_listed(self):
        db = SyncDB(os.path.join(self.tmp, "c.db"))
        db.upsert(make_row("a", status=STATUS_COMPLETE))
        db.upsert(make_row("b", status=STATUS_PENDING))
        db.upsert_playlist("P1", "srv", "uuid", "Has Complete")
        db.replace_playlist_items("P1", [("a", 0, 1)])
        db.upsert_playlist("P2", "srv", "uuid", "Only Pending")
        db.replace_playlist_items("P2", [("b", 0, 1)])
        names = {p["name"] for p in db.list_playlists()}
        self.assertEqual(names, {"Has Complete"})
        db.close()

    def test_playlist_item_rows_in_sort_order(self):
        db = SyncDB(os.path.join(self.tmp, "c.db"))
        db.upsert(make_row("a", name="A", status=STATUS_COMPLETE))
        db.upsert(make_row("b", name="B", status=STATUS_COMPLETE))
        db.upsert_playlist("P", "srv", "uuid", "P")
        # Deliberately record b before a, but with sort_index putting b last.
        db.replace_playlist_items("P", [("b", 1, 1), ("a", 0, 1)])
        ids = [r["item_id"] for r in db.playlist_item_rows("P")]
        self.assertEqual(ids, ["a", "b"])
        db.close()


class OfflinePlaylistBrowseTest(TmpTest):
    def _catalog(self):
        db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        db.upsert(make_row("m1", type="Movie", name="A Movie"))
        db.upsert(make_row("v1", type="Video", name="Home Video"))
        db.upsert(make_row("e1", type="Episode", name="Ep",
                           series_id="s1", series_name="Show",
                           item_json=json.dumps({"Id": "e1", "Name": "Ep",
                                                 "Type": "Episode",
                                                 "SeriesId": "s1",
                                                 "SeriesName": "Show"})))
        db.upsert_playlist("P", "srv", "uuid", "Trip")
        db.replace_playlist_items("P", [("m1", 0, 1), ("v1", 1, 1)])
        db.close()
        return os.path.join(self.tmp, "catalog.db")

    def test_video_is_separated_from_movies(self):
        src = OfflineLibrarySource(self._catalog())
        lib_ids = {l["Id"] for l in src.get_libraries("offline")}
        self.assertIn("offline:movies", lib_ids)
        self.assertIn("offline:videos", lib_ids)
        movies, _ = src.get_library_items("offline", "offline:movies")
        videos, _ = src.get_library_items("offline", "offline:videos")
        self.assertEqual({i["Id"] for i in movies}, {"m1"})
        self.assertEqual({i["Id"] for i in videos}, {"v1"})

    def test_home_rows_split_movies_and_videos(self):
        src = OfflineLibrarySource(self._catalog())
        rows = {r["title"]: [i["Id"] for i in r["items"]]
                for r in src.get_home_rows("offline")}
        self.assertEqual(rows.get("Downloaded Movies"), ["m1"])
        self.assertEqual(rows.get("Downloaded Videos"), ["v1"])

    def test_offline_playlist_tile_and_contents(self):
        src = OfflineLibrarySource(self._catalog())
        lib_ids = {l["Id"] for l in src.get_libraries("offline")}
        self.assertIn("offline:playlists", lib_ids)
        pls, _ = src.get_library_items("offline", "offline:playlists")
        self.assertEqual([(p["Id"], p["Type"]) for p in pls], [("P", "Playlist")])
        items = src.get_playlist_items("offline", "P")
        self.assertEqual([i["Id"] for i in items], ["m1", "v1"])  # playlist order

    def test_no_playlist_tile_without_downloaded_members(self):
        db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        db.upsert(make_row("m1", type="Movie"))
        db.upsert_playlist("P", "srv", "uuid", "Empty")
        db.replace_playlist_items("P", [("ghost", 0, 1)])  # not downloaded
        db.close()
        src = OfflineLibrarySource(os.path.join(self.tmp, "catalog.db"))
        self.assertNotIn("offline:playlists",
                         {l["Id"] for l in src.get_libraries("offline")})


if __name__ == "__main__":
    unittest.main()
