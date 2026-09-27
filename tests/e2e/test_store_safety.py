"""Scenario 10: G5, the one absolute (docs/offline-sync-goals.md).

A **delete** never removes a file the app cannot prove it wrote -- the
download folder may be one the person also keeps their own things in. A
**move** is the deliberate exception: it carries everything, the person's
files included, and must deliver every one byte-identical; interrupted, every
file survives at the source or the destination.

Checked by content hashes, never a listing. Driven by keyboard against the
shipped app; the planted files are the one thing written from outside it,
standing in for a person dropping files into the folder.

Not asserted, pending a ruling: a person's file placed INSIDE a download's
own directory (a subtitle next to the film). Remove Download deletes that
directory whole today (register, "Scenario 10").
"""

import hashlib
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

FILM_NAME = "The Only Film In Its Set"
FILM_QUERY = "Only Film"
#: The app's own bookkeeping, rewritten after a move by design.
NOT_CONTENT = ("catalog.db", "catalog.db-wal", "catalog.db-shm", "catalog.db.bak",
               ".jellyfin-mpv-shim-write-test")


def _backend():
    return os.environ.get("JMS_TEST_BACKEND", "libmpv")


def hashes(root):
    """{relative path: sha256} for every file under ``root``."""
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            full = os.path.join(dirpath, name)
            rel = os.path.relpath(full, root)
            if rel in NOT_CONTENT:
                continue
            h = hashlib.sha256()
            with open(full, "rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[rel] = h.hexdigest()
    return out


def plant(root, rel, size):
    """A person's own file, dropped into the download folder."""
    path = os.path.join(root, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as fh:
        left = size
        while left:
            n = min(left, 1 << 22)
            fh.write(os.urandom(n))
            left -= n
    return rel


@_e2e.require_server
class _StoreCase(unittest.TestCase):
    """The film downloaded online, into the default folder."""

    def setUp(self):
        session = _e2e.Session()
        films = [i for i in session.find_all(item_type="Movie")
                 if i.get("Name") == FILM_NAME]
        self.assertEqual(1, len(films))
        self.film = films[0]["Id"]
        upstream = _e2e.SERVER.split("//", 1)[1]
        host, _, port = upstream.partition(":")
        self.relay = _relay.Relay((host, int(port or 80)))
        self.addCleanup(self.relay.close)
        self.app = _app.App(backend=_backend())
        self.addCleanup(lambda: self.app.close())
        self.root = os.path.join(self.app.config_dir, "offline")
        self.catalog = _flows.Catalog(self.app.config_dir)
        self.app.start()
        _flows.login(self.app, self.relay)
        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.download_open_item(self.app, self.catalog, self.film)

    def move_to_folder(self, dest):
        _flows.open_settings_tab(self.app, "browse")
        # Below the fold, where TAB does not go: it walks only what is on
        # screen (renderer nav_candidates; register, "TAB stops at the
        # fold"). DOWN scrolls focus there, and ENTER opens the field.
        self.app.wait_for(lambda f: _app.node(f, "set-sync_path"),
                          timeout=15, what="the Download Folder field")
        self.app.move_to("set-sync_path", key="DOWN")
        if self.app.frame().get("focus") != "set-sync_path":
            self.app.press_until(
                "ENTER", lambda f: f.get("focus") == "set-sync_path",
                what="the Download Folder field open for typing")
        self.app.clear_field("set-sync_path")
        self.app.type_into("set-sync_path", dest)
        self.app.key("ENTER")


class ADeleteKeepsWhatIsNotOursTest(_StoreCase):

    def test_the_persons_files_survive_the_sweep_and_a_delete(self):
        mine = [plant(self.root, "My Notes.txt", 4096),
                plant(self.root, os.path.join("server", "Holiday Photos",
                                              "beach.jpg"), 65536)]
        want = {rel: h for rel, h in hashes(self.root).items() if rel in mine}
        self.assertEqual(len(mine), len(want))

        # A launch with a row in the catalog runs the orphan sweep.
        self.app = _flows.relaunch(self.app)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the relaunch")
        self.assertEqual("complete",
                         (self.catalog.download(self.film) or {}).get("status"))
        got = hashes(self.root)
        for rel, h in want.items():
            self.assertEqual(h, got.get(rel), "%s did not survive the "
                                              "startup sweep" % rel)

        _flows.open_by_search(self.app, FILM_QUERY, self.film)
        _flows.remove_download_open_item(self.app, self.catalog, self.film)
        self.app = _flows.relaunch(self.app)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the second relaunch")
        got = hashes(self.root)
        for rel, h in want.items():
            self.assertEqual(h, got.get(rel), "%s did not survive Remove "
                                              "Download" % rel)
        self.assertFalse(os.path.exists(
            os.path.join(self.root, "server", self.film)),
            "Remove Download left the film's own directory behind")
        self.assertEqual(0, self.app.quit(timeout=30))


class AMoveCarriesEverythingTest(_StoreCase):

    def test_every_file_arrives_byte_identical(self):
        plant(self.root, "My Notes.txt", 4096)
        plant(self.root, os.path.join("Photos", "beach.jpg"), 65536)
        before = hashes(self.root)
        self.assertTrue(any(rel.startswith("server" + os.sep + self.film)
                            for rel in before), "no film file to carry")
        parent = tempfile.mkdtemp(prefix="jms-e2e-move-")
        self.addCleanup(shutil.rmtree, parent, True)
        dest = os.path.join(parent, "Downloads")

        self.move_to_folder(dest)
        self.app.wait_for(
            lambda f: any("Download folder moved" in t
                          for t in _app.texts(f)),
            timeout=120, what="the move to finish")
        self.assertEqual(before, hashes(dest),
                         "the destination does not hold exactly what the "
                         "source did")
        left = hashes(self.root) if os.path.isdir(self.root) else {}
        self.assertEqual({}, left, "files left behind at the source")

        # And the app knows where they went, across a restart.
        self.app = _flows.relaunch(self.app)
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the relaunch")
        moved = _flows.Catalog(self.app.config_dir, root=dest)
        self.assertEqual("complete",
                         (moved.download(self.film) or {}).get("status"))
        self.assertEqual(before, hashes(dest))
        self.assertEqual(0, self.app.quit(timeout=30))


class AnInterruptedMoveLosesNothingTest(_StoreCase):
    """Killed mid-copy across volumes: every file is whole at one root or
    the other. A same-volume move is a rename and has no window, so this
    needs a destination on another filesystem (``/dev/shm``, or
    JMS_E2E_OTHER_VOLUME); a machine without one skips it."""

    BIG = 1 << 30      # the copy window: a person's large file, moved too

    def test_a_kill_mid_copy_leaves_every_file_somewhere_whole(self):
        other = os.environ.get("JMS_E2E_OTHER_VOLUME") or "/dev/shm"
        if (not os.path.isdir(other)
                or os.stat(other).st_dev == os.stat(self.root).st_dev):
            self.skipTest("no second filesystem to move across")
        plant(self.root, "My Notes.txt", 4096)
        plant(self.root, "big.bin", self.BIG)
        before = hashes(self.root)
        parent = tempfile.mkdtemp(prefix="jms-e2e-move-", dir=other)
        self.addCleanup(shutil.rmtree, parent, True)
        dest = os.path.join(parent, "Downloads")

        self.move_to_folder(dest)
        # Killed while big.bin is partly written: a kill between files
        # proves nothing about a file in flight (the first version of this
        # test killed after a small file had finished, and passed with the
        # source unlinked before its copy).
        partial = os.path.join(dest, "big.bin")
        deadline = time.monotonic() + 60
        copied = 0
        while time.monotonic() < deadline:
            try:
                copied = os.path.getsize(partial)
            except OSError:
                copied = 0
            if 0 < copied < self.BIG:
                break
            time.sleep(0.001)
        self.app.kill()
        self.assertTrue(0 < copied < self.BIG,
                        "the kill did not land inside big.bin's copy (%d of "
                        "%d bytes): the scenario did not happen"
                        % (copied, self.BIG))

        at_src = hashes(self.root) if os.path.isdir(self.root) else {}
        at_dest = hashes(dest) if os.path.isdir(dest) else {}
        for rel, h in before.items():
            self.assertTrue(at_src.get(rel) == h or at_dest.get(rel) == h,
                            "%s is whole at neither root" % rel)

        # The app still starts on what it was left with.
        self.app = _app.App(backend=_backend(),
                            config_dir=self.app.config_dir)
        self.app.start()
        self.app.wait_for(lambda f: _app.shown(f, "row-libs"), timeout=60,
                          what="Home after the interrupted move")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
