"""The startup sweep, on a real filesystem, where it really deletes.

`_reconcile_disk` is the one place this feature removes a directory it was not
told to remove: on every launch it walks the store and takes away per-item
directories the catalog has no row for. The fast suite covers its rules with a
temp dir and a hand-built catalog; what it cannot cover is the thing that
actually costs something, which is **what survives on disk afterwards** when
the folder also holds files the user put there.

That matters because the user chooses this folder. The settings field says the
app manages it, not that the app owns everything under it, and a person who
points it at an existing media folder has done nothing wrong. So the sweep is
bounded three ways and each bound is a separate test here:

* it only ever descends into ``<root>/server``, never the root itself;
* it refuses to run at all unless the catalog reads AND holds at least one
  row, because "no rows" is not evidence that everything present is an orphan;
* inside the store it removes only children *shaped like an item id this app
  would have written*, and says so in the log for anything else.

The adversarial case is out of scope by the owner's own framing: a directory
deliberately named like a 32-character hex id, placed inside the store, is
indistinguishable from ours and is not defended against.

Run under `run_integration.py`; it needs no display, no mpv and no server.
"""

import os
import shutil
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402

from jellyfin_mpv_shim.sync import manager as manager_module  # noqa: E402
from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    STORE_DIR, STATUS_COMPLETE, ORIGIN_USER)

CONTENT_SERVER = "5f0d2c1ab6e4478aa1c4f0f4c0d1e2f3"
HELD = "0" * 31 + "1"           # shaped like an id, and the catalog holds it
STRANDED = "f" * 31 + "e"       # shaped like an id, no row: this one goes
MEDIA = b"x" * 4096


class SweepTestCase(h.TmpDirTest):
    def make_store(self, rows=1):
        root = os.path.join(self.tmp, "store")
        m = manager_module.SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        for name in ("_playback_source", "_download_artwork", "_download_subs",
                     "_download_trickplay", "_download_segments",
                     "_download_series_art", "_download_season_art"):
            setattr(m, name, lambda *a, **k: None)
        m._open_and_run()
        self.addCleanup(m.stop)
        for i in range(rows):
            self._hold(m, HELD[:-1] + str(i + 1))
        return m

    def _hold(self, m, item_id):
        """A complete download the catalog knows about."""
        d = os.path.join(m.root, STORE_DIR, item_id)
        os.makedirs(d, exist_ok=True)
        media = os.path.join(d, "media.mkv")
        with open(media, "wb") as fh:
            fh.write(MEDIA)
        m.db.upsert({
            "item_id": item_id, "server_uuid": "uuid",
            "content_server_id": CONTENT_SERVER, "type": "Movie",
            "name": "Held", "file_path": os.path.relpath(media, m.root),
            "status": STATUS_COMPLETE, "size_bytes": len(MEDIA),
            "downloaded_bytes": len(MEDIA), "origin": ORIGIN_USER,
        })
        return d

    def _users_folder(self, m, name, inside_store=True):
        """Something of the user's, with a file in it so an empty-directory
        cleanup could not be what removed it."""
        parent = os.path.join(m.root, STORE_DIR) if inside_store else m.root
        d = os.path.join(parent, name)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, "holiday.mp4"), "wb") as fh:
            fh.write(b"theirs")
        return d


class WhatTheSweepMayNotTouchTest(SweepTestCase):
    def test_a_folder_of_the_users_inside_the_store_survives(self):
        """Named the way a person names things, so it fails the id test."""
        m = self.make_store()
        theirs = self._users_folder(m, "Holiday 2019")

        m._reconcile_disk()

        self.assertTrue(os.path.isdir(theirs),
                        "the sweep deleted a folder the user put in the store")
        self.assertTrue(os.path.exists(os.path.join(theirs, "holiday.mp4")))

    def test_the_root_itself_is_never_walked(self):
        """`<root>` is the folder the user chose and may share; only
        `<root>/server` is ours. A stranded-looking directory beside the store
        is not the sweep's business even when it *is* shaped like an id."""
        m = self.make_store()
        beside = self._users_folder(m, STRANDED, inside_store=False)

        m._reconcile_disk()

        self.assertTrue(os.path.isdir(beside),
                        "the sweep walked out of the store and into the "
                        "folder the user chose")

    def test_an_empty_catalog_sweeps_nothing(self):
        """No rows is not "everything here is an orphan" -- it is equally the
        catalog that has just been restored, or lost. Deleting on it turns a
        recoverable state into an unrecoverable one."""
        m = self.make_store(rows=0)
        stranded = self._users_folder(m, STRANDED)

        m._reconcile_disk()

        self.assertTrue(os.path.isdir(stranded),
                        "an empty catalog authorised a delete")

    def test_an_unreadable_catalog_sweeps_nothing(self):
        m = self.make_store()
        stranded = self._users_folder(m, STRANDED)
        m.db.healthy = lambda: False

        m._reconcile_disk()

        self.assertTrue(os.path.isdir(stranded),
                        "a catalog that does not read authorised a delete")


class WhatTheSweepIsForTest(SweepTestCase):
    """The control side. Without these the class above passes by the sweep
    doing nothing at all, which is the shape a broken guard hides in."""

    def test_a_stranded_item_directory_is_removed(self):
        m = self.make_store()
        stranded = os.path.join(m.root, STORE_DIR, STRANDED)
        os.makedirs(stranded, exist_ok=True)
        with open(os.path.join(stranded, "media.mkv"), "wb") as fh:
            fh.write(MEDIA)

        m._reconcile_disk()

        self.assertFalse(os.path.exists(stranded),
                         "a directory with no row and no manifest survived")

    def test_the_held_download_is_left_exactly_where_it_was(self):
        m = self.make_store()
        held = os.path.join(m.root, STORE_DIR, HELD[:-1] + "1")

        m._reconcile_disk()

        self.assertTrue(os.path.isdir(held), "the sweep ate a held download")
        self.assertEqual(
            os.path.getsize(os.path.join(held, "media.mkv")), len(MEDIA))

    def test_a_row_whose_file_vanished_is_requeued_rather_than_dropped(self):
        """The other half of the reconcile. A file that is gone is the app's
        anomaly, not the user's instruction, so the row goes back to pending
        instead of being deleted as though they had asked."""
        m = self.make_store()
        item_id = HELD[:-1] + "1"
        shutil.rmtree(os.path.join(m.root, STORE_DIR, item_id))

        m._reconcile_disk()

        row = m.db.get(item_id)
        self.assertIsNotNone(row, "the row went with the file")
        self.assertEqual(row["status"], "pending")


if __name__ == "__main__":
    unittest.main(verbosity=2)
