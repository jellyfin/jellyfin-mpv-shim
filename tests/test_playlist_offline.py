"""Offline playlist support: download provenance/ownership in the sync manager,
the catalog's playlist tables, and the offline browser's playlist + video views.

No network: the manager's client/api are fakes and enqueue only writes catalog
rows (the download worker isn't started), so ownership and membership are
exercised directly.
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
import shutil
import tempfile
import unittest
from unittest import mock

from jellyfin_mpv_shim.sync.manager import SyncManager
from jellyfin_mpv_shim.constants import offline_playlist_id
from jellyfin_mpv_shim.sync.db import (ANY_SERVER, NO_ACTOR, SyncDB, COLUMNS,
                                       STATUS_COMPLETE, STATUS_PENDING,
                                       playlist_art_dir)
from jellyfin_mpv_shim.mpvtk_browser.repository import OfflineLibrarySource


#: The Jellyfin ServerId these fixtures' rows belong to. Set deliberately:
#: a downloads row with no content server is an ORPHAN, and the orphan path
#: is a distinct contract (docs/offline-sync.md section 1).
#: A fixture that omits this silently tests the orphan path under another
#: name -- which is what every row in this file used to do.
CONTENT_SERVER = "srv"   # matches FakeConfig's auth.server-id


def make_row(item_id, **overrides):
    row = {c: None for c in COLUMNS}
    row["item_id"] = item_id
    row["content_server_id"] = CONTENT_SERVER
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
        # auth.server matches the host the artwork fakes below hand back, so
        # _headers_for sees a same-origin url and actually attaches the
        # header here rather than short-circuiting to {}.
        self.data = {"auth.server-id": "srv", "auth.server": "http://s"}


class FakeHttp:
    def _get_authenication_header(self):
        return 'MediaBrowser Client="test", Token="TESTTOKEN"'


class FakeJellyfin:
    def __init__(self, playlist_items, playlist_name="My Playlist"):
        self._items = playlist_items
        self._name = playlist_name

    def get_playlist_items(self, playlist_id, fields=None):
        return {"Items": list(self._items)}

    def get_item(self, item_id, **kw):
        return {"Id": item_id, "Name": self._name, "Type": "Playlist",
                "ServerId": CONTENT_SERVER}


class FakeClient:
    def __init__(self, jf):
        self.jellyfin = jf
        self.config = FakeConfig()
        self.http = FakeHttp()


def make_manager(root, jf):
    m = SyncManager()
    m.root = root
    m.db = SyncDB(os.path.join(root, "catalog.db"))
    m.get_client = lambda uuid: FakeClient(jf)
    # Which server the login speaks for. Without it `content_id_for` answers
    # None, every content read runs unscoped, and the collision door cannot
    # tell a pre-existing row from another server's -- so a fixture row and
    # the login that enqueues beside it silently belong to different servers.
    m.content_id_for = staticmethod(lambda uuid: CONTENT_SERVER)
    m._notify_change = lambda: None
    return m


def pl_item(item_id, item_type="Movie", played=False, size=100):
    return {"Id": item_id, "Name": item_id, "Type": item_type,
            # The production path reads the content server off the DTO
            # (`_add_row`), so a DTO without one makes every row the code
            # creates an orphan -- separately from the hand-built fixtures.
            "ServerId": CONTENT_SERVER,
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
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"a", "b", "c"})
        # The value is (playlist_id, server_id) now: two servers can hold a
        # playlist with one id, so the grouping has to tell them apart.
        self.assertEqual(m.db.playlist_ownership(),
                         {"a": ("PL", CONTENT_SERVER),
                          "b": ("PL", CONTENT_SERVER),
                          "c": ("PL", CONTENT_SERVER)})
        m.db.close()

    def test_preexisting_download_is_not_owned(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        # "a" was already downloaded another way before the playlist.
        m.db.upsert(make_row("a", status=STATUS_COMPLETE))
        m.enqueue("uuid", "PL", "Playlist")
        # a stays unowned (keeps its own grouping); only b is owned.
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"b"})
        # ...but a is still a *member* (it's in the playlist and downloaded).
        rows = m.db.playlist_item_rows("PL", server_id=CONTENT_SERVER)
        # b is only pending here, so complete-only rows == just a.
        self.assertEqual({r["item_id"] for r in rows}, {"a"})
        self.assertEqual(m.db.playlist_ownership(),
                         {"b": ("PL", CONTENT_SERVER)})
        m.db.close()

    def test_redownload_preserves_prior_ownership(self):
        jf = FakeJellyfin([pl_item("a")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")            # a owned (pending)
        m.db.update("a", status=STATUS_COMPLETE)       # finishes downloading
        m.enqueue("uuid", "PL", "Playlist")            # re-download
        # a now pre-exists, but was already owned by PL -> ownership sticks.
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"a"})
        m.db.close()

    def test_duplicate_item_in_playlist_recorded_once(self):
        # Jellyfin allows the same item twice in a playlist; membership is keyed
        # by item_id, so it must not blow up the UNIQUE constraint.
        jf = FakeJellyfin([pl_item("a"), pl_item("b"), pl_item("a")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"a", "b"})
        m.db.close()

    def test_audio_playlist_is_recorded(self):
        # Music playlists download as a unit: Audio is a supported playlist
        # type, so its tracks are recorded as playlist members.
        jf = FakeJellyfin([{"Id": "s1", "Type": "Audio", "MediaType": "Audio",
                            "ServerId": CONTENT_SERVER,
                            "MediaSources": [{"Id": "ms", "Container": "flac",
                                              "Size": 1}],
                            "UserData": {}}])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"s1"})
        m.db.close()

    def test_unsupported_playlist_records_nothing(self):
        # A playlist of only unsupported types (e.g. MusicVideo, not in
        # PLAYLIST_SUPPORTED_TYPES) expands to nothing, so no playlist is made.
        #
        # This is the GENUINELY-empty case and dropping the record is correct.
        # It is not evidence about the failed-expansion case, which looked
        # identical from inside enqueue until _expand learned to raise -- see
        # PlaylistExpansionFailureTest.
        jf = FakeJellyfin([{"Id": "s1", "Type": "MusicVideo",
                            "MediaSources": [{"Id": "ms", "Size": 1}],
                            "UserData": {}}])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), set())
        self.assertEqual(m.db.list_playlists(), [])
        m.db.close()


class PlaylistExpansionFailureTest(TmpTest):
    """A server error while listing a playlist must not be read as "this
    playlist is empty now".

    `_expand` caught everything and returned [], so a 500 on the ordinary
    top-up gesture -- press Download on a playlist you already have -- reached
    `_record_playlist(member_ids=[])`, which deletes the playlist row and every
    ownership row with it. `enqueue` then returned 0 without raising, so the
    dialog ran its `on_ok` and told the user it had worked.

    The ownership loss outlived the outage: with the rows gone, the next
    successful download sees an empty `already_owned` and a full
    `pre_existing`, marks every track unowned, and "Delete playlist" from then
    on removes the record and no files at all.
    """

    class AngryJellyfin(FakeJellyfin):
        def get_playlist_items(self, playlist_id, fields=None):
            raise RuntimeError("500 Internal Server Error")

    def _downloaded_playlist(self):
        """A playlist actually *downloaded*, not merely queued.

        `list_playlists()` only returns playlists holding at least one
        COMPLETE item, so leaving these PENDING would make its assertion pass
        trivially -- it would read [] both before and after.
        """
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        for iid in ("a", "b"):
            m.db.update(iid, status=STATUS_COMPLETE)
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"a", "b"})
        self.assertEqual([p["playlist_id"] for p in m.db.list_playlists()],
                         ["PL"], "the fixture did not record a playlist")
        return m

    def test_a_failed_listing_keeps_the_playlist_and_its_ownership(self):
        m = self._downloaded_playlist()
        self.addCleanup(m.db.close)
        m.get_client = lambda uuid: FakeClient(self.AngryJellyfin([]))
        with self.assertRaises(Exception):
            m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual([p["playlist_id"] for p in m.db.list_playlists()],
                         ["PL"], "a server error deleted the playlist record")
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"a", "b"},
                         "a server error dropped playlist ownership, so a "
                         "later delete would remove the record and no files")

    def test_the_failure_reaches_the_caller(self):
        """`gateway.download_enqueue` documents "Raises on failure" and the
        dialog's `_edit_call` deliberately does not swallow, so the whole
        chain above this already reports it -- once enqueue stops returning
        0 as though it had succeeded."""
        m = self._downloaded_playlist()
        self.addCleanup(m.db.close)
        m.get_client = lambda uuid: FakeClient(self.AngryJellyfin([]))
        with self.assertRaises(Exception):
            m.enqueue("uuid", "PL", "Playlist")

    def test_estimate_also_reports_the_failure(self):
        """Same swallow, second victim: `download_estimate` says returning a
        zero estimate made failure indistinguishable from "already fully
        downloaded", and the dialog then hides the retry control."""
        m = self._downloaded_playlist()
        self.addCleanup(m.db.close)
        m.get_client = lambda uuid: FakeClient(self.AngryJellyfin([]))
        with self.assertRaises(Exception):
            m.estimate("uuid", "PL", "Playlist")


class PlaylistDeleteTest(TmpTest):
    def test_delete_playlist_removes_owned_keeps_preexisting(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.db.upsert(make_row("a", status=STATUS_COMPLETE))  # pre-existing
        m.enqueue("uuid", "PL", "Playlist")                 # b owned (pending)
        m.db.update("b", status=STATUS_COMPLETE)
        m._delete_playlist("PL", server_id=CONTENT_SERVER)
        # Owned b is deleted; pre-existing a survives; playlist record gone.
        self.assertIsNone(m.db.get("b"))
        self.assertIsNotNone(m.db.get("a"))
        self.assertEqual(m.db.list_playlists(), [])
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), set())
        m.db.close()

    def test_delete_item_cascades_membership(self):
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        m.db.delete("a")
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"b"})
        m.db.close()

    def test_reaping_an_auto_row_cascades_membership(self):
        """The other statement that deletes a download row.

        There is no foreign key: the schema declares none, so the cascade is
        hand-written at each `DELETE FROM downloads`, and `delete_if_auto` --
        the reaper's -- had no test at all. A row deleted without its
        membership leaves `owned=1` over an item the catalog no longer has,
        which is a claim on whatever writes that row next.

        (An `ON DELETE CASCADE` is not the shortcut it looks like: sqlite
        implements `INSERT OR REPLACE` as delete-then-insert, so an ordinary
        `upsert` of an existing row would take the membership with it.)
        """
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        m.db.set_origin("a", "auto:nextup")
        self.assertIsNotNone(m.db.delete_if_auto("a"),
                             "the reap declined, so this test proves nothing")
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"b"})
        m.db.close()

    def test_reaping_an_auto_row_takes_its_watched_state_too(self):
        """The second site of the *other* cascade, and the reason it is
        stated separately: `delete_if_auto` does not route through `delete`,
        so a purge written only there leaves a reaped auto-download's
        watched state behind. Re-downloaded, the item comes back already
        watched and is reaped again on the next pass."""
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        m.db.update("a", status=STATUS_COMPLETE)
        m.db.set_watched("a", True, actor=(CONTENT_SERVER, "U1"))
        self.assertTrue(m.db.played_by_anyone("a"),
                        "the fixture never recorded a viewing")
        m.db.set_origin("a", "auto:nextup")
        self.assertIsNotNone(m.db.delete_if_auto("a"),
                             "the reap declined, so this test proves nothing")
        self.assertFalse(m.db.played_by_anyone("a"))
        m.db.close()

    def test_a_reap_that_declines_leaves_the_membership_alone(self):
        """The other half: `delete_if_auto` deletes nothing for a user row,
        so it must take no membership with it either."""
        jf = FakeJellyfin([pl_item("a"), pl_item("b")])
        m = make_manager(self.tmp, jf)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertIsNone(m.db.delete_if_auto("a"))
        self.assertEqual(m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER), {"a", "b"})
        m.db.close()


class OwnershipReadsRequireTheRowTest(TmpTest):
    """`owned=1` means "deleting this playlist may delete this file", so every
    reader of it has to require the file's row.

    The writer checks -- `replace_playlist_items` writes an entry only for an
    item the catalog has, inside the transaction -- but that only holds for
    rows *this build* wrote. A catalog written before it, or by an older build
    afterwards (`_migrate` promises those still open), still holds claims with
    no row, and the readers are what decide whether one is acted on.
    """

    def _catalog(self):
        db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(db.close)
        db.upsert_playlist("P", "srv", "uuid", "P")
        return db

    def _dangling(self, db, item_id="gone"):
        """A claim with no `downloads` row, written the only way a catalog can
        hold one: straight into the table, as an older build left it."""
        db._conn.execute(
            "INSERT INTO playlist_items (playlist_id, item_id, sort_index, "
            "owned) VALUES (?,?,?,1)", ("P", item_id, 0))
        db._conn.commit()

    def test_owned_ids_does_not_answer_with_a_claim_that_has_no_row(self):
        db = self._catalog()
        self._dangling(db)
        self.assertEqual(db.playlist_owned_ids("P", server_id=CONTENT_SERVER), set())

    def test_the_ownership_map_does_not_answer_with_one_either(self):
        db = self._catalog()
        self._dangling(db)
        self.assertEqual(db.playlist_ownership(), {})

    def test_a_claim_that_has_its_row_is_still_answered(self):
        """The join constrains; it must not filter. Ownership applies to a
        download that is still being fetched, so it cannot key on status --
        `_delete_playlist` and `_record_playlist` both read it mid-download.
        """
        db = self._catalog()
        db.upsert(make_row("done", status=STATUS_COMPLETE))
        db.upsert(make_row("busy", status=STATUS_PENDING))
        db.replace_playlist_items("P", [("done", 0, 1), ("busy", 1, 1)], server_id=CONTENT_SERVER)
        self.assertEqual(db.playlist_owned_ids("P", server_id=CONTENT_SERVER), {"done", "busy"})
        self.assertEqual(db.playlist_ownership(),
                         {"done": ("P", CONTENT_SERVER),
                          "busy": ("P", CONTENT_SERVER)})

    def test_an_unowned_membership_is_still_not_ownership(self):
        db = self._catalog()
        db.upsert(make_row("shared", status=STATUS_COMPLETE))
        db.replace_playlist_items("P", [("shared", 0, 0)], server_id=CONTENT_SERVER)
        self.assertEqual(db.playlist_owned_ids("P", server_id=CONTENT_SERVER), set())
        self.assertEqual(db.playlist_ownership(), {})


class ListPlaylistsTest(TmpTest):
    def test_only_playlists_with_complete_items_listed(self):
        db = SyncDB(os.path.join(self.tmp, "c.db"))
        db.upsert(make_row("a", status=STATUS_COMPLETE))
        db.upsert(make_row("b", status=STATUS_PENDING))
        db.upsert_playlist("P1", "srv", "uuid", "Has Complete")
        db.replace_playlist_items("P1", [("a", 0, 1)], server_id=CONTENT_SERVER)
        db.upsert_playlist("P2", "srv", "uuid", "Only Pending")
        db.replace_playlist_items("P2", [("b", 0, 1)], server_id=CONTENT_SERVER)
        names = {p["name"] for p in db.list_playlists()}
        self.assertEqual(names, {"Has Complete"})
        db.close()

    def test_playlist_item_rows_in_sort_order(self):
        db = SyncDB(os.path.join(self.tmp, "c.db"))
        db.upsert(make_row("a", name="A", status=STATUS_COMPLETE))
        db.upsert(make_row("b", name="B", status=STATUS_COMPLETE))
        db.upsert_playlist("P", "srv", "uuid", "P")
        # Deliberately record b before a, but with sort_index putting b last.
        db.replace_playlist_items("P", [("b", 1, 1), ("a", 0, 1)], server_id=CONTENT_SERVER)
        ids = [r["item_id"] for r in db.playlist_item_rows("P", server_id=CONTENT_SERVER)]
        self.assertEqual(ids, ["a", "b"])
        db.close()


class AnEmptyMembershipLeavesNoPlaylistTest(TmpTest):
    """"This playlist has nothing offline" is answered by the statement that
    filters the members, not by a caller looking at the list it is about to
    hand over.

    `replace_playlist_items` writes an entry only for an item the catalog
    has -- so it can write zero rows for a non-empty `entries`, and the
    caller's own `if not member_ids` never sees it. The `playlists` row is
    already committed by then, and `list_playlists` requires a member, so what
    is left is a record nothing lists, nothing deletes, and whose cached
    poster art no path reaches again.
    """

    @staticmethod
    def _records(db):
        """The `playlists` table itself. `list_playlists` requires a completed
        member, so it answers empty for a row that is merely invisible -- the
        very state this is about."""
        return {r[0] for r in db._conn.execute(
            "SELECT playlist_id FROM playlists")}

    def test_a_membership_that_filters_to_nothing_takes_the_record_with_it(self):
        db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(db.close)
        db.upsert_playlist("P", "srv", "uuid", "P")
        db.replace_playlist_items("P", [("no-such-row", 0, 1)], server_id=CONTENT_SERVER)
        self.assertEqual(db.list_playlists(), [])
        self.assertEqual(self._records(db), set(),
                         "an invisible playlist row survived the write that "
                         "found it has no members")

    def test_a_membership_that_writes_something_keeps_the_record(self):
        db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(db.close)
        db.upsert(make_row("a", status=STATUS_COMPLETE))
        db.upsert_playlist("P", "srv", "uuid", "P")
        db.replace_playlist_items("P", [("a", 0, 1), ("no-such-row", 1, 1)], server_id=CONTENT_SERVER)
        self.assertEqual(self._records(db), {"P"})
        self.assertEqual([r["item_id"] for r in db.playlist_item_rows("P", server_id=CONTENT_SERVER)],
                         ["a"])

    def test_a_delete_that_empties_the_playlist_mid_download(self):
        """Through the manager, on the ordering that produces it: every member
        loses its row between the snapshot `_record_playlist` decides from and
        the membership it writes."""
        jf = FakeJellyfin([pl_item("a")])
        m = make_manager(self.tmp, jf)
        self.addCleanup(m.db.close)
        real = m.db.replace_playlist_items

        def racing_replace(playlist_id, entries, *, server_id):
            m.db.delete("a")          # the other worker's delete lands here
            return real(playlist_id, entries, server_id=server_id)

        m.db.replace_playlist_items = racing_replace
        m.enqueue("uuid", "PL", "Playlist")

        self.assertIsNone(m.db.get("a"), "the racing delete did not land")
        self.assertEqual(m.db.list_playlists(), [])
        self.assertEqual(self._records(m.db), set(),
                         "the playlist record outlived every member it had")


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
        db.replace_playlist_items("P", [("m1", 0, 1), ("v1", 1, 1)], server_id=CONTENT_SERVER)
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
        # The offline id, not the catalog's: this library is one pseudo-server
        # and the browser routes a tile by its `Id`, so the server has to be in
        # it or two servers' same-named playlists are one tile.
        key = offline_playlist_id("P", CONTENT_SERVER)
        self.assertEqual([(p["Id"], p["Type"]) for p in pls],
                         [(key, "Playlist")])
        self.assertEqual([p.get("ServerId") for p in pls], [CONTENT_SERVER],
                         "the delete gesture reads the server off the DTO")
        items = src.get_playlist_items("offline", key)
        self.assertEqual([i["Id"] for i in items], ["m1", "v1"])  # playlist order

    def test_two_servers_of_one_name_are_told_apart_on_screen(self):
        """The id keeps them apart underneath; the *name* is what the user
        sees, and both servers call the playlist the same thing -- because the
        id is a hash of that name.

        Suffixed only where there is a duplicate: one server and a bare name is
        the normal case.
        """
        path = self._catalog()
        db = SyncDB(path)
        other = "9c75893ea3e942feb36f39670713b975"
        db.upsert(make_row("m2", type="Movie", name="Theirs",
                           content_server_id=other))
        db.upsert_playlist("P", other, "uuid-b", "Trip")
        db.replace_playlist_items("P", [("m2", 0, 1)], server_id=other)
        db.close()
        src = OfflineLibrarySource(path, server_name=lambda sid: {
            CONTENT_SERVER: "izzie-fileserver", other: "stdjflib"}.get(sid))
        pls, _sk = src.get_library_items("offline", "offline:playlists")
        self.assertEqual(sorted(p["Name"] for p in pls),
                         ["Trip (izzie-fileserver)", "Trip (stdjflib)"])

    def test_one_of_a_name_keeps_its_plain_name(self):
        src = OfflineLibrarySource(self._catalog(),
                                   server_name=lambda sid: "izzie-fileserver")
        pls, _sk = src.get_library_items("offline", "offline:playlists")
        self.assertEqual([p["Name"] for p in pls], ["Trip"],
                         "a playlist with no twin was qualified anyway")

    def test_a_server_nothing_can_name_leaves_the_name_alone(self):
        """A raw ServerId on a tile is worse than an ambiguous one, and the
        pair is still told apart by its contents and its art."""
        path = self._catalog()
        db = SyncDB(path)
        other = "9c75893ea3e942feb36f39670713b975"
        db.upsert(make_row("m2", type="Movie", name="Theirs",
                           content_server_id=other))
        db.upsert_playlist("P", other, "uuid-b", "Trip")
        db.replace_playlist_items("P", [("m2", 0, 1)], server_id=other)
        db.close()
        src = OfflineLibrarySource(path, server_name=lambda sid: None)
        pls, _sk = src.get_library_items("offline", "offline:playlists")
        self.assertEqual([p["Name"] for p in pls], ["Trip", "Trip"])

    def test_two_servers_are_two_tiles_with_their_own_items(self):
        """What the scoped id is for. A playlist id is a hash of its name, so
        two servers hand out the same one -- and keyed by it alone, whichever
        server's items were built last answered for both tiles."""
        path = self._catalog()
        db = SyncDB(path)
        other = "9c75893ea3e942feb36f39670713b975"
        db.upsert(make_row("m2", type="Movie", name="Theirs",
                           content_server_id=other))
        db.upsert_playlist("P", other, "uuid-b", "Trip")
        db.replace_playlist_items("P", [("m2", 0, 1)], server_id=other)
        db.close()
        src = OfflineLibrarySource(path)
        pls, _ = src.get_library_items("offline", "offline:playlists")
        keys = {p["Id"] for p in pls}
        self.assertEqual(keys, {offline_playlist_id("P", CONTENT_SERVER),
                                offline_playlist_id("P", other)},
                         "two servers' playlists collapsed into one tile")
        self.assertEqual(
            [i["Id"] for i in src.get_playlist_items(
                "offline", offline_playlist_id("P", other))],
            ["m2"], "the tile opened the other server's items")

    def test_no_playlist_tile_without_downloaded_members(self):
        db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        db.upsert(make_row("m1", type="Movie"))
        db.upsert_playlist("P", "srv", "uuid", "Empty")
        db.replace_playlist_items("P", [("ghost", 0, 1)], server_id=CONTENT_SERVER)  # not downloaded
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
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        class FakeSync:
            db = type("DB", (), {"path": catalog_path})()

        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))
        return PlayerGateway()

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

            def is_complete(self_inner, item_id, *, server_id):
                return item_id in ("m1", "m2")

        class FakeSync:
            db = FakeDB()

            # The manager facade the gateway actually calls: it takes a
            # saved login and translates it to the catalog's content key.
            # Modelled here rather than skipped, because a fake missing the
            # method the code under test calls is how the path stops being
            # exercised while the test still reports a pass.
            # No default, as the real facade has none: one here would let a
            # caller that forgot the scope pass against the double and fail in
            # production. The uuid is *translated* rather than discarded, so
            # the scoping path is exercised instead of bypassed -- the
            # downloads screen browses the "offline" pseudo-server, which
            # `content_id_for` turns into the unscoped ask.
            def is_complete(self_inner, item_id, server_uuid):
                return self_inner.db.is_complete(
                    item_id, server_id=SyncManager.content_id_for(server_uuid))

        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))

        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        calls = []
        self._patched_start_playback(calls)
        ctl = PlayerGateway()

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


class OrphanedItemsGetTheirOwnLibraryTest(TmpTest):
    """A download whose content server is unknown, in the offline library.

    Its manifest could not be read, so nothing can show whose content it is —
    and since step 5 no *scoped* read answers for it. Without a home of its own
    it would be listed nowhere at all: visible in the downloads manager and
    invisible in the library it is a part of.

    [iw]'s ruling: *"the items should probably be displayed under an 'orphaned
    items' category in the browser."*
    """

    def _catalog(self, orphan=True):
        path = os.path.join(self.tmp, "catalog.db")
        db = SyncDB(path)
        db.upsert(make_row("m1", type="Movie", name="Homed"))
        # A Book on both sides of the split. The shelf this branch added is
        # the fifth site of the rule below, and a fixture that seeds only a
        # Movie and an Episode cannot fail on the path it does not model --
        # which is how the site was missed.
        db.upsert(make_row("b1", type="Book", name="Homed Book"))
        db.upsert(make_row("e1", type="Episode", name="Ep", series_id="s1",
                           series_name="Show",
                           item_json=json.dumps({"Id": "e1", "Name": "Ep",
                                                 "Type": "Episode",
                                                 "SeriesId": "s1",
                                                 "SeriesName": "Show"})))
        if orphan:
            db.upsert(make_row("m2", type="Movie", name="Unhomed",
                               content_server_id=None))
            db.upsert(make_row("b2", type="Book", name="Orphan Book",
                               content_server_id=None))
            db.upsert(make_row("e2", type="Episode", name="Orphan Ep",
                               content_server_id=None, series_id="s2",
                               series_name="Other Show",
                               item_json=json.dumps({"Id": "e2",
                                                     "Type": "Episode",
                                                     "SeriesId": "s2",
                                                     "SeriesName":
                                                     "Other Show"})))
        db.close()
        return path

    def test_the_library_appears_only_when_there_is_an_orphan(self):
        with_orphan = OfflineLibrarySource(self._catalog(orphan=True))
        self.assertIn("offline:orphans",
                      {l["Id"] for l in with_orphan.get_libraries("offline")})
        shutil.rmtree(self.tmp, ignore_errors=True)
        os.makedirs(self.tmp, exist_ok=True)
        without = OfflineLibrarySource(self._catalog(orphan=False))
        self.assertNotIn("offline:orphans",
                         {l["Id"] for l in without.get_libraries("offline")},
                         "an empty category on every ordinary install")

    def test_it_lists_the_unhomed_downloads(self):
        src = OfflineLibrarySource(self._catalog())
        items, _sk = src.get_library_items("offline", "offline:orphans")
        self.assertEqual({i["Id"] for i in items}, {"m2", "e2", "b2"})

    def test_an_orphan_is_in_exactly_one_category(self):
        """The guard that matters. **Five** places partition the library by
        type and they all go through one filter; a rule applied at four of
        them lists an orphan twice, which is this repository's recurring
        defect.

        The count was four and the fifth -- the books shelf -- was added on
        this branch without going through the filter, while this docstring
        went on saying four and the fixture went on seeding only a Movie and
        an Episode. A census in a guard's own text is a claim; keep it true
        or the guard stops being one.
        """
        src = OfflineLibrarySource(self._catalog())
        seen = {}
        for lib in src.get_libraries("offline"):
            items, _sk = src.get_library_items("offline", lib["Id"])
            for item in items:
                # A Series tile is synthesized per series, so count what it
                # contains rather than the tile.
                if item.get("Type") == "Series":
                    kids, _k = src.get_library_items("offline", item["Id"])
                    for kid in kids:
                        seen.setdefault(kid["Id"], []).append(lib["Id"])
                    continue
                seen.setdefault(item["Id"], []).append(lib["Id"])
        for item_id in ("m2", "e2", "b2"):
            self.assertEqual(seen.get(item_id), ["offline:orphans"],
                             "%s is listed in %s" % (item_id, seen.get(item_id)))

    def test_the_homed_items_keep_their_own_categories(self):
        """The control: a filter that dropped everything would pass above."""
        src = OfflineLibrarySource(self._catalog())
        movies, _sk = src.get_library_items("offline", "offline:movies")
        self.assertEqual({i["Id"] for i in movies}, {"m1"})
        series, _sk = src.get_library_items("offline", "offline:tv")
        self.assertEqual({i["Id"] for i in series}, {"s1"},
                         "the orphaned episode's series was listed too")
        books, _sk = src.get_library_items("offline", "offline:books")
        self.assertEqual({i["Id"] for i in books}, {"b1"},
                         "the books shelf dropped everything, or kept the "
                         "orphan")

    def test_the_home_rows_leave_them_out(self):
        src = OfflineLibrarySource(self._catalog())
        rows = {r["title"]: [i["Id"] for i in r["items"]]
                for r in src.get_home_rows("offline")}
        self.assertEqual(rows.get("Downloaded Movies"), ["m1"])


class TestOfflinePlaylistArt(TmpTest):
    """A playlist tile uses the playlist's OWN poster, cached at download
    time. It used to borrow a member's, so a playlist whose first member
    had no poster.jpg on disk showed a bare letter glyph.

    **A real ServerId here, not this module's `CONTENT_SERVER`.** The art
    directory is a filesystem path built from a value the server supplies, so
    `playlist_art_dir` refuses anything that is not a plain id -- and `"srv"`
    is not hex, so it lands in the `unscoped` directory and the scoped path
    never gets exercised at all.
    """

    #: The QA 12.0 container's real one.
    PL_SERVER = "0ccef36552284944ab0d183114fcbe92"

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
        # One scope for the row and its membership: `list_playlists` correlates
        # the two, so a playlist whose members sit on another scope is not
        # listed at all.
        db.upsert_playlist("P", self.PL_SERVER, "uuid", "Mine")
        db.replace_playlist_items("P", [("m1", 0, 1), ("m2", 1, 1)],
                                  server_id=self.PL_SERVER)
        db.close()
        if playlist_art:
            # `<root>/server/playlist/<content server>/<playlist>/`, through
            # the helper the writer and the reader now share
            # (`db.playlist_art_dir`) rather than spelled out here: a fixture
            # holding its own copy of the layout is how a path change becomes
            # a tile that silently stops drawing. `server` stays a literal --
            # `downloads.server_id` is NULL on every row, so the whole store
            # is one directory; the *content* server scopes the poster below
            # `playlist/`, because two servers can hold this id.
            pl_dir = playlist_art_dir(self.tmp, self.PL_SERVER, "P")
            os.makedirs(pl_dir, exist_ok=True)
            with open(os.path.join(pl_dir, "poster.jpg"), "wb") as fh:
                fh.write(b"jpeg")
        return path

    def test_uses_the_playlists_own_poster(self):
        src = OfflineLibrarySource(self._catalog(playlist_art=True))
        # Asked with the offline id, which is what a tile holds; the directory
        # on disk is named with the real one, so this also pins the round trip.
        key = offline_playlist_id("P", self.PL_SERVER)
        self.assertIsNotNone(src.image_spec({"Id": key, "Type": "Playlist"}))
        self.assertTrue(
            src.image_url("offline", key, "Primary", "offline", 100)
            .endswith(os.path.join("server", "playlist", self.PL_SERVER, "P",
                                   "poster.jpg")))

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
    the playlist's own poster to show.

    A real ServerId for the reason `TestOfflinePlaylistArt` gives: a non-hex
    one lands in the `unscoped` directory, and the writer's scoped path is then
    never exercised -- the fixture and the assertion would agree by both being
    wrong.
    """

    PL_SERVER = "0ccef36552284944ab0d183114fcbe92"

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
        # include_apikey mirrors the apiclient signature; the sync manager
        # passes False and sends the token as a header instead.
        jf.artwork = (lambda item_id, kind, size, include_apikey=True:
                      "http://s/%s/%s" % (item_id, kind))
        m = make_manager(self.tmp, jf)
        self.addCleanup(m.db.close)
        return m

    def _poster(self):
        return os.path.join(playlist_art_dir(self.tmp, self.PL_SERVER, "P"),
                            "poster.jpg")

    def test_downloads_the_playlists_own_primary(self):
        fetched = []
        m = self._manager(fetched)
        m._download_playlist_art(m.get_client("uuid"), self.PL_SERVER,
                                 "P")
        self.assertEqual(fetched, ["http://s/P/Primary"])
        self.assertTrue(os.path.exists(self._poster()))

    def test_is_skipped_when_already_cached(self):
        fetched = []
        m = self._manager(fetched)
        m._download_playlist_art(m.get_client("uuid"), self.PL_SERVER,
                                 "P")
        m._download_playlist_art(m.get_client("uuid"), self.PL_SERVER,
                                 "P")
        self.assertEqual(len(fetched), 1, "re-fetched a cached poster")

    def test_a_playlist_without_art_is_not_fatal(self):
        """Most playlists have no image; that must not fail the download."""
        fetched = []
        m = self._manager(fetched, status=404)
        m._download_playlist_art(m.get_client("uuid"), self.PL_SERVER,
                                 "P")
        self.assertFalse(os.path.exists(self._poster()))


class TestDeleteScope(TmpTest):
    """An unscoped delete used to mean "delete the entire catalog", which
    the downloads manager reached by simply not passing a scope for its
    flat Movies group — behind a prompt naming only that group."""

    def _manager(self):
        jf = FakeJellyfin([])
        m = make_manager(self.tmp, jf)
        self.addCleanup(m.db.close)
        for item in ("m1", "m2", "e1"):
            m.db.upsert(make_row(item, type="Movie", server_id="srv"))
        return m

    def test_unscoped_delete_removes_nothing(self):
        m = self._manager()
        m.delete()
        self.assertEqual(len(m.db.list()), 3, "wiped the catalog")

    def test_scoped_deletes_still_work(self):
        m = self._manager()
        m.delete(item_id="m1")
        self.assertEqual({r["item_id"] for r in m.db.list()}, {"m2", "e1"})

    def test_watched_sweep_must_be_explicit(self):
        """The one legitimate catalog-wide delete asks for it by name."""
        m = self._manager()
        m.delete(watched_only=True)
        self.assertEqual(len(m.db.list()), 3, "no scope, no sweep")


class TestOfflineWatchedQueue(TmpTest):
    """Marking watched offline used to return silently while the UI showed
    an optimistic tick — it reverted on reload and never reached the
    server. It now queues into the catalog for later replay."""

    def _controller(self, catalog_path):
        import jellyfin_mpv_shim.sync.manager as mgr
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        db = SyncDB(catalog_path)
        self.addCleanup(db.close)

        # A REAL SyncManager over this catalog, and a machine with no saved
        # credential at all. `actor_of` was modelled here and answered the
        # unattributed sentinel by construction, which is the shape that hid
        # three findings in the 2026-09-12 round: the double agreed with
        # what the class was named after instead of deciding it. With no
        # credentials the real resolver reaches the same answer, and now it
        # is the resolver saying so. [iw]'s ruling is that such a viewing is
        # recorded locally and never queued -- there is no account to send
        # it as -- so the pending queue staying empty below is the contract,
        # not a miss.
        from jellyfin_mpv_shim.users import userManager
        for attr, value in (("users", []), ("active_id", None)):
            patch = mock.patch.object(userManager, attr, value)
            self.addCleanup(patch.stop)
            patch.start()
        manager = mgr.SyncManager.__new__(mgr.SyncManager)
        manager.db = db
        patch = mock.patch.object(mgr, "syncManager", manager)
        self.addCleanup(patch.stop)
        patch.start()
        return PlayerGateway(), db

    def _catalog(self):
        path = os.path.join(self.tmp, "catalog.db")
        db = SyncDB(path)
        db.upsert(make_row("e1", type="Episode", series_id="sh1",
                           season_id="s1", server_uuid="uuid"))
        db.upsert(make_row("e2", type="Episode", series_id="sh1",
                           season_id="s1", server_uuid="uuid"))
        db.close()
        return path

    def _played(self, db, item_id):
        """Read the mark back from where it now lives: the per-actor table,
        under the unattributed actor, because browsing offline names
        nobody."""
        return db.userdata(item_id, actor=(CONTENT_SERVER, NO_ACTOR))["played"]

    def test_marking_an_item_watched_offline_is_queued(self):
        ctl, db = self._controller(self._catalog())
        self.assertTrue(ctl.set_watched("offline", "e1", True))
        self.assertTrue(self._played(db, "e1"))
        self.assertEqual(db.list_playstate(), [],
                         "a viewing nobody can be named for must be "
                         "recorded locally and never queued")

    def test_a_series_fans_out_to_its_downloaded_episodes(self):
        ctl, db = self._controller(self._catalog())
        self.assertTrue(ctl.set_watched("offline", "sh1", True))
        for eid in ("e1", "e2"):
            self.assertTrue(self._played(db, eid), "%s not marked" % eid)

    def test_unwatching_offline_is_refused_rather_than_half_applied(self):
        """The pending queue is advance-only, so un-watching can't be
        represented — say so instead of pretending it worked."""
        ctl, db = self._controller(self._catalog())
        self.assertFalse(ctl.set_watched("offline", "e1", False))

    def test_an_item_with_nothing_downloaded_reports_failure(self):
        ctl, _db = self._controller(self._catalog())
        self.assertFalse(ctl.set_watched("offline", "nope", True))


if __name__ == "__main__":
    unittest.main()


class PlaylistContentScopeTest(TmpTest):
    """The playlist row's content server, through the real store.

    Every other fixture here puts the server identity in the client's config
    under the same value `content_id_for` returns, so which of the two
    `_record_playlist` is handed cannot matter and the scope is untested.
    **Production supplies neither** -- the apiclient never assigns that key
    (`docs/do-not-fix.md` 1), so every playlist row ever written carried a NULL
    server.

    These use a config without it, which is what a real client has.
    """

    @staticmethod
    def _production_client(jf):
        client = FakeClient(jf)
        # The real shape: the apiclient never assigns that key.
        client.config.data = {"auth.server": "http://s"}
        return client

    def _manager(self, jf):
        m = make_manager(self.tmp, jf)
        m.get_client = lambda uuid: self._production_client(jf)
        return m

    def test_the_row_carries_the_server_the_login_speaks_for(self):
        m = self._manager(FakeJellyfin([pl_item("a")]))
        self.addCleanup(m.db.close)
        m.enqueue("uuid", "PL", "Playlist")
        # `list_playlists` only answers for a playlist with a *finished*
        # member, so the queue alone is not enough to make the row visible.
        m.db.update("a", status=STATUS_COMPLETE)
        rows = m.db.list_playlists()
        self.assertEqual([r["playlist_id"] for r in rows], ["PL"])
        self.assertEqual(CONTENT_SERVER, rows[0]["server_id"])

    def test_a_playlist_from_one_server_is_not_listed_on_another(self):
        """The badge finding, end to end and through the real query.

        `list_playlists` admits a NULL server on every scope deliberately -- an
        unattributable row cannot be shown to be somebody else's -- so a row
        written with no server ticks a tile on every server there is.
        """
        m = self._manager(FakeJellyfin([pl_item("a")]))
        self.addCleanup(m.db.close)
        m.enqueue("uuid", "PL", "Playlist")
        m.db.update("a", status=STATUS_COMPLETE)
        self.assertEqual(
            [r["playlist_id"] for r in m.db.list_playlists(CONTENT_SERVER)],
            ["PL"], "its own server must still see it")
        self.assertEqual(
            [], m.db.list_playlists("some-other-server"),
            "a playlist held from one server must not appear on another")


class AnUnresolvableLoginDoesNotUnscopeThePlaylistTest(TmpTest):
    """**The playlist row's scope comes from its items, not from the login.**

    Ruled 2026-09-19: a playlist is its id plus the content server its items
    came from, an unknown scope is the NULL row rather than every server, and
    nothing moves a row between scopes on a guess.

    What it replaces: `enqueue` keyed the playlist on `content_id_for(login)`
    while every member row carried the item's own `ServerId`. `content_id_for`
    answers `ANY_SERVER` when the registry lookup **raises** -- present but
    unreadable, which is a state the Servers tab has its own warning for -- and
    `ANY_SERVER` reads as "every server" while the three playlist writers fold
    it to the NULL row. So the read and the writes were about different rows.
    """

    OTHER = "other-server"

    def _away(self, m, *item_ids, row_server=None):
        """A second server's playlist, same id, holding its own items.

        A playlist id is a hash of its *name* (R26), so this is the ordinary
        case rather than a contrived one.

        ``row_server`` is the **download row's** content server, which is not
        always the membership row's: a membership row takes its *playlist's*
        scope, and `playlist_owned_ids` joins `downloads` on the item id
        alone. Defaulting it to `OTHER` would make every caller collide with
        `claim_identity`, which refuses a row that names another server
        (F43's door) -- a different and correct guard, and not this one.
        """
        for iid in item_ids:
            m.db.upsert(make_row(
                iid, content_server_id=row_server or self.OTHER))
        m.db.upsert_playlist("PL", self.OTHER, "uuid-away", "Example Playlist")
        m.db.replace_playlist_items(
            "PL", [(iid, i, 1) for i, iid in enumerate(item_ids)],
            server_id=self.OTHER)

    @staticmethod
    def _membership(m, item_id):
        """Every membership row for one item, as (server_id, owned).

        Read straight from the table because neither public reader can answer
        this: `playlist_item_rows` filters on a COMPLETE download and drops a
        row this enqueue only queued, and `playlist_ownership` is deliberately
        unscoped and keeps one owner per item -- so with two rows claiming one
        item it answers whichever the scan reaches first.
        """
        return [(r[0], r[1]) for r in m.db._conn.execute(
            "SELECT server_id, owned FROM playlist_items "
            "WHERE playlist_id=? AND item_id=? ORDER BY server_id IS NULL",
            ("PL", item_id))]

    def _broken_registry(self, jf):
        m = make_manager(self.tmp, jf)
        # The `except Exception` arm of `content_id_for`: the registry is what
        # failed, not the lookup.
        m.content_id_for = staticmethod(lambda uuid: ANY_SERVER)
        return m

    def test_the_playlist_is_recorded_at_its_items_server(self):
        jf = FakeJellyfin([pl_item("a")])
        m = self._broken_registry(jf)
        self.addCleanup(m.db.close)
        self._away(m, "away-film")
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(
            {"a"}, m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER),
            "the playlist just downloaded is invisible at its own scope")

    def test_and_its_membership_is_not_left_on_the_unscoped_row(self):
        """Where it actually landed. `list_playlists` does not show the NULL
        row here, so asserting through it measured nothing -- the membership
        table is where the split is visible.
        """
        jf = FakeJellyfin([pl_item("a")])
        m = self._broken_registry(jf)
        self.addCleanup(m.db.close)
        self._away(m, "away-film")
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(
            [(CONTENT_SERVER, 1)], self._membership(m, "a"),
            "an unresolvable login put the membership on the NULL row while "
            "the item's own download row names a server")

    def test_a_playlist_that_becomes_empty_still_loses_its_record(self):
        """**The case with no member to take a scope from.**

        `_record_playlist` deletes the row when nothing is left, so that an
        emptied playlist does not linger in the offline UI -- and the delete
        has to name the row the *last* download wrote. With no members there
        is no member DTO to read a server off, so the login's server is the
        only thing that still names it.

        Found by re-reading the scope change rather than by a review: taking
        the scope only from the members made this delete miss, silently, on
        the one path that has no members by construction.
        """
        jf = FakeJellyfin([pl_item("a")])
        m = make_manager(self.tmp, jf)     # an ordinary, resolvable login
        self.addCleanup(m.db.close)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(
            {"a"}, m.db.playlist_owned_ids("PL", server_id=CONTENT_SERVER),
            "the fixture did not record a playlist to empty")

        jf._items = []                     # emptied on the server
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual([], m.db.list_playlists(),
                         "the emptied playlist still lingers in the offline "
                         "library, with nothing in it")

    def test_and_the_other_servers_playlist_is_left_alone(self):
        jf = FakeJellyfin([pl_item("a")])
        m = self._broken_registry(jf)
        self.addCleanup(m.db.close)
        self._away(m, "away-film")
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(
            {"away-film"}, m.db.playlist_owned_ids("PL", server_id=self.OTHER),
            "the other server's playlist lost its own membership")

    def test_ownership_is_not_computed_from_another_servers_claims(self):
        """The sharp end of it. `already_owned` read unscoped, so an item the
        **other** server's playlist owns came back as "we already own this" --
        and the item then kept `owned=1` under a playlist that never pulled it
        down. Deleting that playlist would take the other server's file.
        """
        jf = FakeJellyfin([pl_item("shared")])
        m = self._broken_registry(jf)
        self.addCleanup(m.db.close)
        # The download row is **this** server's -- otherwise `claim_identity`
        # refuses the item outright and the enqueue never reaches the
        # ownership question. What the other server holds is a membership row
        # claiming it, which `playlist_owned_ids` answers for because it joins
        # `downloads` on the item id alone.
        self._away(m, "shared", row_server=CONTENT_SERVER)
        m.enqueue("uuid", "PL", "Playlist")
        self.assertEqual(
            [(self.OTHER, 1), (CONTENT_SERVER, 0)], self._membership(m, "shared"),
            "this playlist claimed an item it did not pull down -- the claim "
            "came from the other server's, read unscoped")
