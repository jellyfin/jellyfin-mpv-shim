"""Slice S5, `downloads_store`: downloads that are interrupted, a catalog
that is lost, and a store that moves.

Critical-path inventory rows 59, 65 and 66, on the shipped app by keys.
Scenario 10 (test_store_safety) already has G5 -- the person's files through
a sweep, a delete, a move and a kill mid-copy; this is the rest:

- row 59: a download cut mid-way, and one killed mid-way, finish -- and
  finish by RESUMING (a Range request from where the .part file ends), not
  by starting again;
- row 66: a catalog.db truncated between launches is restored from its
  .bak, and the downloads are still there -- and so is a damaged
  users.json (c0474bb5);
- row 65 / F42: after a move, the Download Folder field shows the path the
  store is actually in, across leaving Settings and a relaunch; a blank
  field moves it back to the default.
"""

import glob
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
import _e2e  # noqa: E402
import _flows  # noqa: E402
import _relay  # noqa: E402
from test_store_safety import FILM_NAME, FILM_QUERY, _StoreCase  # noqa: E402

#: Ten minutes in 44 MB: at THROTTLE a download is still running ~20 s in.
LONG_NAME = "Eat for Health"
THROTTLE = 2_000_000


def _backend():
    return os.environ.get("JMS_TEST_BACKEND", "libmpv")


@_e2e.require_server
class _DownloadCase(unittest.TestCase):

    def setUp(self):
        self.session = _e2e.Session()
        films = [i for i in self.session.find_all(item_type="Movie")
                 if i.get("Name") == LONG_NAME]
        self.assertEqual(1, len(films))
        self.film = films[0]["Id"]
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.app.start()
        _flows.login(self.app, self.relay)

    def item_dir(self):
        return os.path.join(self.app.config_dir, "offline", "server",
                            self.film)

    def part_bytes(self):
        return sum(os.path.getsize(p) for p in
                   glob.glob(os.path.join(self.item_dir(), "**", "*.part"),
                             recursive=True))

    def start_the_download(self):
        """Download, confirmed, and back as soon as bytes are arriving."""
        self.relay.throttle(THROTTLE)
        _flows.open_by_search(self.app, LONG_NAME, self.film)
        self.app.move_to("act-download")
        self.app.key("ENTER")
        self.app.wait_for(lambda f: _app.shown(f, "dl-ok"), timeout=15,
                          what="the download dialog")
        self.app.move_to("dl-ok")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(lambda: self.part_bytes() > 4_000_000,
                                      timeout=60),
                        "no partial file grew (have %d bytes)"
                        % self.part_bytes())
        return self.part_bytes()

    def resumed(self, had):
        """A ranged request for the media, from at least where the partial
        file had got to: a resume, not a restart."""
        starts = []
        for path, rng in self.relay.ranges:
            if self.film in path and rng.startswith("bytes="):
                try:
                    starts.append(int(rng[6:].split("-")[0]))
                except ValueError:
                    pass
        return any(s >= had * 0.9 for s in starts), starts

    def complete(self, timeout=180):
        self.assertTrue(_e2e.wait_for(
            lambda: (self.catalog.download(self.film) or {}).get("status")
            == "complete", timeout=timeout),
            "the download never completed (row %r)"
            % (self.catalog.download(self.film),))


class ACutDownloadResumesTest(_DownloadCase):
    """Row 59: the network drops mid-download and comes back."""

    def test_it_finishes_from_where_it_was(self):
        had = self.start_the_download()
        self.relay.cut()
        self.assertTrue(self.relay.probe_refused())
        time.sleep(5)
        self.relay.restore()
        self.relay.throttle(None)
        self.complete()
        ok, starts = self.resumed(had)
        self.assertTrue(ok, "finished, but not by resuming from %d bytes "
                            "(ranged starts: %r)" % (had, starts))
        self.assertEqual(0, self.app.quit(timeout=30))


class AKilledDownloadResumesTest(_DownloadCase):
    """Row 59: the app is killed mid-download (no chance to tidy up), and
    the next launch finishes it from the .part."""

    def test_the_next_launch_finishes_it(self):
        had = self.start_the_download()
        self.app.kill()
        self.relay.throttle(None)
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=90,
                          what="Home after the kill")
        self.complete()
        ok, starts = self.resumed(had)
        self.assertTrue(ok, "finished, but not by resuming from %d bytes "
                            "(ranged starts: %r)" % (had, starts))
        self.assertEqual(0, self.app.quit(timeout=30))


@_e2e.require_server
class AMusicPlaylistDownloadsAsOneTest(unittest.TestCase):
    """Row 67 (INV N15): a playlist of songs, downloaded from its tile's
    menu with the MENU key -- the ten-foot way -- lands as one unit: its
    songs complete, one playlist row in the catalog, and the Downloads
    screen names the playlist once."""

    def setUp(self):
        self.session = _e2e.Session()
        self.songs = []
        for a in self.session.find_all(library="Music",
                                       item_type="MusicAlbum"):
            got = self.session.find_all(item_type="Audio",
                                        parent_id=a["Id"])
            if len(got) >= 2:
                self.songs = [s["Id"] for s in got[:2]]
                break
        self.assertEqual(2, len(self.songs), "no album with two tracks")
        self.name = "jms-e2e-dl-songs"
        # Before sign-in: Home's library row is read then (S2's note).
        self.playlist = self.session._request("/Playlists", method="POST",
                                              body={
            "Name": self.name, "Ids": self.songs,
            "UserId": self.session.user_id, "MediaType": "Audio"})["Id"]
        self.addCleanup(self.session._request, "/Items/%s" % self.playlist,
                        "DELETE")
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.app.start()
        _flows.login(self.app, self.relay)

    def pick_from_menu(self, label):
        f = self.app.wait_for(lambda f: f.get("menu_open"), timeout=10,
                              what="the tile's menu")
        menu = next(n for n in f["nodes"] if n.get("t") == "menu")
        index = menu["items"].index(label)
        for _ in range(index):
            self.app.key("DOWN")
        self.app.key("ENTER")

    def test_one_unit(self):
        views = self.session._request("/Users/%s/Views"
                                      % self.session.user_id)["Items"]
        shelf = next(v["Id"] for v in views
                     if v.get("CollectionType") == "playlists")
        f = self.app.frame()
        first = next(n["id"] for n in f["nodes"]
                     if (n.get("id") or "").startswith("row-libs-"))
        self.app.move_to(first)
        self.app.move_to("row-libs-" + shelf, key="RIGHT")
        tile = "grid-0-" + self.playlist
        self.app.press_until("ENTER", lambda f: _app.shown(f, tile),
                             what="the Playlists shelf")
        self.app.move_to(tile)
        self.app.key("MENU")
        self.pick_from_menu("Download")
        self.app.wait_for(lambda f: _app.shown(f, "dl-ok"), timeout=15,
                          what="the download dialog")
        self.app.move_to("dl-ok")
        self.app.key("ENTER")
        self.assertTrue(_e2e.wait_for(
            lambda: all((self.catalog.download(s) or {}).get("status")
                        == "complete" for s in self.songs), timeout=120),
            "the playlist's songs did not all download")
        rows = self.catalog._query(
            "SELECT playlist_id FROM playlists WHERE playlist_id = ?",
            (self.playlist,))
        self.assertEqual(1, len(rows), "one playlist, one row: %r" % rows)
        _flows.open_settings_tab(self.app, "downloads")
        f = self.app.wait_for(lambda f: any(self.name in t
                                            for t in _app.texts(f)),
                              timeout=15, what="the playlist on Downloads")
        self.assertEqual(1, sum(1 for t in _app.texts(f) if self.name in t),
                         "the playlist is listed more than once")

        # And offline, it plays from the store.
        self.app = _flows.relaunch(self.app, self.relay, cut=True)
        f = self.app.wait_for(
            lambda f: _app.shown(f, "row-libs-offline:playlists"), timeout=90,
            what="an offline Playlists library")
        first = next(n["id"] for n in f["nodes"]
                     if (n.get("id") or "").startswith("row-libs-"))
        self.app.move_to(first)
        self.app.move_to("row-libs-offline:playlists", key="RIGHT")
        # Offline, a tile's id is a pseudo-id ending in the playlist's.
        def offline_tile(f):
            return next((n["id"] for n in f.get("nodes", [])
                         if (n.get("id") or "").startswith("grid-0-offline:")
                         and n["id"].endswith(self.playlist)), None)
        f = self.app.press_until("ENTER", offline_tile,
                                 what="the playlist, offline")
        tile = offline_tile(f)
        self.app.move_to(tile)
        self.app.press_until("ENTER", lambda f: _app.shown(f, "pl-play"),
                             what="the playlist's page, offline")
        self.app.move_to("pl-play")
        self.app.key("ENTER")
        path = self.app.playing_path()
        store = os.path.join(self.app.config_dir, "offline", "server")
        self.assertTrue(path.startswith(store) and any(s in path
                                                       for s in self.songs),
                        "offline, the playlist did not play a downloaded "
                        "song (%r)" % path)
        self.assertEqual(0, self.app.quit(timeout=30))


@_e2e.require_server
class AutoDownloadAndTheReaperTest(unittest.TestCase):
    """Row 64 under the REAL timers -- a long leg (~20 minutes): the settle
    (60 s), the userdata sweep floor (300 s) and the reap hold (900 s from
    launch) are constants, not settings. A show put into Next Up downloads
    its next episodes by itself; one of them watched on the server is
    reaped (watched grace 0 h), the next is kept; and only auto-downloads
    are ever reaped."""

    SHOW = "The Standard Show"
    CONF = {"auto_download_enable": True, "auto_download_next_up": True,
            "auto_download_lookahead": 2,
            "auto_download_keep_watched_hours": 0,
            "auto_download_delete_watched": True,
            "auto_download_interval_mins": 1}

    def setUp(self):
        self.session = _e2e.Session()
        show = next(s for s in self.session.find_all(item_type="Series")
                    if s.get("Name") == self.SHOW)
        eps = self.session._request("/Shows/%s/Episodes?UserId=%s"
                                    % (show["Id"], self.session.user_id)
                                    )["Items"]
        # Real episodes of season 1 on: specials (season 0) sort first and
        # are not what Next Up offers after the first episode.
        eps = [e for e in eps if e.get("LocationType") != "Virtual"
               and (e.get("ParentIndexNumber") or 0) >= 1]
        eps.sort(key=lambda e: (e.get("ParentIndexNumber") or 0,
                                e.get("IndexNumber") or 0))
        self.eps = [e["Id"] for e in eps[:5]]
        self.assertEqual(5, len(self.eps), "the show needs five episodes")
        for e in self.eps:
            self.session.reset_played(e)
            self.addCleanup(self.session.reset_played, e)
        self.session.api.item_played(self.eps[0], True)   # Next Up: E02
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend(), conf=dict(self.CONF))
        self.addCleanup(lambda: self.app.close())
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.launched = time.monotonic()
        self.app.start()
        _flows.login(self.app, self.relay)

    def row(self, item_id):
        return self.catalog.download(item_id) or {}

    def test_next_up_downloads_itself_and_the_watched_one_is_reaped(self):
        _flows.open_settings_tab(self.app, "servers")
        self.app.move_to("sv-auto-0")
        self.app.key("ENTER")
        e2, e3 = self.eps[1], self.eps[2]
        self.assertTrue(_e2e.wait_for(
            lambda: all(self.row(e).get("status") == "complete"
                        for e in (e2, e3)), timeout=600),
            "Next Up never downloaded by itself (rows %r)"
            % [(e, self.row(e).get("status")) for e in (e2, e3)])
        self.assertTrue(all(str(self.row(e).get("origin") or "")
                            .startswith("auto") for e in (e2, e3)),
                        "not recorded as auto-downloads: %r"
                        % [self.row(e).get("origin") for e in (e2, e3)])
        self.assertFalse(self.row(self.eps[0]),
                         "the watched episode before Next Up was fetched")
        self.session.api.item_played(e2, True)
        # The hold runs from launch; then the next sweep and reap pass.
        budget = max(0, 900 - (time.monotonic() - self.launched)) + 600
        self.assertTrue(_e2e.wait_for(lambda: not self.row(e2),
                                      timeout=budget),
                        "the watched auto-download was never reaped")
        self.assertFalse(os.path.exists(os.path.join(
            self.app.config_dir, "offline", "server", e2)),
            "its files outlived its row")
        self.assertEqual("complete", self.row(e3).get("status"),
                         "the reaper took the unwatched next episode")
        self.assertEqual(0, self.app.quit(timeout=40))


class ATruncatedCatalogIsRestoredTest(_StoreCase):
    """Row 66 (INV N6): catalog.db cut to nothing between launches. The
    .bak comes back, and the download is still a download."""

    def test_the_backup_brings_the_downloads_back(self):
        self.assertEqual("complete",
                         (self.catalog.download(self.film) or {})
                         .get("status"))
        # The backup is taken at launch (manager._backup_catalog), so one
        # launch after the download is what puts it in the .bak. A download
        # newer than the backup is re-listed a launch later, by design.
        self.app = _flows.relaunch(self.app)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the relaunch that backs up")
        self.assertEqual(0, self.app.quit(timeout=30))
        db = os.path.join(self.root, "catalog.db")
        self.assertTrue(os.path.exists(db + ".bak"),
                        "no catalog backup was kept to restore from")
        for suffix in ("-wal", "-shm"):
            if os.path.exists(db + suffix):
                os.remove(db + suffix)
        with open(db, "wb") as fh:
            fh.write(b"not a database")
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=90,
                          what="Home after the damage")
        self.assertEqual("complete",
                         (self.catalog.download(self.film) or {})
                         .get("status"),
                         "the download is gone after the catalog was lost")
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        # A downloaded item offers Remove Download (act-undownload).
        self.app.wait_for(lambda f: _app.shown(f, "act-undownload"),
                          timeout=15, what="the film shown as downloaded")
        self.assertEqual(0, self.app.quit(timeout=30))


class ADamagedUsersFileIsRestoredTest(_StoreCase):
    """c0474bb5, unit-only until now (fix-coverage audit gap 4): users.json
    cut to garbage between launches. Its .bak comes back -- the same
    profiles, by id -- the app signs in without asking, and the download is
    still one. Mutation-checked: with the backup ignored, the app starts
    fresh at the sign-in form."""

    def test_the_backup_brings_the_profiles_back(self):
        self.assertEqual(0, self.app.quit(timeout=30))
        path = os.path.join(self.app.config_dir, "users.json")
        with open(path, encoding="utf-8") as fh:
            before = json.load(fh)
        ids = sorted(u["id"] for u in before["users"])
        self.assertTrue(os.path.exists(path + ".bak"),
                        "no users.json backup was kept to restore from")
        with open(path, "wb") as fh:
            fh.write(b"{not json")
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=90,
                          what="Home, signed in, after the damage")
        with open(path, encoding="utf-8") as fh:
            after = json.load(fh)
        self.assertEqual(ids, sorted(u["id"] for u in after["users"]),
                         "the profiles came back as new ones")
        self.assertEqual(before.get("active"), after.get("active"))
        self.assertTrue(glob.glob(path + ".unreadable-*"),
                        "the damaged bytes were not kept aside")
        self.assertEqual("complete",
                         (self.catalog.download(self.film) or {})
                         .get("status"),
                         "the download is gone after users.json was lost")
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        self.app.wait_for(lambda f: _app.shown(f, "act-undownload"),
                          timeout=15, what="the film shown as downloaded")
        self.assertEqual(0, self.app.quit(timeout=30))


class TheMoveUsesThePathOnScreenTest(_StoreCase):
    """Row 65 / F42 (rulings inventory N7): type a path, leave Settings
    without moving, come back, press Move -- the path ON SCREEN is the one
    used. The bug was a typed path outliving the field that produced it, so
    Move relocated the whole store somewhere the field no longer said.
    Then a blank field moves the store back to the default."""

    def field(self):
        _flows.open_settings_tab(self.app, "browse")
        f = self.app.wait_for(lambda f: _app.node(f, "set-sync_path"),
                              timeout=15, what="the Download Folder field")
        return (_app.fields(f).get("set-sync_path")
                or (_app.node(f, "set-sync_path") or {}).get("text") or "")

    def moved(self):
        self.app.wait_for(lambda f: any("Download folder moved" in t
                                        for t in _app.texts(f)),
                          timeout=120, what="the move to finish")

    def test_the_shown_path_is_the_one_moved_to(self):
        parent = tempfile.mkdtemp(prefix="jms-e2e-f42-", dir="/dev/shm"
                                  if os.path.isdir("/dev/shm") else None)
        self.addCleanup(shutil.rmtree, parent, True)
        here = os.path.join(parent, "Here")
        typed_only = os.path.join(parent, "TypedAndAbandoned")
        self.move_to_folder(here)                # cross-volume, onto tmpfs
        self.moved()
        self.app.key("ESC")
        shown = self.field()
        self.assertTrue(os.path.exists(os.path.join(shown, "catalog.db")),
                        "the field says %r, and the store is not there"
                        % shown)

        # Typed, never moved, and left.
        self.app.move_to("set-sync_path", key="DOWN")
        if self.app.frame().get("focus") != "set-sync_path":
            self.app.press_until(
                "ENTER", lambda f: f.get("focus") == "set-sync_path",
                what="the field open for typing")
        self.app.clear_field("set-sync_path")
        self.app.type_into("set-sync_path", typed_only)
        self.app.key("ESC")                      # out of the field
        self.app.move_to("nav-home")             # and away from Settings
        self.app.press_until("ENTER", lambda f: _app.shown(f, "row-libs"),
                             what="Home, leaving Settings")
        self.assertEqual(shown, self.field(),
                         "the field came back with the abandoned path")
        # Beside the field, in its row: down to the field, then right.
        self.app.move_to("set-sync_path", key="DOWN")
        self.app.move_to("set-sync-move", key="RIGHT")
        self.app.key("ENTER")
        time.sleep(5)
        self.assertFalse(os.path.exists(os.path.join(typed_only,
                                                     "catalog.db")),
                         "Move went to the path typed and abandoned, not "
                         "the one on screen")
        self.assertTrue(os.path.exists(os.path.join(shown, "catalog.db")))

        # Blank: back to the default, confirmed.
        self.move_to_folder("")
        self.app.wait_for(lambda f: _app.shown(f, "dlg-ok"), timeout=15,
                          what="the confirmation to use the default folder")
        self.app.move_to("dlg-ok")
        self.app.key("ENTER")
        self.moved()
        self.assertTrue(os.path.exists(os.path.join(self.root, "catalog.db")),
                        "a blank field did not bring the store home")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
