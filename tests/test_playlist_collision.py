"""Two servers, one playlist id — the collision Jellyfin actually produces.

A playlist id is a hash of its **name**, not of its path. `Example Playlist`
created on the QA 10.11 container came back
`cf0ce7fc72247deaa755cc40b9219e0d`, byte for byte the id a real personal
server had already handed out for its own `Example Playlist`. The paths differ
(`/config/data/playlists/Example Playlist`) and `md5(type+path)` does not
reproduce it. Measured 2026-09-18;
docs/offline-sync.md section 4.

With `playlists.playlist_id TEXT PRIMARY KEY` the consequence was silent and
total: downloading the same-named playlist from a second server **overwrote**
the first server's name, membership and ownership, and a delete of either took
both. The id below is that measured one rather than an invented fixture,
because the whole entry exists to say this is not hypothetical.

What this module does not cover, deliberately: the offline *library* still
routes a playlist tile by its DTO `Id`, so two entries sharing one are not yet
reachable as two there. See the note in `repository.py`'s snapshot build.
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
import sqlite3
import tempfile
import unittest

from jellyfin_mpv_shim.constants import (offline_playlist_id,
                                       split_offline_playlist_id)
from jellyfin_mpv_shim.sync.db import (ANY_SERVER, COLUMNS, STORE_DIR,
                                       STATUS_COMPLETE, SyncDB, item_dir,
                                       legacy_playlist_art_dir,
                                       playlist_art_dir, season_art_dir,
                                       series_art_dir)

#: The real one. Both servers hand this out for a playlist called
#: "Example Playlist".
COLLIDING = "cf0ce7fc72247deaa755cc40b9219e0d"

HOME = "0ccef36552284944ab0d183114fcbe92"      # the QA 12.0 container
AWAY = "9c75893ea3e942feb36f39670713b975"      # the QA 10.11 container


def _row(item_id, server_id):
    row = {c: None for c in COLUMNS}
    row.update({"item_id": item_id, "content_server_id": server_id,
                "status": STATUS_COMPLETE, "type": "Movie", "name": item_id,
                "file_path": "%s/f.mkv" % item_id,
                "item_json": json.dumps({"Id": item_id})})
    return row


class TwoServersOnePlaylistIdTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        # One item each, so "whose members are these" has an answer.
        self.db.upsert(_row("home-film", HOME))
        self.db.upsert(_row("away-film", AWAY))
        self.db.upsert_playlist(COLLIDING, HOME, "uuid-home", "Example Playlist")
        self.db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                      server_id=HOME)
        self.db.upsert_playlist(COLLIDING, AWAY, "uuid-away", "Example Playlist")
        self.db.replace_playlist_items(COLLIDING, [("away-film", 0, 1)],
                                      server_id=AWAY)

    def _servers(self):
        # Sorted with a key: a NULL scope is one of the values here.
        return sorted((r["server_id"] for r in self.db.list_playlists()),
                      key=lambda s: s or "")

    def test_both_rows_survive_the_second_download(self):
        """The whole finding: the second `upsert_playlist` used to replace the
        first, because REPLACE resolved against `playlist_id` alone."""
        self.assertEqual(self._servers(), sorted([HOME, AWAY]))

    def test_each_server_sees_only_its_own(self):
        self.assertEqual([r["server_id"] for r in
                          self.db.list_playlists(HOME)], [HOME])
        self.assertEqual([r["server_id"] for r in
                          self.db.list_playlists(AWAY)], [AWAY])

    def test_membership_does_not_merge(self):
        """Keyed on the playlist id alone, one membership list served both and
        each download's `replace` wiped the other's."""
        self.assertEqual(
            [r["item_id"] for r in
             self.db.playlist_item_rows(COLLIDING, server_id=HOME)],
            ["home-film"])
        self.assertEqual(
            [r["item_id"] for r in
             self.db.playlist_item_rows(COLLIDING, server_id=AWAY)],
            ["away-film"])

    def test_ownership_is_not_shared(self):
        """`owned` decides whether deleting the playlist may delete the file,
        so a shared answer here is a delete reaching another server's item."""
        self.assertEqual(
            self.db.playlist_owned_ids(COLLIDING, server_id=HOME),
            {"home-film"})
        self.assertEqual(
            self.db.playlist_owned_ids(COLLIDING, server_id=AWAY),
            {"away-film"})

    def test_deleting_one_leaves_the_other_intact(self):
        self.db.delete_playlist(COLLIDING, server_id=HOME)
        self.assertEqual(self._servers(), [AWAY])
        self.assertEqual(
            [r["item_id"] for r in
             self.db.playlist_item_rows(COLLIDING, server_id=AWAY)],
            ["away-film"], "the other server's membership went with it")

    def test_the_ownership_map_tells_them_apart(self):
        """The Downloads screen groups by this, and it has to be able to say
        which of two same-named playlists an item belongs under."""
        self.assertEqual(self.db.playlist_ownership(),
                         {"home-film": (COLLIDING, HOME),
                          "away-film": (COLLIDING, AWAY)})

    def test_an_unscoped_row_is_its_own_row_and_not_a_wildcard(self):
        """NULL means "could not tell", which `list_playlists` admits on every
        scope -- but it is still one row, so a third write must not multiply
        it. `INSERT OR REPLACE` against a plain composite key would, because
        SQLite treats two NULLs as distinct."""
        self.db.upsert_playlist(COLLIDING, None, "uuid-x", "Example Playlist")
        self.db.replace_playlist_items(COLLIDING, [("home-film", 0, 0)],
                                      server_id=None)
        self.db.upsert_playlist(COLLIDING, None, "uuid-x", "Example Playlist")
        self.assertEqual(self._servers(),
                         sorted([HOME, AWAY, None], key=lambda s: s or ""))


class WhatAnAuditFoundTest(unittest.TestCase):
    """Three consequences of the scoped key that its author did not predict.

    All three were confirmed by running them before they were fixed, and all
    three are the same shape: the playlist row's scope and something else's
    disagreed.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "catalog.db")

    def test_a_member_with_no_server_does_not_hide_its_siblings(self):
        """The migration used to give each membership row **its own item's**
        server, which read as more precise and was incoherent.

        One member with no `content_server_id` -- an adopted orphan, or a row
        the content-key backfill could not read a manifest for -- makes the
        members disagree, so the playlist stays NULL; the members that *do*
        name a server then answered at a scope the playlist does not have, so
        they vanished from it. Their files were never deleted with it either,
        and their `owned=1` rows outlived it.
        """
        db = SyncDB(self.path)
        db.upsert(_row("homed", HOME))
        db.upsert(_row("orphan", None))
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE playlists")
        conn.execute("DROP TABLE playlist_items")
        conn.execute("CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, "
                     "server_id TEXT, server_uuid TEXT, name TEXT, "
                     "added_at INTEGER)")
        conn.execute("CREATE TABLE playlist_items (playlist_id TEXT, "
                     "item_id TEXT, sort_index INTEGER, "
                     "owned INTEGER DEFAULT 0, "
                     "PRIMARY KEY (playlist_id, item_id))")
        conn.execute("INSERT INTO playlists VALUES (?,?,?,?,?)",
                     (COLLIDING, None, "uuid-home", "Mixed", 1000))
        conn.execute("INSERT INTO playlist_items VALUES (?,?,?,?)",
                     (COLLIDING, "homed", 0, 1))
        conn.execute("INSERT INTO playlist_items VALUES (?,?,?,?)",
                     (COLLIDING, "orphan", 1, 1))
        conn.commit()
        conn.close()

        db = SyncDB(self.path)
        self.addCleanup(db.close)
        scope = db.list_playlists()[0]["server_id"]
        self.assertIsNone(scope, "the members disagree, so the scope is NULL")
        self.assertEqual(
            sorted(r["item_id"] for r in
                   db.playlist_item_rows(COLLIDING, server_id=scope)),
            ["homed", "orphan"],
            "a member vanished from the playlist it belongs to")
        self.assertEqual(
            db.playlist_owned_ids(COLLIDING, server_id=scope),
            {"homed", "orphan"},
            "deleting the playlist would leave this file behind for good")

    def test_a_catalog_migrated_by_the_broken_version_is_repaired(self):
        """The fix above is forward-looking on its own: the index marker stops
        the rebuild re-running, so a catalog that came through the broken
        migration would keep its drifted membership rows forever. The repair
        is unconditional and WHERE-guarded -- the shape the `origin` backfill
        uses -- so it reaches one and is a no-op on everything else.
        """
        db = SyncDB(self.path)
        db.upsert(_row("home-film", HOME))
        db.upsert_playlist(COLLIDING, None, "uuid-x", "Mixed")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=None)
        db.close()
        # Exactly what the broken migration left: the member on its item's
        # server while its playlist is unscoped.
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE playlist_items SET server_id=?", (HOME,))
        conn.commit()
        conn.close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(
            [r["item_id"] for r in
             db.playlist_item_rows(COLLIDING, server_id=None)],
            ["home-film"], "the drift survived the open that should fix it")

    def test_the_repair_does_not_merge_two_servers_playlists(self):
        """**The repair correlates on `playlist_id` alone.** With two
        `playlists` rows for one id -- the ordinary case here, since a
        playlist id hashes its name -- its scalar subquery returns whichever
        row SQLite yields first and every membership row is rewritten to it.

        Reproduced by the reviewer before it was fixed: A's members became
        both films, B's none, `playlist_owned_ids(pid, A)` returned B's item,
        and deleting A's playlist therefore deleted B's download off disk.
        It ran on **every** open, so a catalog healed by hand re-broke on the
        next launch.

        The repair's own job is real and is not being removed: a membership
        row carrying anything but its playlist's scope is invisible, with its
        file undeletable through the playlist. It simply may not guess which
        playlist a row belongs to when the id names more than one.
        """
        db = SyncDB(self.path)
        db.upsert(_row("home-film", HOME))
        db.upsert(_row("away-film", AWAY))
        db.upsert_playlist(COLLIDING, HOME, "uuid-home", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=HOME)
        db.upsert_playlist(COLLIDING, AWAY, "uuid-away", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("away-film", 0, 1)],
                                  server_id=AWAY)
        db.close()

        db = SyncDB(self.path)          # the repair runs here
        self.addCleanup(db.close)
        self.assertEqual(
            ["home-film"], [r["item_id"] for r in
                            db.playlist_item_rows(COLLIDING, server_id=HOME)])
        self.assertEqual(
            ["away-film"], [r["item_id"] for r in
                            db.playlist_item_rows(COLLIDING, server_id=AWAY)])
        self.assertEqual(
            {"home-film"}, db.playlist_owned_ids(COLLIDING, server_id=HOME),
            "deleting this playlist would now delete the other server's file")

    def test_the_repair_still_fixes_a_drifted_row_when_the_id_is_unambiguous(self):
        """The negative control, and the reason this is not just a deletion.
        One playlist row for the id: the repair still has to act, or the
        member stays invisible with its file undeletable through it.
        """
        db = SyncDB(self.path)
        db.upsert(_row("home-film", HOME))
        db.upsert_playlist(COLLIDING, HOME, "uuid-home", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=HOME)
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("UPDATE playlist_items SET server_id=?", (AWAY,))
        conn.commit()
        conn.close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(
            ["home-film"], [r["item_id"] for r in
                            db.playlist_item_rows(COLLIDING, server_id=HOME)],
            "the drift survived the open that should fix it")

    def test_an_emptied_playlist_is_not_listed_on_its_own_server(self):
        """`list_playlists`' EXISTS was not correlated on the server, so one
        server's row was listed on the strength of the *other* server's
        complete member -- an empty group in the Downloads manager, and a
        ticked Playlist tile on a server holding nothing of it.

        Reachable without any migration: hold the same-named playlist on two
        servers and delete one side's items. `db.delete` removes membership
        and never the now-empty `playlists` row.
        """
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        db.upsert(_row("home-film", HOME))
        db.upsert_playlist(COLLIDING, HOME, "uuid-home", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=HOME)
        db.upsert(_row("away-film", AWAY))
        db.upsert_playlist(COLLIDING, AWAY, "uuid-away", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("away-film", 0, 1)],
                                  server_id=AWAY)
        db.delete("away-film")
        self.assertEqual([r["server_id"] for r in db.list_playlists(AWAY)], [],
                         "a playlist with no members of its own was listed")
        self.assertEqual([r["server_id"] for r in db.list_playlists(HOME)],
                         [HOME], "and the one that does have members lost it")

    def test_an_unscoped_row_is_still_listed_on_every_scope(self):
        """The control for the correlation above: `IS`, not `=`. A NULL-unsafe
        comparison would drop every unscoped playlist from every scope, which
        is the opposite of what NULL means here."""
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        db.upsert(_row("home-film", HOME))
        db.upsert_playlist(COLLIDING, None, "uuid-x", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=None)
        self.assertEqual([r["server_id"] for r in db.list_playlists(HOME)],
                         [None])
        self.assertEqual([r["server_id"] for r in db.list_playlists(AWAY)],
                         [None])

    def test_the_unscoped_sentinel_reaches_none_of_these_as_a_parameter(self):
        """`ANY_SERVER` is truthy and sqlite has no adapter for it. The write
        pair raised; `playlist_item_rows` did **not** -- it went through
        `_query`, which logs and answers with nothing, so the playlist read as
        empty. The same trap `upsert_playlist` already carries a test for,
        two methods along.
        """
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        db.upsert(_row("home-film", HOME))
        db.upsert_playlist(COLLIDING, ANY_SERVER, "uuid-x", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=ANY_SERVER)
        # A read with the sentinel asks unscoped, which is what it means.
        self.assertEqual(
            [r["item_id"] for r in
             db.playlist_item_rows(COLLIDING, server_id=ANY_SERVER)],
            ["home-film"])
        self.assertEqual(
            db.playlist_owned_ids(COLLIDING, server_id=ANY_SERVER),
            {"home-film"})
        # The write folded it to NULL, as `upsert_playlist` does, so all three
        # wrote the same row.
        self.assertEqual([r["server_id"] for r in db.list_playlists()], [None])
        # A second, *scoped* row before the delete -- without it this cannot
        # tell "folded to NULL" from "deleted every row for this id", and the
        # mutation round said so.
        db.upsert_playlist(COLLIDING, HOME, "uuid-home", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                  server_id=HOME)
        db.delete_playlist(COLLIDING, server_id=ANY_SERVER)
        self.assertEqual([r["server_id"] for r in db.list_playlists()], [HOME],
                         "the sentinel delete took a row it does not name")


class TheCachedArtMovesUnderItsServerTest(unittest.TestCase):
    """R23: *"Moving is fine, we should just make it transactional so it
    doesn't strand files."*

    Every poster production has cached sits at
    `<root>/server/playlist/<playlist_id>/`, because the writer was handed
    `None` for the server. They move below `playlist/` under the content
    server, since two servers can hold a playlist with one id and one poster
    was overwriting the other.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = SyncDB(os.path.join(self.tmp, "catalog.db"))
        self.addCleanup(self.db.close)
        self.db.upsert(_row("home-film", HOME))
        self.db.upsert_playlist(COLLIDING, HOME, "uuid-home", "Example")
        self.db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                       server_id=HOME)

    def _manager(self):
        from jellyfin_mpv_shim.sync.manager import SyncManager
        m = SyncManager()
        m.db = self.db
        m.root = self.tmp
        return m

    def _old_dir(self):
        return os.path.join(self.tmp, "server", "playlist", COLLIDING)

    def _seed_old(self, name="poster.jpg", body=b"old"):
        os.makedirs(self._old_dir(), exist_ok=True)
        with open(os.path.join(self._old_dir(), name), "wb") as fh:
            fh.write(body)

    def test_the_poster_lands_under_its_server(self):
        self._seed_old()
        self._manager()._rehome_playlist_art()
        moved = os.path.join(playlist_art_dir(self.tmp, HOME, COLLIDING),
                             "poster.jpg")
        self.assertTrue(os.path.exists(moved))
        self.assertFalse(os.path.isdir(self._old_dir()),
                         "the old directory was left behind")

    def test_running_it_again_does_nothing(self):
        """Unconditional on every open, so it has to be a no-op once clean --
        and must not move a poster that is already in place back out."""
        self._seed_old()
        m = self._manager()
        m._rehome_playlist_art()
        m._rehome_playlist_art()
        moved = os.path.join(playlist_art_dir(self.tmp, HOME, COLLIDING),
                             "poster.jpg")
        self.assertEqual(open(moved, "rb").read(), b"old")

    def test_an_interrupted_move_is_finished_by_the_next_pass(self):
        """The crash-safety property, and the reason the pass is unconditional
        rather than marked done: interrupted, a poster is at one path or the
        other -- never neither -- and the next open completes it. Modelled as
        the state an interruption leaves: one file already moved, another not.
        """
        self._seed_old()
        self._seed_old(name="backdrop.jpg", body=b"also-old")
        new_dir = playlist_art_dir(self.tmp, HOME, COLLIDING)
        os.makedirs(new_dir, exist_ok=True)
        os.replace(os.path.join(self._old_dir(), "poster.jpg"),
                   os.path.join(new_dir, "poster.jpg"))
        self._manager()._rehome_playlist_art()
        self.assertEqual(sorted(os.listdir(new_dir)),
                         ["backdrop.jpg", "poster.jpg"])
        self.assertFalse(os.path.isdir(self._old_dir()))

    def test_a_duplicate_at_the_destination_wins(self):
        """The one file this deletes. If both ends hold a poster of that name,
        the destination's is the newer write -- a re-download after the move --
        and the old copy has to go or the pass never converges."""
        self._seed_old(body=b"old")
        new_dir = playlist_art_dir(self.tmp, HOME, COLLIDING)
        os.makedirs(new_dir, exist_ok=True)
        with open(os.path.join(new_dir, "poster.jpg"), "wb") as fh:
            fh.write(b"new")
        self._manager()._rehome_playlist_art()
        self.assertEqual(open(os.path.join(new_dir, "poster.jpg"),
                              "rb").read(), b"new")
        self.assertFalse(os.path.isdir(self._old_dir()))

    def test_a_playlist_with_no_art_is_not_a_failure(self):
        self._manager()._rehome_playlist_art()      # must not raise
        self.assertFalse(os.path.isdir(self._old_dir()))

    def test_an_unscoped_playlist_gets_its_own_directory(self):
        """NULL is a real scope here, not a reason to leave the poster where it
        was: a second layout is what the shared helper exists to prevent."""
        self.db.upsert_playlist(COLLIDING, None, "uuid-x", "Example")
        self.db.replace_playlist_items(COLLIDING, [("home-film", 0, 1)],
                                       server_id=None)
        self.db.delete_playlist(COLLIDING, server_id=HOME)
        self._seed_old()
        self._manager()._rehome_playlist_art()
        self.assertTrue(os.path.exists(os.path.join(
            playlist_art_dir(self.tmp, None, COLLIDING), "poster.jpg")))


class TheArtDirectoryIsBuiltSafelyTest(unittest.TestCase):
    """`playlist_art_dir` turns a value the *server* supplies into a
    filesystem path, so it refuses anything that is not a plain id."""

    def test_a_scope_that_is_not_an_id_becomes_the_unscoped_directory(self):
        from jellyfin_mpv_shim.sync.db import UNSCOPED_ART_DIR
        got = playlist_art_dir("/store", "../../../etc", "P")
        self.assertEqual(
            got, os.path.join("/store", "server", "playlist",
                              UNSCOPED_ART_DIR, "P"),
            "a server id was joined into the path without being checked")

    def test_a_real_server_id_is_used_as_it_is(self):
        """The control: a guard that replaced everything would pass the test
        above and put every playlist's art in one directory."""
        self.assertEqual(
            playlist_art_dir("/store", HOME, "P"),
            os.path.join("/store", "server", "playlist", HOME, "P"))

    def test_the_dashed_spelling_of_an_id_is_still_an_id(self):
        """Both spellings reach a client depending on the endpoint."""
        dashed = "0ccef365-5228-4944-ab0d-183114fcbe92"
        self.assertTrue(playlist_art_dir("/store", dashed, "P").endswith(
            os.path.join(dashed, "P")))

    #: A server-supplied id that walks out of the store. Both spellings of
    #: the separator, because a Windows build joins with the other one.
    HOSTILE = "../..\\..\\etc/cron.d/x"

    def test_no_server_supplied_id_escapes_the_store(self):
        """**Every builder in one test, on purpose.** A test per site is how
        the next site gets missed -- which is exactly what happened to
        `_typed`'s four-of-five, in this same repository, this same week.

        `playlist_art_dir` validated the *scope* it joins and said why in its
        own docstring -- "this builds a filesystem path out of a value a
        server supplies" -- while joining the equally server-supplied
        playlist id unchecked, and its three siblings checked nothing at all.
        """
        root = "/store"
        # **Both sides normalised.** `normpath` rewrites a leading `/` to `\`
        # on Windows, so comparing a normalised path against an un-normalised
        # base failed there on every builder -- reported as an escape that had
        # not happened. Caught by the Windows VM leg, not by Linux.
        base = os.path.normpath(os.path.join(root, STORE_DIR))
        built = {
            "playlist scope": playlist_art_dir(root, self.HOSTILE, "pl1"),
            "playlist id": playlist_art_dir(root, HOME, self.HOSTILE),
            "series": series_art_dir(root, self.HOSTILE),
            "season": season_art_dir(root, self.HOSTILE),
            "item": item_dir(root, self.HOSTILE),
            "legacy playlist": legacy_playlist_art_dir(root, self.HOSTILE),
        }
        for name, path in built.items():
            self.assertTrue(
                os.path.normpath(path).startswith(base + os.sep),
                "%s escaped the store: %s" % (name, os.path.normpath(path)))

    def test_and_an_ordinary_id_is_still_used_as_it_is(self):
        """The control. A stand-in for every id would contain the escape and
        lose the layout, and nothing else here would notice."""
        root = "/store"
        self.assertEqual(
            os.path.join(root, STORE_DIR, "series", COLLIDING),
            series_art_dir(root, COLLIDING))
        self.assertEqual(
            os.path.join(root, STORE_DIR, "item-1"),
            item_dir(root, "item-1"))

    def test_and_the_reader_builds_the_same_path_as_the_writer(self):
        """**Why these are functions rather than a check at each site.** The
        series and season caches are spelled in two places -- the downloader
        writes them and the offline browser reads them back -- and
        `playlist_art_dir` exists because that exact pair drifted once
        already: "they carried separate copies of this layout ... which is
        how a layout change becomes a silently blank tile". A sanitiser
        applied at the writer alone would blank the tile for any id it
        rewrote.
        """
        import inspect

        from jellyfin_mpv_shim.mpvtk_browser import repository
        from jellyfin_mpv_shim.sync import manager as sync_manager

        for module, name in ((sync_manager, "_download_series_art"),
                             (sync_manager, "_download_season_art"),
                             (repository, "_art_path")):
            src = inspect.getsource(getattr(
                getattr(module, "SyncManager", None)
                or repository.OfflineLibrarySource, name))
            self.assertNotIn(
                'STORE_DIR, "series"', src,
                "%s still spells the series cache path itself" % name)
            self.assertNotIn(
                'STORE_DIR, "season"', src,
                "%s still spells the season cache path itself" % name)

    def test_the_startup_pass_is_actually_called(self):
        """A rule parked where nothing calls it. Asserted structurally rather
        than by driving `start()`, which opens a catalog, migrates it,
        reconciles the disk and starts a worker thread -- the wiring is two
        calls and this is what fails if either goes away.

        **Both links, because one is not enough:** the pass sits in
        `_open_and_run` beside the disk reconcile, and `start` is what reaches
        it. Asserting only the inner one would pass with a startup path that
        no longer opens anything.
        """
        import ast
        import inspect
        import textwrap

        from jellyfin_mpv_shim.sync.manager import SyncManager

        # dedent: `getsource` keeps the class body's indentation, which
        # `ast.parse` rejects -- and a test that raises for its own reason
        # "catches" every mutation while checking nothing. This one did,
        # until the full suite ran it.
        tree = ast.parse(textwrap.dedent(inspect.getsource(SyncManager)))
        calls = {}
        for fn in tree.body[0].body:
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                calls[fn.name] = {n.func.attr for n in ast.walk(fn)
                                  if isinstance(n, ast.Call)
                                  and isinstance(n.func, ast.Attribute)}
        self.assertIn("_rehome_playlist_art", calls.get("_open_and_run", set()),
                      "nothing moves the cached art, so every playlist "
                      "poster stays where the old layout put it and no tile "
                      "finds one")
        self.assertIn("_open_and_run", calls.get("start", set()),
                      "`start` no longer reaches the pass that moves it")


class TheTombstonesBecomeOneTableTest(unittest.TestCase):
    """`auto_discarded` (item id alone) folds into `auto_discarded_scoped`.

    A tombstone keyed on the item id binds **every** server, and item ids are
    not unique across servers -- so once server A's copy was discarded and
    deleted, server B's *different* film of the same id was invisible to the
    scheduler forever, with nothing left in the catalog to explain why. Folded
    where a held download names the server, dropped otherwise: keeping such a
    row means keeping the bug.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "catalog.db")

    def _pre_migration(self, *tombstones, held=()):
        """A catalog with the old unscoped table and rows in it."""
        db = SyncDB(self.path)
        for item_id, server_id in held:
            db.upsert(_row(item_id, server_id))
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("CREATE TABLE IF NOT EXISTS auto_discarded ("
                     "item_id TEXT PRIMARY KEY, discarded_at INTEGER)")
        for item_id in tombstones:
            conn.execute("INSERT INTO auto_discarded VALUES (?, ?)",
                         (item_id, 1000))
        conn.commit()
        conn.close()

    def test_a_tombstone_whose_row_names_a_server_is_kept(self):
        self._pre_migration("film", held=[("film", HOME)])
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.discarded_ids(server_id=HOME), {"film"})

    def test_and_it_stops_binding_the_other_server(self):
        """The whole finding. Before this, server B's different film of the
        same id was suppressed by A's giving-up, forever."""
        self._pre_migration("film", held=[("film", HOME)])
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.discarded_ids(server_id=AWAY), set())

    def test_a_tombstone_with_no_row_to_name_it_is_dropped(self):
        """A tombstone outlives the row it describes, so this is the ordinary
        state for one whose download is gone. Nothing can say which server it
        was about, and keeping it would suppress the id everywhere."""
        self._pre_migration("orphaned")
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.discarded_ids(server_id=ANY_SERVER), set())

    def test_a_row_that_names_no_server_drops_its_tombstone_too(self):
        self._pre_migration("film", held=[("film", None)])
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.discarded_ids(server_id=ANY_SERVER), set())
        # **And the migration finished.** Without this the check above passes
        # when the fold *raises* -- `server_id` is NOT NULL, so inserting an
        # unnameable row aborts the whole migration, the old table survives and
        # nothing is suppressed for a second reason. The mutation round found
        # exactly that.
        self.assertIsNone(db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='auto_discarded'").fetchone(),
            "the fold aborted instead of skipping the row")

    def test_the_fold_reads_this_item_s_row_and_not_another_s(self):
        """Two held items on two servers, because with one the join cannot be
        wrong: `ON 1=1` gives the same answer. The third time a single-row
        fixture has hidden a wrong join in this work."""
        self._pre_migration("film", held=[("other-film", AWAY),
                                          ("film", HOME)])
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.discarded_ids(server_id=HOME), {"film"})
        self.assertEqual(db.discarded_ids(server_id=AWAY), set(),
                         "the tombstone took the other item's server")

    def test_it_reports_how_many_it_actually_kept(self):
        """The count is the only report anyone gets, and it was a *different
        number*: `connection.total_changes` is the connection's whole lifetime,
        so on the real 14-row catalog this said 44 rows were kept when the
        answer was zero -- the 44 were the homings and attributions earlier in
        the same migration. One foldable tombstone and one that names nothing,
        so a lifetime counter and a row count cannot agree by accident.
        """
        self._pre_migration("film", "orphaned", held=[("film", HOME)])
        with self.assertLogs("sync.db", level="INFO") as caught:
            SyncDB(self.path).close()
        line = [m for m in caught.output if "Tombstones" in m]
        self.assertTrue(line, "the fold said nothing")
        self.assertIn("1 unscoped row(s)", line[0])

    def test_the_old_table_is_gone(self):
        """Read alongside the new one it was still the bug; and the schema
        script must not bring it back on the next open."""
        self._pre_migration("film", held=[("film", HOME)])
        SyncDB(self.path).close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertIsNone(db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='auto_discarded'").fetchone())

    def test_a_second_open_is_a_no_op(self):
        self._pre_migration("film", held=[("film", HOME)])
        SyncDB(self.path).close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(db.discarded_ids(server_id=HOME), {"film"})

    def test_a_fresh_catalog_never_had_the_table(self):
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertIsNone(db._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name='auto_discarded'").fetchone())
        db.mark_discarded("film", server_id=HOME)
        self.assertEqual(db.discarded_ids(server_id=HOME), {"film"})


class TheOfflineIdRoundTripsTest(unittest.TestCase):
    """The id spelling itself, and the two callers that have to reverse it.

    The offline library is one pseudo-server and the browser routes a tile by
    its DTO `Id`, so the server has to be *in* the id -- and then the delete
    gesture and the downloaded badge both need it back out.
    """

    def test_it_round_trips(self):
        key = offline_playlist_id(COLLIDING, HOME)
        self.assertEqual(split_offline_playlist_id(key), (COLLIDING, HOME))

    def test_an_unscoped_playlist_round_trips_too(self):
        """NULL is a real scope: `list_playlists` admits it on every server, so
        such a playlist has a tile and it needs an id like any other."""
        key = offline_playlist_id(COLLIDING, None)
        self.assertEqual(split_offline_playlist_id(key), (COLLIDING, None))

    def test_an_ordinary_id_comes_back_unchanged(self):
        """Which is what lets the delete gesture and the badge call this on
        whatever id they hold rather than testing first."""
        self.assertEqual(split_offline_playlist_id(COLLIDING),
                         (COLLIDING, None))
        self.assertEqual(split_offline_playlist_id(None), (None, None))

    def test_the_two_servers_ids_differ(self):
        """The whole point, stated as the thing that was false before."""
        self.assertNotEqual(offline_playlist_id(COLLIDING, HOME),
                            offline_playlist_id(COLLIDING, AWAY))

    def test_the_badge_still_ticks_an_offline_playlist_tile(self):
        """The consequence that would have been missed: the badge set holds
        the catalog's plain ids, so comparing a scoped id raw loses the tick on
        every playlist tile in the offline library -- while every other tile
        there keeps one."""
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        r = TileRenderer.__new__(TileRenderer)
        r._downloaded = set()
        r._downloaded_series = set()
        r._downloaded_seasons = set()
        r._downloaded_playlists = {COLLIDING}
        key = offline_playlist_id(COLLIDING, HOME)
        self.assertTrue(r.is_downloaded({"Id": key, "Type": "Playlist"}))
        self.assertFalse(r.is_downloaded(
            {"Id": offline_playlist_id("other", HOME), "Type": "Playlist"}),
            "the badge ticked a playlist the catalog does not hold")

    def test_the_delete_gesture_names_the_catalog_s_id_and_server(self):
        """The other reverse. The catalog wants the two apart, and an *online*
        playlist DTO has to keep working through the same path."""
        from unittest import mock

        from jellyfin_mpv_shim.mpvtk_browser.item_actions import ItemActions

        actions = ItemActions(services=mock.Mock(), run=None, dialogs=None,
                              on_launch=None)
        ctl = mock.Mock()
        actions._drop_downloads(ctl, {
            "Id": offline_playlist_id(COLLIDING, HOME), "Type": "Playlist"})
        ctl.delete_download.assert_called_once_with(
            playlist_id=COLLIDING, playlist_server_id=HOME)
        ctl.reset_mock()
        actions._drop_downloads(ctl, {"Id": COLLIDING, "Type": "Playlist",
                                      "ServerId": AWAY})
        ctl.delete_download.assert_called_once_with(
            playlist_id=COLLIDING, playlist_server_id=AWAY)


class AnUnmigratedCatalogStillReadsTest(unittest.TestCase):
    """A read-only open runs neither the schema nor the migration.

    So the offline library -- and the read-only handle a failed writable open
    falls back to -- can be looking at a catalog whose `playlist_items` has no
    `server_id`. A scoped query there raises `no such column`, and
    `repository.reload` turns any exception into an **empty offline library**:
    every download invisible, on exactly the launch that has nothing else to
    show. So the scope is dropped rather than asked for, which is what these
    reads did before the column existed.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "catalog.db")
        db = SyncDB(self.path)
        db.upsert(_row("home-film", HOME))
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE playlists")
        conn.execute("DROP TABLE playlist_items")
        conn.execute("CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, "
                     "server_id TEXT, server_uuid TEXT, name TEXT, "
                     "added_at INTEGER)")
        conn.execute("CREATE TABLE playlist_items (playlist_id TEXT, "
                     "item_id TEXT, sort_index INTEGER, "
                     "owned INTEGER DEFAULT 0, "
                     "PRIMARY KEY (playlist_id, item_id))")
        conn.execute("INSERT INTO playlists VALUES (?,?,?,?,?)",
                     (COLLIDING, HOME, "uuid-home", "Example Playlist", 1000))
        conn.execute("INSERT INTO playlist_items VALUES (?,?,?,?)",
                     (COLLIDING, "home-film", 0, 1))
        conn.commit()
        conn.close()

    def test_the_members_still_come_back(self):
        db = SyncDB(self.path, read_only=True)
        self.addCleanup(db.close)
        self.assertEqual(
            [r["item_id"] for r in
             db.playlist_item_rows(COLLIDING, server_id=HOME)],
            ["home-film"], "the offline library went empty")

    def test_and_so_does_the_ownership_map(self):
        db = SyncDB(self.path, read_only=True)
        self.addCleanup(db.close)
        self.assertEqual(db.playlist_ownership(),
                         {"home-film": (COLLIDING, None)})

    def test_owned_ids_answer_too(self):
        db = SyncDB(self.path, read_only=True)
        self.addCleanup(db.close)
        self.assertEqual(db.playlist_owned_ids(COLLIDING, server_id=HOME),
                         {"home-film"})

    def test_a_writable_open_migrates_and_then_scopes(self):
        """The other side of it: the same catalog opened writable is migrated,
        so the very next read is scoped for real."""
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(
            db.playlist_owned_ids(COLLIDING, server_id=AWAY), set(),
            "after the migration a wrong scope must answer with nothing")


class AMissingCatalogReadsEmptyRatherThanRaisingTest(unittest.TestCase):
    """The read-only open with no file at all -- the handle `_open_catalog`
    is left holding when a restore fails, and the one the offline library
    builds before anything has ever been downloaded.

    It holds no connection, so every read is meant to answer "nothing". The
    constructor used to `return` before setting `_playlist_items_scoped`, so
    the four playlist reads raised `AttributeError` instead -- which is not
    `sqlite3.Error` and is not an empty list, so it escaped every caller that
    had thought about an unreadable catalog. `repository.reload` caught it
    and logged "failed to open"; `gateway/downloads.py` did not.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "nothing-here.db")
        self.db = SyncDB(self.path, read_only=True)
        self.addCleanup(self.db.close)

    def test_it_creates_nothing(self):
        """A writable open would; this one must not, or a later launch reads
        an empty catalog as the truth and never retries the restore."""
        self.assertFalse(os.path.exists(self.path))

    def test_every_playlist_read_answers_empty(self):
        self.assertEqual(self.db.list_playlists(), [])
        self.assertEqual(self.db.list_playlists(HOME), [])
        self.assertEqual(self.db.playlist_ownership(), {})
        self.assertEqual(self.db.playlist_item_rows(COLLIDING,
                                                    server_id=HOME), [])
        self.assertEqual(self.db.playlist_owned_ids(COLLIDING,
                                                    server_id=HOME), set())

    def test_and_so_does_the_catalog_itself(self):
        """The reads that always worked, so the class cannot pass by the
        object being broken in some other way."""
        self.assertEqual(self.db.list(), [])
        self.assertIsNone(self.db.get("home-film"))
        self.assertFalse(self.db.healthy())

    def test_the_absence_is_logged(self):
        """An offline library that reads empty looks the same whether the
        catalog is gone or was never written. Only the log can tell the two
        apart, and it is the only artefact a bug report carries."""
        with self.assertLogs("sync.db", level="INFO") as caught:
            SyncDB(os.path.join(self.tmp, "also-missing.db"),
                   read_only=True).close()
        self.assertTrue(any("also-missing.db" in m for m in caught.output),
                        caught.output)


class TheMigrationSplitsAnOverwrittenRowTest(unittest.TestCase):
    """The upgrade path, from the shape every existing catalog has.

    Reached by putting the **old** tables back after a normal open: the schema
    is `CREATE TABLE IF NOT EXISTS`, so a pre-migration table survives it, and
    the rebuild is keyed on its index being absent. That is what a real
    upgrade looks like from the inside.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, "catalog.db")

    def _seed_pre_migration(self, playlist_server=None):
        db = SyncDB(self.path)
        db.upsert(_row("home-film", HOME))
        db.close()
        conn = sqlite3.connect(self.path)
        conn.execute("DROP TABLE playlists")
        conn.execute("DROP TABLE playlist_items")
        conn.execute("CREATE TABLE playlists (playlist_id TEXT PRIMARY KEY, "
                     "server_id TEXT, server_uuid TEXT, name TEXT, "
                     "added_at INTEGER)")
        conn.execute("CREATE TABLE playlist_items (playlist_id TEXT, "
                     "item_id TEXT, sort_index INTEGER, "
                     "owned INTEGER DEFAULT 0, "
                     "PRIMARY KEY (playlist_id, item_id))")
        conn.execute("INSERT INTO playlists VALUES (?,?,?,?,?)",
                     (COLLIDING, playlist_server, "uuid-home",
                      "Example Playlist", 1000))
        conn.execute("INSERT INTO playlist_items VALUES (?,?,?,?)",
                     (COLLIDING, "home-film", 0, 1))
        conn.commit()
        conn.close()

    def test_a_null_server_is_derived_from_the_members(self):
        """Every playlist row a shipped build ever wrote carried NULL -- its
        writer read a key the apiclient misspells -- so this is the only
        branch a real upgrade takes."""
        self._seed_pre_migration(playlist_server=None)
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        rows = db.list_playlists()
        self.assertEqual([r["server_id"] for r in rows], [HOME])

    def test_the_membership_takes_the_server_of_its_own_item(self):
        self._seed_pre_migration()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual(
            [r["item_id"] for r in
             db.playlist_item_rows(COLLIDING, server_id=HOME)],
            ["home-film"])

    def test_a_server_that_was_already_recorded_is_left_alone(self):
        self._seed_pre_migration(playlist_server=AWAY)
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual([r["server_id"] for r in db.list_playlists()],
                         [AWAY], "the migration overwrote a known server")

    def test_members_that_disagree_leave_the_server_unknown(self):
        """A playlist whose members came from two servers cannot be scoped to
        either, so it stays NULL -- visible on every scope, which is what
        `list_playlists` already does with an unattributable row.

        Reachable: the same playlist id on two servers, downloaded twice into
        the ONE membership list the old key gave them. That is the state this
        migration exists to come out of, so guessing a server here would file
        half of somebody's playlist under the wrong one.
        """
        self._seed_pre_migration(playlist_server=None)
        conn = sqlite3.connect(self.path)
        conn.execute("INSERT INTO downloads (item_id, content_server_id, "
                     "status, type, name) VALUES (?,?,?,?,?)",
                     ("away-film", AWAY, STATUS_COMPLETE, "Movie", "away"))
        conn.execute("INSERT INTO playlist_items VALUES (?,?,?,?)",
                     (COLLIDING, "away-film", 1, 1))
        conn.commit()
        conn.close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual([r["server_id"] for r in db.list_playlists()], [None],
                         "the migration guessed a server for a playlist whose "
                         "members disagree")

    def test_a_second_open_changes_nothing(self):
        """The index is the marker, so the rebuild must not run twice -- a
        second pass would copy the copy."""
        self._seed_pre_migration()
        SyncDB(self.path).close()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        self.assertEqual([r["server_id"] for r in db.list_playlists()], [HOME])
        self.assertEqual(
            [r["item_id"] for r in
             db.playlist_item_rows(COLLIDING, server_id=HOME)],
            ["home-film"])

    def test_and_the_migrated_catalog_can_hold_the_second_server(self):
        """The point of the rebuild, asked of a catalog that came through it
        rather than of a fresh one."""
        self._seed_pre_migration()
        db = SyncDB(self.path)
        self.addCleanup(db.close)
        db.upsert(_row("away-film", AWAY))
        db.upsert_playlist(COLLIDING, AWAY, "uuid-away", "Example Playlist")
        db.replace_playlist_items(COLLIDING, [("away-film", 0, 1)],
                                  server_id=AWAY)
        self.assertEqual(
            sorted(r["server_id"] for r in db.list_playlists()),
            sorted([HOME, AWAY]))


if __name__ == "__main__":
    unittest.main()
