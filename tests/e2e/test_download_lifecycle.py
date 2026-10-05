"""A downloaded video and a music track, end to end against a real server:
the bytes arrive, and deleting takes them.

`tests/e2e/test_books.py` takes a *book* through the real download endpoint.
Substitution (the copy playing instead of the stream) and whose viewing a
playback is filed under are asserted at the keyboard of the real app, in
`test_offline_ui.py` (TheLocalCopyStandsInTest, TwoProfilesOfflineTest).

Each one is an acceptance assertion for the offline-sync work:

* **the bytes arrive** — `_download` against `/Items/{id}/Download`, into a
  real store, with the catalog row agreeing with the file;
* **delete removes the files** — a row that outlives its bytes is how a
  re-download comes back already watched.

Slow by construction: it moves real media. It picks the smallest video the
library offers so the cost is a few megabytes rather than a few hundred.
"""

import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

import _e2e  # noqa: E402

from jellyfin_mpv_shim.sync.db import SyncDB  # noqa: E402
from jellyfin_mpv_shim.sync.manager import SyncManager  # noqa: E402

#: The saved-login uuid these tests pretend to have signed in under. A real
#: one is a uuid4 in `users.json`; nothing here reads the registry, and
#: `get_client` is overridden, so the value only has to be consistent.
LOGIN = "e2e-download"


class _DownloadCase(unittest.TestCase):
    """One session, one throwaway store, one smallest-possible video."""

    @classmethod
    def setUpClass(cls):
        cls.session = _e2e.Session()
        cls.server_id = cls.session.server_id()

    @classmethod
    def tearDownClass(cls):
        cls.session.stop()

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="jms-e2e-dl-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.m = SyncManager()
        self.m.root = self.root
        self.m.db = SyncDB(os.path.join(self.root, "catalog.db"))
        self.addCleanup(self.m.db.close)
        self.m.get_client = lambda uuid: self.session.client
        self.item = self._smallest_video()

    def _smallest_video(self):
        """The cheapest thing to move that is still a video.

        Sorted by the declared source size rather than by name, because this
        runs over a real network and an arbitrary pick could be a feature
        film. A video with no usable size is skipped rather than guessed at —
        that is the `_declared_bytes` None case, and it belongs to the reaper's
        tests rather than to these.
        """
        best = None
        for lib, kind in (("Movies", "Movie"), ("Shows", "Episode")):
            try:
                items = self.session.find_all(library=lib, item_type=kind,
                                              fields="MediaSources")
            except Exception:
                continue
            for it in items:
                src = (it.get("MediaSources") or [{}])[0]
                size = src.get("Size") or 0
                if size and (best is None or size < best[0]):
                    best = (size, it)
        if best is None:
            self.skipTest("no video with a declared size in this library")
        return best[1]

    def _download_it(self):
        added = self.m.enqueue(LOGIN, self.item["Id"], self.item["Type"],
                               include_watched=True)
        self.assertEqual(added, 1, "the item was not queued for download")
        row = self.m.db.get(self.item["Id"])
        self.m._download(row)
        return self.m.db.get(self.item["Id"])


@_e2e.require_server
class TheBytesArriveTest(_DownloadCase):
    def test_a_video_downloads_and_the_row_agrees_with_the_file(self):
        row = self._download_it()
        self.assertEqual(row["status"], "complete", "the video did not download")
        path = os.path.join(self.root, row["file_path"])
        self.assertTrue(os.path.exists(path), "the row names a file that is not there")
        self.assertGreater(row["size_bytes"], 0)
        self.assertEqual(os.path.getsize(path), row["size_bytes"],
                         "the row's byte count and the file disagree")

    def test_the_row_knows_which_server_it_came_from(self):
        """An orphan is a recognised state, but a fresh download is not one:
        a row homed to nothing here would mean every scoped read answers for
        it, and the claim door would later have to reap it on byte evidence
        rather than simply recognising it."""
        row = self._download_it()
        self.assertEqual(row["content_server_id"], self.server_id)


@_e2e.require_server
class DeletingTakesTheFilesTest(_DownloadCase):
    def test_the_row_and_the_bytes_go_together(self):
        """A row that outlives its bytes re-downloads; bytes that outlive
        their row are an orphan directory the sweep has to reason about.
        Neither is what Delete means."""
        row = self._download_it()
        path = os.path.join(self.root, row["file_path"])
        self.assertTrue(os.path.exists(path))

        self.m.delete(self.item["Id"])

        self.assertIsNone(self.m.db.get(self.item["Id"]), "the catalog row survived")
        self.assertFalse(os.path.exists(path), "the media file survived the delete")
        self.assertFalse(os.path.isdir(os.path.dirname(path)),
                         "the item's directory was left behind")


@_e2e.require_server
class MusicDownloadsToolTest(_DownloadCase):
    """Audio is the other download shape, and it was the one with no
    end-to-end coverage at all.

    It is not a video with a different extension. `Played` on a track is the
    only progress signal Jellyfin gives for music -- the server declines to
    count a short track as a play, so `PlayCount` and the resume position are
    legitimately zero on something finished many times -- and a download that
    keyed "does this have progress" on either would make every music download
    invisible to it. The download path itself has no such quirk, which is
    exactly why it is worth asserting rather than assuming.
    """

    def setUp(self):
        # Deliberately not `_DownloadCase.setUp`'s smallest *video*.
        unittest.TestCase.setUp(self)
        self.root = tempfile.mkdtemp(prefix="jms-e2e-dl-audio-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self.m = SyncManager()
        self.m.root = self.root
        self.m.db = SyncDB(os.path.join(self.root, "catalog.db"))
        self.addCleanup(self.m.db.close)
        self.m.get_client = lambda uuid: self.session.client
        self.item = self._smallest_track()

    def _smallest_track(self):
        best = None
        for lib in ("Music", "Bulk Music"):
            try:
                items = self.session.find_all(library=lib, item_type="Audio",
                                              fields="MediaSources")
            except Exception:
                continue
            for it in items:
                src = (it.get("MediaSources") or [{}])[0]
                size = src.get("Size") or 0
                if size and (best is None or size < best[0]):
                    best = (size, it)
        if best is None:
            self.skipTest("no audio track with a declared size")
        return best[1]

    def test_a_track_downloads_and_the_row_agrees_with_the_file(self):
        row = self._download_it()
        self.assertEqual(row["status"], "complete", "the track did not download")
        path = os.path.join(self.root, row["file_path"])
        self.assertTrue(os.path.exists(path))
        self.assertEqual(os.path.getsize(path), row["size_bytes"])
        self.assertGreater(row["size_bytes"], 0)

    def test_it_keeps_the_container_the_server_served(self):
        """A track that lands without its extension is one the desktop cannot
        route and mpv has to sniff."""
        row = self._download_it()
        served = ((self.item.get("MediaSources") or [{}])[0]
                  .get("Container") or "").split(",")[0]
        if not served:
            self.skipTest("the server declared no container for this track")
        self.assertEqual((row["ext"] or "").lower(), served.lower())
        self.assertTrue(row["file_path"].lower().endswith(served.lower()))

    def test_the_track_is_held_for_its_own_server(self):
        self._download_it()
        self.assertTrue(self.m.db.is_complete(self.item["Id"],
                                              server_id=self.server_id))

    def test_deleting_a_track_takes_its_bytes(self):
        row = self._download_it()
        path = os.path.join(self.root, row["file_path"])
        self.m.delete(self.item["Id"])
        self.assertIsNone(self.m.db.get(self.item["Id"]))
        self.assertFalse(os.path.exists(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
