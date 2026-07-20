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


class TestMpvtkOfflineFallback(TmpTest):
    """work_offline has to actually browse the downloads in the mpvtk UI.
    It used to only skip the connect, which left the browser on a login
    screen it could never get past."""

    def _controller(self, catalog_path):
        import jellyfin_mpv_shim.sync.manager as mgr
        from jellyfin_mpv_shim.mpvtk_browser.ui import _PlayerController

        class FakeSync:
            db = type("DB", (), {"path": catalog_path})()

        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))
        return _PlayerController()

    def _catalog_with_a_movie(self):
        path = os.path.join(self.tmp, "catalog.db")
        db = SyncDB(path)
        db.upsert(make_row("m1", type="Movie"))
        db.close()
        return path

    def test_work_offline_falls_back_to_the_catalog(self):
        from jellyfin_mpv_shim.conf import settings

        ctl = self._controller(self._catalog_with_a_movie())
        old, settings.work_offline = settings.work_offline, True
        self.addCleanup(lambda: setattr(settings, "work_offline", old))
        source = ctl.connect_and_rebuild()
        self.assertIsInstance(source, OfflineLibrarySource)
        self.assertEqual([s["uuid"] for s in source.servers()], ["offline"])

    def _patched_start_playback(self, calls):
        # The app parses sys.argv the first time a module resolves the config
        # dir, and event_handler pulls that in — under the test runner argv
        # carries unittest's tokens and argparse exits. Same guard the
        # integration harness uses.
        import sys

        saved, sys.argv = sys.argv, [sys.argv[0]]
        self.addCleanup(lambda: setattr(sys, "argv", saved))
        import jellyfin_mpv_shim.event_handler as eh

        real = eh.start_playback
        eh.start_playback = lambda client, ids, **kw: calls.append(
            (client, list(ids), kw))
        self.addCleanup(lambda: setattr(eh, "start_playback", real))

    def test_offline_play_goes_to_the_local_file(self):
        """The pseudo-server "offline" has no client. Bailing on that made
        every downloaded item unplayable — start_playback takes client=None
        and resolves the item against the catalog."""
        import jellyfin_mpv_shim.sync.manager as mgr

        class FakeDB:
            path = "unused"

            def is_complete(self_inner, item_id):
                return item_id in ("m1", "m2")

        class FakeSync:
            db = FakeDB()

        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))

        from jellyfin_mpv_shim.mpvtk_browser.ui import _PlayerController

        calls = []
        self._patched_start_playback(calls)
        ctl = _PlayerController()

        ctl.play_list(["m1", "m2"], "offline", 0)
        self.assertEqual(len(calls), 1)
        self.assertIsNone(calls[0][0], "no client offline")
        self.assertEqual(calls[0][1], ["m1", "m2"])

        # Starting partway in checks *that* item, not item_ids[0].
        ctl.play_list(["ghost", "m2"], "offline", 1)
        self.assertEqual(len(calls), 2)

        # Nothing downloaded: still refuses rather than starting a dead play.
        ctl.play_list(["ghost"], "offline", 0)
        self.assertEqual(len(calls), 2)

    def test_empty_catalog_yields_no_source(self):
        """Nothing downloaded: the caller wants None so it can show login,
        not an empty library that looks like a broken server."""
        ctl = self._controller(os.path.join(self.tmp, "missing.db"))
        self.assertIsNone(ctl.offline_source())


class TestOfflinePlaylistArt(TmpTest):
    """A playlist tile uses the playlist's OWN poster, cached at download
    time. It used to borrow a member's, so a playlist whose first member
    had no poster.jpg on disk showed a bare letter glyph."""

    def _catalog(self, playlist_art=False, member_art=True):
        path = os.path.join(self.tmp, "catalog.db")
        db = SyncDB(path)
        for item in ("m1", "m2"):
            db.upsert(make_row(item, type="Movie", server_id="srv",
                               file_path="%s/file.mkv" % item))
            item_dir = os.path.join(self.tmp, item)
            os.makedirs(item_dir, exist_ok=True)
            if member_art:
                with open(os.path.join(item_dir, "poster.jpg"), "wb") as fh:
                    fh.write(b"jpeg")
        db.upsert_playlist("P", "srv", "uuid", "Mine")
        db.replace_playlist_items("P", [("m1", 0, 1), ("m2", 1, 1)])
        db.close()
        if playlist_art:
            pl_dir = os.path.join(self.tmp, "srv", "playlist", "P")
            os.makedirs(pl_dir, exist_ok=True)
            with open(os.path.join(pl_dir, "poster.jpg"), "wb") as fh:
                fh.write(b"jpeg")
        return path

    def test_uses_the_playlists_own_poster(self):
        src = OfflineLibrarySource(self._catalog(playlist_art=True))
        self.assertIsNotNone(src.image_spec({"Id": "P", "Type": "Playlist"}))
        self.assertTrue(
            src.image_url("offline", "P", "Primary", "offline", 100)
            .endswith(os.path.join("srv", "playlist", "P", "poster.jpg")))

    def test_does_not_borrow_a_members_poster(self):
        """Members have art, the playlist doesn't: the tile shows its glyph
        rather than pretending a member's poster is the playlist's."""
        src = OfflineLibrarySource(self._catalog(playlist_art=False,
                                                 member_art=True))
        self.assertIsNone(src.image_spec({"Id": "P", "Type": "Playlist"}))

    def test_playlists_library_tile_uses_a_playlist_poster(self):
        src = OfflineLibrarySource(self._catalog(playlist_art=True))
        self.assertIsNotNone(
            src.image_spec({"Id": "offline:playlists", "Type": "UserView"}))


class TestOnlinePlaylistArt(unittest.TestCase):
    """Online, a playlist's image comes from its own item id — asked for
    even when the DTO carries no tag, since the server generates it."""

    def test_playlist_resolves_to_its_own_primary(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource

        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "P1", "Type": "Playlist", "ImageTags": {}}
        self.assertEqual(src.image_spec(item), ("P1", "Primary", "playlist"))

    def test_a_tagged_playlist_keeps_its_tag(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource

        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "P1", "Type": "Playlist", "ImageTags": {"Primary": "t9"}}
        self.assertEqual(src.image_spec(item), ("P1", "Primary", "t9"))

    def test_a_playlist_never_borrows_series_or_album_art(self):
        """The playlist branch must come before the parent fallbacks, or a
        playlist DTO carrying SeriesId/AlbumId would show that instead."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource

        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "P1", "Type": "Playlist", "ImageTags": {},
                "AlbumId": "A1", "AlbumPrimaryImageTag": "at"}
        self.assertEqual(src.image_spec(item)[0], "P1")


class TestPlaylistArtDownload(TmpTest):
    """Playlist art is fetched at download time so the offline tile has
    the playlist's own poster to show."""

    def _manager(self, fetched, status=200):
        import jellyfin_mpv_shim.sync.manager as mgr

        class FakeResp:
            status_code = status
            content = b"jpeg"

            def raise_for_status(self):
                if status >= 400:
                    raise RuntimeError("HTTP %d" % status)

        def fake_get(url, **kw):
            fetched.append(url)
            return FakeResp()

        real, mgr.requests = mgr.requests, type(
            "R", (), {"get": staticmethod(fake_get)})
        self.addCleanup(lambda: setattr(mgr, "requests", real))

        jf = FakeJellyfin([])
        jf.artwork = lambda item_id, kind, size: "http://s/%s/%s" % (item_id,
                                                                    kind)
        m = make_manager(self.tmp, jf)
        self.addCleanup(m.db.close)
        return m

    def _poster(self):
        return os.path.join(self.tmp, "srv", "playlist", "P", "poster.jpg")

    def test_downloads_the_playlists_own_primary(self):
        fetched = []
        m = self._manager(fetched)
        m._download_playlist_art(m.get_client("uuid"), "srv", "P")
        self.assertEqual(fetched, ["http://s/P/Primary"])
        self.assertTrue(os.path.exists(self._poster()))

    def test_is_skipped_when_already_cached(self):
        fetched = []
        m = self._manager(fetched)
        m._download_playlist_art(m.get_client("uuid"), "srv", "P")
        m._download_playlist_art(m.get_client("uuid"), "srv", "P")
        self.assertEqual(len(fetched), 1, "re-fetched a cached poster")

    def test_a_playlist_without_art_is_not_fatal(self):
        """Most playlists have no image; that must not fail the download."""
        fetched = []
        m = self._manager(fetched, status=404)
        m._download_playlist_art(m.get_client("uuid"), "srv", "P")
        self.assertFalse(os.path.exists(self._poster()))


if __name__ == "__main__":
    unittest.main()
