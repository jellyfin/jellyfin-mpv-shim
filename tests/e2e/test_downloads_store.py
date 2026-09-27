"""Slice S5, `downloads_store`: downloads that are interrupted, a catalog
that is lost, and a store that moves.

Critical-path inventory rows 59, 65 and 66, on the shipped app by keys.
Scenario 10 (test_store_safety) already has G5 -- the person's files through
a sweep, a delete, a move and a kill mid-copy; this is the rest:

- row 59: a download cut mid-way, and one killed mid-way, finish -- and
  finish by RESUMING (a Range request from where the .part file ends), not
  by starting again;
- row 66: a catalog.db truncated between launches is restored from its
  .bak, and the downloads are still there;
- row 65 / F42: after a move, the Download Folder field shows the path the
  store is actually in, across leaving Settings and a relaunch; a blank
  field moves it back to the default.
"""

import glob
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
