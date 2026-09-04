"""Reliability tests for the offline-sync download manager.

These exercise the resume/promote/size logic, the cancel-before-complete race,
the startup orphan sweep, and shutdown (worker join + catalog close) without
touching the network — `_stream` is stubbed or fed a fake `requests` response,
and the side-artwork/subtitle downloads are no-ops.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import errno
import glob
import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from jellyfin_mpv_shim.sync import manager as manager_module
from jellyfin_mpv_shim.sync.manager import SyncManager
from jellyfin_mpv_shim.sync.db import (SyncDB, STATUS_PENDING, STATUS_COMPLETE,
                                       STATUS_ERROR, ORIGIN_USER,
                                       ORIGIN_AUTO_NEXT_UP)


import contextlib


@contextlib.contextmanager
def _short_join_timeout(seconds=0.1):
    """Shrink the worker-join timeout so a "worker won't stop" test doesn't
    actually wait the production 10s."""
    original = manager_module.STOP_JOIN_TIMEOUT
    manager_module.STOP_JOIN_TIMEOUT = seconds
    try:
        yield
    finally:
        manager_module.STOP_JOIN_TIMEOUT = original


class FakeJellyfin:
    # include_apikey mirrors the apiclient's own signature. The sync manager
    # passes False and authenticates by header instead; a fake that did not
    # take the argument would make every download here a TypeError.
    def download_url(self, item_id, include_apikey=True):
        return "http://example/download/%s" % item_id


class FakeHttp:
    def _get_authenication_header(self):
        return 'MediaBrowser Client="test", Token="TESTTOKEN"'


class FakeConfig:
    # Same origin as FakeJellyfin's urls, so _headers_for actually attaches
    # the header in these tests rather than short-circuiting to {}.
    data = {"auth.server": "http://example", "auth.token": "TESTTOKEN"}


class FakeClient:
    def __init__(self):
        self.jellyfin = FakeJellyfin()
        self.http = FakeHttp()
        self.config = FakeConfig()


class FakeResp:
    """Minimal stand-in for a streaming requests.Response context manager."""

    def __init__(self, status=200, headers=None, body=b""):
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            err = manager_module.requests.HTTPError("HTTP %d" % self.status_code)
            err.response = self
            raise err

    def iter_content(self, size):
        for i in range(0, len(self._body), size):
            yield self._body[i:i + size]


#: A directory name the orphan sweep will act on. It is a Jellyfin item id
#: (32 hex) because that is the *only* name this app gives an item directory,
#: and the sweep now says so: an "orphan" that is not shaped like one is
#: somebody else's folder inside our root and is left alone.
ORPHAN_ID = "f" * 32


def make_manager(root, cleanup=None, clients=None):
    """``clients`` maps server uuid -> client, so a test can make one server
    unresolvable. Omitted, every uuid resolves — which is the shape that hid
    the head-of-line block in the download queue for as long as it existed."""
    m = SyncManager()
    m.root = root
    m.db = SyncDB(os.path.join(root, "catalog.db"))
    if cleanup is not None:
        cleanup(lambda: m.db.close())
    if clients is None:
        m.get_client = lambda uuid: FakeClient()
    else:
        m.get_client = lambda uuid: clients.get(uuid)
    # Stub the side downloads / playback-info so _download stays offline.
    m._playback_source = lambda *a, **k: None
    m._download_artwork = lambda *a, **k: None
    m._download_subs = lambda *a, **k: None
    m._download_trickplay = lambda *a, **k: None
    m._download_segments = lambda *a, **k: None
    m._download_series_art = lambda *a, **k: None
    m._download_season_art = lambda *a, **k: None
    return m


def add_row(m, item_id, server_id="srv", status=STATUS_PENDING, size_bytes=0,
            file_path=None, server_uuid="uuid", origin=ORIGIN_USER):
    m.db.upsert({
        "item_id": item_id,
        "server_id": server_id,
        "server_uuid": server_uuid,
        "origin": origin,
        "type": "Movie",
        "name": item_id,
        "series_id": None, "series_name": None, "season_id": None,
        "parent_index": None, "index_number": None,
        "media_source_id": "ms",
        "file_path": file_path,
        "ext": "mkv",
        "size_bytes": size_bytes,
        "downloaded_bytes": 0,
        "status": status,
        "runtime_ticks": None,
        "item_json": json.dumps({"Id": item_id, "Type": "Movie"}),
        "source_json": json.dumps({"Id": "ms"}),
        "userdata_json": "{}",
        "added_at": 1,
    })


class TmpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)


class DownloadCommitTest(TmpTest):
    def test_short_read_stays_pending_and_keeps_size(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        # Stream reports only 50 of the expected 100 bytes.
        m._stream = lambda *a, **k: (50, 100)
        m._download(m.db.get("a"))
        row = m.db.get("a")
        self.assertEqual(row["status"], STATUS_PENDING)
        # size_bytes must not be clobbered with the short length.
        self.assertEqual(row["size_bytes"], 100)
        self.assertEqual(row["downloaded_bytes"], 50)

    def test_full_read_marks_complete_and_promotes_part(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        item_dir = m._item_dir(m.db.get("a"))

        # headers= is passed by _download so the media request carries the
        # Authorization header, and on_headers= so a book download can read
        # the served filename back off the response. A fake missing either
        # makes the download a TypeError and the row lands in 'error' --
        # which is how this signature stays honest.
        def fake_stream(url, dest, item_id, name, expected,
                        stopping=None, headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 100)
            return 100, 100

        m._stream = fake_stream
        m._download(m.db.get("a"))
        row = m.db.get("a")
        self.assertEqual(row["status"], STATUS_COMPLETE)
        media = os.path.join(item_dir, "media.mkv")
        self.assertTrue(os.path.exists(media))
        self.assertFalse(os.path.exists(media + ".part"))

    def test_cancel_after_last_chunk_is_honoured(self):
        # S4: a delete that lands after the final chunk but before COMPLETE must
        # not be lost. Simulate it by flagging the item cancelled from inside the
        # stream (i.e. right as it finishes).
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        item_dir = m._item_dir(m.db.get("a"))

        # headers= is passed by _download so the media request carries the
        # Authorization header, and on_headers= so a book download can read
        # the served filename back off the response. A fake missing either
        # makes the download a TypeError and the row lands in 'error' --
        # which is how this signature stays honest.
        def fake_stream(url, dest, item_id, name, expected,
                        stopping=None, headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 100)
            m._cancelled.add(item_id)  # delete raced in at the finish line
            return 100, 100

        m._stream = fake_stream
        m._download(m.db.get("a"))
        # Row deleted (delete honoured), files gone, not left COMPLETE.
        self.assertIsNone(m.db.get("a"))
        self.assertFalse(os.path.exists(item_dir))

    def test_active_item_cleared_after_commit(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)

        # headers= is passed by _download so the media request carries the
        # Authorization header, and on_headers= so a book download can read
        # the served filename back off the response. A fake missing either
        # makes the download a TypeError and the row lands in 'error' --
        # which is how this signature stays honest.
        def fake_stream(url, dest, item_id, name, expected,
                        stopping=None, headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 100)
            return 100, 100

        m._stream = fake_stream
        m._download(m.db.get("a"))
        self.assertIsNone(m._active_item)
        self.assertNotIn("a", m._cancelled)


class StreamResumeTest(TmpTest):
    def test_full_part_promoted_without_request(self):
        # S6: a full-size .part must be promoted, never re-requested (would 416).
        m = make_manager(self.tmp, self.addCleanup)
        dest = os.path.join(self.tmp, "media.mkv")
        with open(dest + ".part", "wb") as fh:
            fh.write(b"x" * 100)

        def boom(*a, **k):
            raise AssertionError("must not hit the network for a full .part")

        orig = manager_module.requests.get
        manager_module.requests.get = boom
        try:
            size, total = m._stream("url", dest, "a", "a", 100)
        finally:
            manager_module.requests.get = orig
        self.assertEqual((size, total), (100, 100))

    def test_416_restart_from_scratch(self):
        # S6: a stale/over-long .part (unknown expected size) gets a 416; the
        # stream should drop it and restart cleanly rather than erroring.
        m = make_manager(self.tmp, self.addCleanup)
        dest = os.path.join(self.tmp, "media.mkv")
        with open(dest + ".part", "wb") as fh:
            fh.write(b"stale-oversized-partial")  # resume offset the server 416s

        responses = [
            FakeResp(status=416),
            FakeResp(status=200, headers={"Content-Length": "100"},
                     body=b"y" * 100),
        ]
        calls = []

        def fake_get(url, **kwargs):
            calls.append(kwargs.get("headers") or {})
            return responses.pop(0)

        orig = manager_module.requests.get
        manager_module.requests.get = fake_get
        try:
            size, total = m._stream("url", dest, "a", "a", 0)
        finally:
            manager_module.requests.get = orig
        self.assertEqual(size, 100)
        # Second request must be a full (Range-less) restart.
        self.assertEqual(calls[1], {})
        with open(dest + ".part", "rb") as fh:
            self.assertEqual(fh.read(), b"y" * 100)

    def test_resume_appends_with_range(self):
        m = make_manager(self.tmp, self.addCleanup)
        dest = os.path.join(self.tmp, "media.mkv")
        with open(dest + ".part", "wb") as fh:
            fh.write(b"a" * 40)

        resp = FakeResp(status=206, headers={"Content-Length": "60"},
                        body=b"b" * 60)
        seen = {}

        def fake_get(url, **kwargs):
            seen.update(kwargs.get("headers") or {})
            return resp

        orig = manager_module.requests.get
        manager_module.requests.get = fake_get
        try:
            size, total = m._stream("url", dest, "a", "a", 100)
        finally:
            manager_module.requests.get = orig
        self.assertEqual(seen.get("Range"), "bytes=40-")
        self.assertEqual(size, 100)
        self.assertEqual(total, 100)
        with open(dest + ".part", "rb") as fh:
            self.assertEqual(fh.read(), b"a" * 40 + b"b" * 60)


class ReconcileDiskTest(TmpTest):
    def test_missing_complete_file_requeued(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "gone", status=STATUS_COMPLETE,
                file_path="srv/gone/media.mkv")
        m._reconcile_disk()
        row = m.db.get("gone")
        self.assertEqual(row["status"], STATUS_PENDING)
        self.assertIsNone(row["file_path"])

    def test_present_complete_file_kept(self):
        m = make_manager(self.tmp, self.addCleanup)
        item_dir = os.path.join(self.tmp, "srv", "here")
        os.makedirs(item_dir)
        rel = os.path.join("srv", "here", "media.mkv")
        with open(os.path.join(self.tmp, rel), "wb") as fh:
            fh.write(b"x")
        add_row(m, "here", status=STATUS_COMPLETE, file_path=rel)
        m._reconcile_disk()
        self.assertEqual(m.db.get("here")["status"], STATUS_COMPLETE)
        self.assertTrue(os.path.isdir(item_dir))

    def test_orphan_dir_removed(self):
        m = make_manager(self.tmp, self.addCleanup)
        # A row for this server, because a server directory the catalog does
        # not name is not a sweep candidate at all. The dir this test used to
        # use was called "orphan", which is not a name this app ever writes:
        # the sweep passed only because it deleted anything it found, which
        # is the behaviour that ate `~/Videos/Holidays/2019 Italy`.
        add_row(m, ORPHAN_ID.replace("f", "e"), status=STATUS_PENDING)
        orphan = os.path.join(self.tmp, "srv", ORPHAN_ID)
        os.makedirs(orphan)
        m._reconcile_disk()
        self.assertFalse(os.path.exists(orphan))

    def test_a_dir_not_named_like_a_download_is_left(self):
        """The user's own folders, when the store shares a directory with
        them. Two levels down is an item dir's position, and this is the only
        thing that tells the two apart."""
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID, status=STATUS_PENDING)
        theirs = os.path.join(self.tmp, "srv", "2019 Italy")
        os.makedirs(theirs)
        m._reconcile_disk()
        self.assertTrue(os.path.exists(theirs))

    def test_a_server_dir_the_catalog_does_not_name_is_left(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID, server_id="srv", status=STATUS_PENDING)
        theirs = os.path.join(self.tmp, "Holidays", ORPHAN_ID)
        os.makedirs(theirs)
        m._reconcile_disk()
        self.assertTrue(os.path.exists(theirs),
                        "the sweep walked a directory no catalog row names")

    def test_an_unreadable_catalog_sweeps_nothing(self):
        """One corrupt page answers `[]` to every query, which reads as
        "none of this media is known". Measured: 60 downloads, one zeroed
        4 KiB page, zero survivors."""
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID, status=STATUS_COMPLETE)
        live = os.path.join(self.tmp, "srv", ORPHAN_ID)
        os.makedirs(live)
        m.db.healthy = lambda: False
        m._reconcile_disk()
        self.assertTrue(os.path.exists(live))

    def test_a_complete_download_with_no_row_is_re_adopted(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID.replace("f", "e"), status=STATUS_PENDING)
        item_dir = os.path.join(self.tmp, "srv", ORPHAN_ID)
        os.makedirs(item_dir)
        with open(os.path.join(item_dir, "item.json"), "w") as fh:
            json.dump({"Id": ORPHAN_ID, "Type": "Movie", "Name": "Film"}, fh)
        with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
            fh.write(b"x" * 10)
        m._reconcile_disk()
        row = m.db.get(ORPHAN_ID)
        self.assertIsNotNone(row, "a whole, playable download was deleted "
                                  "because the catalog had forgotten it")
        self.assertEqual(row["status"], STATUS_COMPLETE)
        self.assertEqual(row["name"], "Film")
        # Its server's uuid is not in the path; it comes from a surviving row.
        self.assertEqual(row["server_uuid"], "uuid")
        # Never auto: the reaper deletes those, and this row is an inference.
        self.assertEqual(row["origin"], ORIGIN_USER)

    def test_a_manifest_without_media_is_still_reclaimed(self):
        """The other half — an interrupted download, or the residue of a
        delete whose unlink failed, is what the sweep is actually for."""
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID.replace("f", "e"), status=STATUS_PENDING)
        item_dir = os.path.join(self.tmp, "srv", ORPHAN_ID)
        os.makedirs(item_dir)
        with open(os.path.join(item_dir, "item.json"), "w") as fh:
            json.dump({"Id": ORPHAN_ID}, fh)
        with open(os.path.join(item_dir, "media.mkv.part"), "wb") as fh:
            fh.write(b"x")
        m._reconcile_disk()
        self.assertFalse(os.path.exists(item_dir))

    def test_shared_art_caches_preserved(self):
        """The user-visible property: `playlist` was missing from
        RESERVED_STORE_DIRS for the life of the feature, so every start
        deleted the poster cache _download_playlist_art writes and nothing
        but re-downloading the playlist put it back.

        Two guards now hold this -- the reserved list and the item-id shape
        -- so this test alone does not say which. The one below does.
        """
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID, status=STATUS_PENDING)
        dirs = [os.path.join(self.tmp, "srv", kind, "id1")
                for kind in ("series", "season", "playlist")]
        for path in dirs:
            os.makedirs(path)
        m._reconcile_disk()
        for path in dirs:
            self.assertTrue(os.path.exists(path), path)

    def test_the_reserved_list_holds_on_its_own(self):
        """With the shape check answering yes to everything, the reserved
        list is the only thing left between the sweep and the shared caches.

        Written this way because emptying RESERVED_STORE_DIRS failed nothing:
        the names are 6-8 characters and _looks_like_item_id wants 32, so the
        test above was measuring the other guard and the constant was
        untested defence in depth.
        """
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID, status=STATUS_PENDING)
        # Named literally, NOT derived from RESERVED_STORE_DIRS: building the
        # fixture out of the constant under test means emptying the constant
        # empties the fixture and the test passes with nothing left to check.
        # (It did. That is the second tautology this one file has grown.)
        dirs = [os.path.join(self.tmp, "srv", kind, "id1")
                for kind in ("series", "season", "playlist")]
        for path in dirs:
            os.makedirs(path)
        original = manager_module._looks_like_item_id
        manager_module._looks_like_item_id = lambda name: True
        try:
            m._reconcile_disk()
        finally:
            manager_module._looks_like_item_id = original
        for path in dirs:
            self.assertTrue(os.path.exists(path), path)

    def test_a_download_that_cannot_be_described_is_left_alone(self):
        """_adopt_orphan answers "do not delete this" when it cannot read the
        manifest, which is an explicit delete-safety decision and had no
        coverage at all."""
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, ORPHAN_ID.replace("f", "e"), status=STATUS_PENDING)
        item_dir = os.path.join(self.tmp, "srv", ORPHAN_ID)
        os.makedirs(item_dir)
        with open(os.path.join(item_dir, "item.json"), "w") as fh:
            fh.write('{"Id": "trunc')          # a torn write
        with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
            fh.write(b"x" * 10)
        m._reconcile_disk()
        self.assertTrue(os.path.exists(item_dir),
                        "a download whose manifest could not be parsed was "
                        "deleted rather than left for a human to look at")

    def test_catalog_db_not_swept(self):
        # The catalog file lives directly in root and must survive the sweep.
        m = make_manager(self.tmp, self.addCleanup)
        m._reconcile_disk()
        self.assertTrue(os.path.exists(os.path.join(self.tmp, "catalog.db")))


class RelocateTest(TmpTest):
    def _seed_download(self, m):
        """A COMPLETE row with its media file on disk, so a move+reconcile keeps
        it (an orphan dir with no catalog row would be swept)."""
        rel = os.path.join("srv", "keep", "media.mkv")
        os.makedirs(os.path.join(m.root, "srv", "keep"))
        with open(os.path.join(m.root, rel), "wb") as fh:
            fh.write(b"x" * 100)
        add_row(m, "keep", status=STATUS_COMPLETE, file_path=rel)

    def test_move_to_new_folder(self):
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        self._seed_download(m)
        new = os.path.join(self.tmp, "drive2", "offline")

        ok, msg = m.relocate(new)
        self.assertTrue(ok, msg)
        self.assertEqual(os.path.abspath(m.root), os.path.abspath(new))
        self.assertTrue(os.path.exists(os.path.join(new, "catalog.db")))
        self.assertTrue(os.path.exists(
            os.path.join(new, "srv", "keep", "media.mkv")))
        self.assertFalse(os.path.exists(old))
        # Catalog reopened at the new root and the row survived reconcile.
        self.assertEqual(m.db.path, os.path.join(new, "catalog.db"))
        self.assertEqual(m.db.get("keep")["status"], STATUS_COMPLETE)

    def test_noop_when_path_unchanged(self):
        m = make_manager(self.tmp, self.addCleanup)
        ok, msg = m.relocate(self.tmp)
        self.assertTrue(ok)
        self.assertEqual(msg, "")

    def test_refuse_while_download_active(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._active_item = "busy"
        ok, msg = m.relocate(os.path.join(self.tmp, "elsewhere"))
        self.assertFalse(ok)
        self.assertIn("in progress", msg)
        self.assertEqual(m.root, self.tmp)  # unchanged

    def test_refuse_when_the_worker_will_not_stop(self):
        """The _active_item check is sampled BEFORE stop(), and the chunk loop
        only notices _stop between chunks — a stalled connection parks it in a
        socket read for up to the 60s read timeout, well past
        STOP_JOIN_TIMEOUT. stop() used to log a warning and let the move
        proceed anyway, so the tree was renamed out from under an open .part
        handle while _open_and_run started a SECOND worker on the same rows at
        the new root: two writers interleaving into one file.
        """
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self._seed_download(m)
        # A worker that ignores _stop, exactly like one parked in a socket read.
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        worker = threading.Thread(target=lambda: stuck.wait(30), daemon=True)
        worker.start()
        m._worker = worker

        with _short_join_timeout():
            ok, msg = m.relocate(os.path.join(self.tmp, "drive2"))

        self.assertFalse(ok)
        self.assertIn("still finishing", msg)
        # Nothing moved, and the catalog is open again where the files are.
        self.assertEqual(os.path.abspath(m.root), os.path.abspath(old))
        self.assertTrue(os.path.exists(
            os.path.join(old, "srv", "keep", "media.mkv")))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "drive2",
                                                     "catalog.db")))
        # And the guard was released, or every later enqueue/delete no-ops.
        self.assertFalse(m._relocating)
        self.assertIsNotNone(m.db.get("keep"))

    def test_stop_reports_whether_the_worker_unwound(self):
        # relocate's refusal rests on this return value, so assert it directly
        # rather than only through the path above.
        m = make_manager(self.tmp, self.addCleanup)
        self.assertTrue(m.stop(), "a quiescent manager should report a clean stop")

        m2 = make_manager(self.tmp, self.addCleanup)
        stuck = threading.Event()
        self.addCleanup(stuck.set)
        worker = threading.Thread(target=lambda: stuck.wait(30), daemon=True)
        worker.start()
        m2._worker = worker
        with _short_join_timeout():
            self.assertFalse(m2.stop())

    def test_refuse_target_with_existing_catalog(self):
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self._seed_download(m)
        target = os.path.join(self.tmp, "taken")
        os.makedirs(target)
        open(os.path.join(target, "catalog.db"), "w", encoding="utf-8").close()
        ok, msg = m.relocate(target)
        self.assertFalse(ok)
        self.assertIn("already contains", msg)
        self.assertEqual(m.root, old)

    def test_progress_reported_and_monotonic(self):
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        self._seed_download(m)  # 100-byte media file + a catalog.db
        calls = []
        m.relocate(os.path.join(self.tmp, "new"), progress=lambda c, t: calls.append((c, t)))
        self.assertTrue(calls)
        totals = {t for _c, t in calls}
        self.assertEqual(len(totals), 1)  # total is stable across the move
        total = totals.pop()
        self.assertGreaterEqual(total, 100)  # at least the media file's bytes
        copied = [c for c, _t in calls]
        self.assertEqual(copied, sorted(copied))  # monotonic
        self.assertEqual(calls[-1], (total, total))  # ends at 100%

    def test_cross_drive_copy_fallback_moves_and_reports_interim(self):
        # Force os.rename to fail with EXDEV so _move_tree takes the copy path a
        # real cross-drive move would. A file larger than PROGRESS_STEP must emit
        # at least one interim (0 < copied < total) progress tick.
        import errno
        from unittest import mock
        old = os.path.join(self.tmp, "old")
        os.makedirs(os.path.join(old, "srv", "big"))
        rel = os.path.join("srv", "big", "media.mkv")
        size = manager_module.PROGRESS_STEP * 2 + 1234
        with open(os.path.join(old, rel), "wb") as fh:
            fh.write(b"z" * size)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        add_row(m, "big", server_id="srv", status=STATUS_COMPLETE, file_path=rel)
        new = os.path.join(self.tmp, "new")
        calls = []

        def boom(src, dst):
            raise OSError(errno.EXDEV, "cross-device link")

        with mock.patch("jellyfin_mpv_shim.sync.manager.os.rename", side_effect=boom):
            ok, msg = m.relocate(new, progress=lambda c, t: calls.append((c, t)))
        self.assertTrue(ok, msg)
        self.assertEqual(os.path.getsize(os.path.join(new, rel)), size)
        self.assertFalse(os.path.exists(old))
        interim = [c for c, t in calls if 0 < c < t]
        self.assertTrue(interim, "expected at least one interim progress tick")

    def test_copy_tree_copies_bytes_and_advances_state(self):
        m = make_manager(self.tmp, self.addCleanup)
        src = os.path.join(self.tmp, "src")
        os.makedirs(os.path.join(src, "sub"))
        with open(os.path.join(src, "a.bin"), "wb") as fh:
            fh.write(b"a" * 1500)
        with open(os.path.join(src, "sub", "b.bin"), "wb") as fh:
            fh.write(b"b" * 500)
        dst = os.path.join(self.tmp, "dst")
        state = [0, 2000, 0]
        seen = []
        m._copy_tree(src, dst, state, lambda c, t: seen.append((c, t)))
        self.assertEqual(state[0], 2000)  # all bytes accounted for
        with open(os.path.join(dst, "a.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"a" * 1500)
        with open(os.path.join(dst, "sub", "b.bin"), "rb") as fh:
            self.assertEqual(fh.read(), b"b" * 500)

    def test_relocating_flag_blocks_enqueue_and_delete(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", status=STATUS_COMPLETE, file_path="srv/a/media.mkv")
        m._relocating = True
        self.assertEqual(m.enqueue("uuid", "x", "Movie"), 0)
        m.delete(item_id="a")  # must be a no-op, not touch the row
        self.assertIsNotNone(m.db.get("a"))

    def test_move_failure_leaves_downloads_in_place(self):
        """The move raises before it starts. Stubbing `_move_tree` is what
        makes this the EASY half -- nothing was copied, so nothing can have
        been half-deleted. The hard half is the test below, which must not
        be folded into this one: a stub cannot fail partway."""
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        self._seed_download(m)
        m._move_tree = lambda *a, **k: (_ for _ in ()).throw(OSError("disk full"))
        ok, msg = m.relocate(os.path.join(self.tmp, "drive2"))
        self.assertFalse(ok)
        self.assertEqual(m.root, old)  # rolled back
        # Downloads still readable at the old location.
        self.assertEqual(m.db.get("keep")["status"], STATUS_COMPLETE)
        self.assertTrue(os.path.exists(
            os.path.join(old, "srv", "keep", "media.mkv")))

    def test_a_cross_drive_move_that_fails_partway_destroys_nothing(self):
        """A real partial move, which is where the downloads were lost.

        `_move_tree` used to copy each entry and delete the original
        immediately, so a cross-device move that copied `catalog.db` and then
        ran out of space on the media had *already* destroyed the catalog.
        The recovery reopens the old root, finds no catalog, creates an empty
        one -- and `_open_and_run`'s startup `_reconcile_disk` then removes
        every surviving media directory as an orphan, because an empty
        catalog says nothing on disk is known. The user was told their
        downloads had been left in place.

        Not a stub: the whole defect lives in the half-finished state, which
        is exactly what a stubbed `_move_tree` cannot produce.

        The listdir order is pinned rather than left to the filesystem
        because the catastrophic case is specifically catalog-then-media --
        with the media first, the catalog survives and the reconcile is
        harmless. Sorted puts `catalog.db` before `srv`.
        """
        import errno
        from unittest import mock
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        self._seed_download(m)
        media = os.path.join(old, "srv", "keep", "media.mkv")
        real_listdir = os.listdir
        real_copy_tree = m._copy_tree

        def copy_tree(src, dst, state, progress):
            # The disk fills on the media, after the catalog has gone across.
            if os.path.basename(src) == "srv":
                raise OSError(errno.ENOSPC, "No space left on device")
            return real_copy_tree(src, dst, state, progress)

        m._copy_tree = copy_tree

        def no_rename(src, dst):
            raise OSError(errno.EXDEV, "cross-device link")

        with mock.patch("jellyfin_mpv_shim.sync.manager.os.rename",
                        side_effect=no_rename), \
                mock.patch("jellyfin_mpv_shim.sync.manager.os.listdir",
                           side_effect=lambda p: sorted(real_listdir(p))):
            ok, msg = m.relocate(os.path.join(self.tmp, "drive2"))

        self.assertFalse(ok)
        self.assertTrue(msg)
        self.assertEqual(m.root, old)
        # The media itself. This is the assertion the old stubbed test could
        # never make fail, and the one the user cares about.
        self.assertTrue(os.path.exists(media),
                        "a failed move deleted the downloaded media")
        # ...and the catalog still knows about it, so it is still reachable
        # from the UI rather than being an unreferenced file on disk.
        row = m.db.get("keep")
        self.assertIsNotNone(row, "a failed move emptied the catalog")
        self.assertEqual(row["status"], STATUS_COMPLETE)


    def test_a_disk_full_move_says_so(self):
        """ENOSPC is the one failure the user can act on, so it gets its own
        wording. The generic message sent people hunting for a bug."""
        import errno
        m = make_manager(os.path.join(self.tmp, "old"), self.addCleanup)
        self.addCleanup(m.stop)
        os.makedirs(m.root, exist_ok=True)
        self._seed_download(m)

        def full(*a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")

        m._move_tree = full
        ok, msg = m.relocate(os.path.join(self.tmp, "drive2"))
        self.assertFalse(ok)
        self.assertIn("space", msg.lower())

    def test_a_failed_copy_leaves_no_partial_destination(self):
        """A half-copied destination directory would be skipped by the
        `already there` guard on the next attempt, so retrying after freeing
        space would silently finish a partial tree."""
        import errno
        from unittest import mock
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        self._seed_download(m)
        new = os.path.join(self.tmp, "drive2")
        real_copy_tree = m._copy_tree

        def die_after_writing_something(src, dst, state, progress):
            real_copy_tree(src, dst, state, progress)   # creates dst for real
            raise OSError(errno.ENOSPC, "No space left on device")

        m._copy_tree = die_after_writing_something

        def no_rename(src, dst):
            raise OSError(errno.EXDEV, "cross-device link")

        with mock.patch("jellyfin_mpv_shim.sync.manager.os.rename",
                        side_effect=no_rename):
            ok, _msg = m.relocate(new)
        self.assertFalse(ok)
        self.assertFalse(os.path.exists(os.path.join(new, "srv")),
                         "a half-copied destination was left behind")
        self.assertTrue(os.path.exists(
            os.path.join(old, "srv", "keep", "media.mkv")))


class ReconcileGateTest(TmpTest):
    """`_reconcile_disk` deletes on-disk item dirs with no catalog row. An
    empty catalog therefore says "delete everything", which is precisely the
    state a failed relocation used to leave behind."""

    def test_a_brand_new_catalog_does_not_sweep_existing_media(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(os.path.join(root, "srv", "keep"))
        media = os.path.join(root, "srv", "keep", "media.mkv")
        with open(media, "wb") as fh:
            fh.write(b"x" * 100)
        self.assertFalse(os.path.exists(os.path.join(root, "catalog.db")))

        m = SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        self.addCleanup(m.stop)
        m._open_and_run()          # creates catalog.db, then would sweep
        self.addCleanup(lambda: m.db.close())

        self.assertTrue(os.path.exists(media),
                        "the orphan sweep ran against a catalog it had just "
                        "created and deleted media it could not know about")

    def test_an_existing_catalog_still_sweeps_orphans(self):
        """The other half: the sweep must still do its job, or this fix has
        quietly disabled a feature instead of gating it."""
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        m = make_manager(root, self.addCleanup)   # creates the catalog
        self.addCleanup(m.stop)
        # A row, so "srv" is a server directory the catalog names, and an
        # item-shaped orphan beside it. Both are now required: this used to
        # sweep a dir called "nobody" out of a catalog holding no rows at
        # all, which is the same permission the corrupt-catalog wipe used.
        add_row(m, ORPHAN_ID.replace("f", "e"), status=STATUS_PENDING)
        m.db.close()
        orphan = os.path.join(root, "srv", ORPHAN_ID)
        os.makedirs(orphan)
        m._open_and_run()
        self.assertFalse(os.path.exists(orphan),
                         "an orphan next to a real catalog was not swept")


class StopTest(TmpTest):
    def test_stop_joins_worker_and_closes_db(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._stop = False
        m._worker = threading.Thread(target=m._run, daemon=True)
        m._worker.start()
        time.sleep(0.1)
        m.stop()
        self.assertFalse(m._worker.is_alive())
        self.assertIsNone(m.db._conn)

    def test_db_close_is_safe_and_idempotent(self):
        m = make_manager(self.tmp, self.addCleanup)
        m.db.close()
        m.db.close()  # no raise on a second close
        self.assertIsNone(m.db._conn)
        # Reads after close degrade to empty rather than crashing.
        self.assertIsNone(m.db.get("anything"))


if __name__ == "__main__":
    unittest.main()


class SupersededWorkerTest(TmpTest):
    """stop() joins with a timeout and gives up if the worker is still busy,
    but leaves it running. _open_and_run then set _stop back to False, which
    re-armed that abandoned thread: two workers against one catalog,
    interleaving appends into the same .part file."""

    def test_a_superseded_worker_stops_even_when_stop_is_cleared(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._generation = 7
        stopped = []

        # Drive _run's loop body once as generation 7, then supersede it.
        def fake_download(row, stopping=None):
            m._generation = 8          # a newer worker took over
            stopped.append(stopping())
        m._download = fake_download
        add_row(m, "a")
        t = threading.Thread(target=lambda: m._run(7))
        t.start()
        t.join(timeout=5)
        self.assertFalse(t.is_alive(), "the superseded worker never exited")
        self.assertTrue(stopped and stopped[0],
                        "the stop check ignored the generation bump")

    def test_the_current_worker_is_not_stopped_by_its_own_generation(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._generation = 3
        seen = []

        def fake_download(row, stopping=None):
            seen.append(stopping())
            m._stop = True             # end the loop the normal way
        m._download = fake_download
        add_row(m, "a")
        m._run(3)
        self.assertEqual(seen, [False])

    def test_each_start_takes_a_new_generation(self):
        m = make_manager(self.tmp, self.addCleanup)
        before = m._generation
        m._stop = True                 # keep the worker from doing anything
        m._open_and_run()
        self.addCleanup(m.stop)
        self.assertGreater(m._generation, before)


class RelocateGuardTest(TmpTest):
    """_relocating was cleared before the catalog was reopened, leaving a
    window where self.db was the closed old handle and the enqueue/delete
    guards were open. An enqueue landing there wrote nothing and still
    reported success."""

    def test_the_guard_outlives_the_reopen(self):
        # Source and destination must be siblings: relocating into a
        # subdirectory of the source makes _move_tree recurse into itself.
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        seen = []
        real_open = m._open_and_run

        def watched_open():
            # The guard must still be set while the catalog is being
            # reopened -- that is the whole window.
            seen.append(m._relocating)
            real_open()
        m._open_and_run = watched_open
        ok, msg = m.relocate(os.path.join(self.tmp, "drive2", "offline"))
        self.assertTrue(ok, msg)
        self.assertEqual(seen, [True])
        self.assertFalse(m._relocating, "the guard was left set")

    def test_the_guard_is_cleared_after_a_failed_move(self):
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self.addCleanup(m.stop)
        m._move_tree = lambda *a, **k: (_ for _ in ()).throw(OSError("nope"))
        ok, msg = m.relocate(os.path.join(self.tmp, "drive2", "offline"))
        self.assertFalse(ok)
        self.assertTrue(msg)
        self.assertFalse(m._relocating, "a failed move left the guard set")


class QueryClosedCatalogTest(TmpTest):
    """_query tested _conn outside its lock, so a close() landing between
    the check and the execute raised AttributeError -- which
    `except sqlite3.Error` does not catch."""

    def test_a_close_during_a_read_does_not_raise(self):
        from jellyfin_mpv_shim.sync.db import SyncDB
        db = SyncDB(os.path.join(self.tmp, "c.db"))
        errors = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    db.list()
                except Exception as exc:       # noqa: BLE001 - that's the point
                    errors.append(exc)
                    return

        t = threading.Thread(target=reader)
        t.start()
        time.sleep(0.05)
        db.close()
        time.sleep(0.05)
        stop.set()
        t.join(timeout=5)
        self.assertEqual(errors, [], "a concurrent close escaped as %r"
                         % (errors[:1],))


class DownloadSegmentsTest(unittest.TestCase):
    """Media segments are cached beside the file so Skip Intro/Credits works
    over a downloaded item (there was no offline detection at all before).

    Best-effort like the trickplay tiles beside it: segments come from a
    plugin, so most items legitimately have none and a server without it
    answers nothing. A failure must not fail the download.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.m = SyncManager.__new__(SyncManager)

    def _client(self, answer):
        class Api:
            def get_media_segments(self, _src_id):
                if isinstance(answer, Exception):
                    raise answer
                return answer

        return type("C", (), {"jellyfin": Api()})()

    def _run(self, answer):
        self.m._download_segments(self._client(answer), {"Id": "src"},
                                  self.tmp.name)
        path = os.path.join(self.tmp.name, "segments.json")
        if not os.path.exists(path):
            return None
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def test_segments_are_written_beside_the_media(self):
        seg = {"Type": "Intro", "StartTicks": 0, "EndTicks": 10}
        self.assertEqual(self._run({"Items": [seg]}), [seg])

    def test_every_type_is_kept(self):
        """The settings filter at playback (conf.segment_action), because
        what is on disk outlives the settings that wrote it."""
        segs = [{"Type": t, "StartTicks": 0, "EndTicks": 10}
                for t in ("Intro", "Recap", "Commercial")]
        self.assertEqual([s["Type"] for s in self._run({"Items": segs})],
                         ["Intro", "Recap", "Commercial"])

    def test_no_segments_writes_no_file(self):
        self.assertIsNone(self._run({"Items": []}))

    def test_a_server_without_the_plugin_is_not_an_error(self):
        self.assertIsNone(self._run(RuntimeError("404")))


class QueueHeadOfLineTest(TmpTest):
    """The worker must make progress on every row it *can* run.

    The pending queue is drained in enqueue order so a not-yet-resolvable item
    cannot float to the front on catalog sort (`db.list`'s own comment says
    so). That ordering guarantee is worth nothing if the worker then takes the
    head unconditionally: one row for a server that is gone parks itself there
    and everything behind it waits forever, because nothing retires such a row
    — it is left pending *on purpose* so it resumes when the server returns,
    and removing a server does not purge its catalog rows.
    """

    def _worker(self, m, until, timeout=5.0):
        """Run the real worker loop until `until()` or a timeout, then stop."""
        m._stop = False
        t = threading.Thread(target=m._run, daemon=True)
        t.start()
        deadline = time.monotonic() + timeout
        while not until() and time.monotonic() < deadline:
            time.sleep(0.01)
        m._stop = True
        m._wake.set()
        t.join(2)
        self.assertFalse(t.is_alive(), "worker did not stop")

    def test_an_unreachable_server_does_not_block_what_is_behind_it(self):
        m = make_manager(self.tmp, self.addCleanup, clients={"here": FakeClient()})
        add_row(m, "blocked", server_uuid="gone")     # added first: the head
        add_row(m, "b", server_uuid="here")
        add_row(m, "c", server_uuid="here")
        done = []

        def fake_download(row, stopping=None):
            done.append(row["item_id"])
            m.db.update(row["item_id"], status=STATUS_COMPLETE)

        m._download = fake_download
        self._worker(m, lambda: len(done) >= 2)
        self.assertEqual(sorted(done), ["b", "c"])
        # The unrunnable row is skipped, not retried in a spin and not retired.
        self.assertNotIn("blocked", done)
        self.assertEqual(m.db.get("blocked")["status"], STATUS_PENDING)

    def test_auto_download_still_runs_while_the_queue_holds_only_dead_work(self):
        """The damaging half. The auto pass is gated on there being no
        download in flight — a pending row for a server we cannot reach is not
        one, and treating it as one switched the reaper off (retention, the
        cap, failed-row reclaim) for the life of the process."""
        m = make_manager(self.tmp, self.addCleanup, clients={})
        add_row(m, "blocked", server_uuid="gone")
        ticks = []

        class FakeAuto:
            def tick(self):
                ticks.append(1)

        m.auto = FakeAuto()
        m._download = lambda row, stopping=None: self.fail("nothing is runnable")
        self._worker(m, lambda: len(ticks) >= 1)
        self.assertTrue(ticks, "the auto pass never ran")

    def test_a_runnable_row_still_defers_the_auto_pass(self):
        """The gate's actual purpose survives: no auto pass while there is
        real work, so the scheduler never competes with the user's download."""
        m = make_manager(self.tmp, self.addCleanup, clients={"here": FakeClient()})
        add_row(m, "b", server_uuid="here")
        ticks, done = [], []

        class FakeAuto:
            def tick(self):
                ticks.append(1)

        def fake_download(row, stopping=None):
            self.assertEqual(ticks, [], "auto ran with a download queued")
            done.append(row["item_id"])
            m.db.update(row["item_id"], status=STATUS_COMPLETE)

        m.auto = FakeAuto()
        m._download = fake_download
        self._worker(m, lambda: len(done) >= 1)
        self.assertEqual(done, ["b"])

    def test_next_runnable_skips_to_the_first_resolvable_row(self):
        m = make_manager(self.tmp, self.addCleanup, clients={"here": FakeClient()})
        add_row(m, "x", server_uuid="gone")
        add_row(m, "y", server_uuid="gone")
        add_row(m, "z", server_uuid="here")
        self.assertEqual(m._next_runnable()["item_id"], "z")
        m.get_client = lambda uuid: None
        self.assertIsNone(m._next_runnable())


def _http_error(status):
    err = manager_module.requests.HTTPError("HTTP %d" % status)
    err.response = FakeResp(status=status)
    return err


class PermanentFailureTest(TmpTest):
    """A failure the code has judged permanent has to outlive the row.

    The reaper deletes auto rows in ERROR to reclaim their .part bytes, one
    call before the planner runs in the same pass — so the row cannot be what
    remembers the attempt. Without a tombstone the item is re-enqueued
    immediately (still unwatched, still Next Up), re-downloaded, re-failed,
    every pass, forever.
    """

    def _fail_with(self, exc, origin=ORIGIN_AUTO_NEXT_UP):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100, origin=origin)

        def boom(*a, **k):
            raise exc

        m._stream = boom
        return m

    def test_a_4xx_is_remembered(self):
        m = self._fail_with(_http_error(404))
        m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_ERROR)
        self.assertEqual(m.db.discarded_ids(), {"a"},
                         "the scheduler will fetch this again next pass")

    def test_a_5xx_is_not(self):
        """Transient by construction — the row stays pending to resume, and a
        tombstone would take an item out of auto-download for a server that
        was merely busy."""
        m = self._fail_with(_http_error(503))
        with self.assertRaises(manager_module.requests.HTTPError):
            m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_PENDING)
        self.assertEqual(m.db.discarded_ids(), set())

    def test_an_unexpected_exception_is_not(self):
        """Disk full, a permissions problem, a bug in us: not the item's
        fault, gone as soon as the environment is fixed. Blacklisting every
        episode that met a full disk would quietly gut auto-download."""
        m = self._fail_with(OSError("No space left on device"))
        m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_ERROR)
        self.assertEqual(m.db.discarded_ids(), set())

    def test_a_users_own_download_is_never_tombstoned(self):
        """The discard list records the scheduler's decisions. A download the
        user asked for is theirs to see failed and retry."""
        m = self._fail_with(_http_error(404), origin=ORIGIN_USER)
        m._download(m.db.get("a"))
        self.assertEqual(m.db.discarded_ids(), set())

    def test_a_repeatedly_truncated_download_is_remembered(self):
        """The other branch that gives up: a server that keeps ending the
        response at the same offset. Three stalls and it is marked failed —
        which the reaper then deletes."""
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100, origin=ORIGIN_AUTO_NEXT_UP)
        m._stream = lambda *a, **k: (50, 100)
        for _attempt in range(4):
            m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_ERROR)
        self.assertEqual(m.db.discarded_ids(), {"a"})


class BookDownloadTest(TmpTest):
    """A Book is the one download target with no media source at all.

    No MediaSources, no Container, no size under any Fields value — measured
    against a live server, not assumed. Everything the pipeline normally
    reads off a source has to come from somewhere else or be skipped, and
    the extension in particular is load-bearing: for a video it is cosmetic,
    for a book it is what tells the desktop which application opens the file.
    """

    def _book(self, path="/library/A Novel.epub", **extra):
        return {"Id": "b", "Type": "Book", "Name": "A Novel", "Path": path,
                **extra}

    # -- the extension -----------------------------------------------------

    def test_a_book_takes_its_extension_from_its_path(self):
        m = make_manager(self.tmp, self.addCleanup)
        self.assertEqual(m._ext_for(self._book()), "epub")

    def test_a_container_still_wins_where_there_is_one(self):
        m = make_manager(self.tmp, self.addCleanup)
        self.assertEqual(
            m._ext_for({"Type": "Movie",
                        "MediaSources": [{"Container": "mkv,webm"}]}), "mkv")

    def test_a_book_with_no_path_is_not_called_a_video(self):
        # "bin" rather than the mkv default: an unopenable file named
        # honestly beats one claiming to be something it is not, and
        # _download corrects it from Content-Disposition anyway.
        m = make_manager(self.tmp, self.addCleanup)
        self.assertEqual(m._ext_for({"Type": "Book"}), "bin")

    def test_the_row_is_written_with_the_book_extension(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._add_row("uuid", "srv", self._book(path="/l/A Comic.cbz"))
        self.assertEqual(m.db.get("b")["ext"], "cbz")

    # -- what the pipeline must NOT do -------------------------------------

    def test_a_book_download_asks_for_no_media_source(self):
        """PlaybackInfo, subtitles, trickplay and media segments are all
        properties of a media source. A Book is not IHasMediaSources, so
        each of these is a request that can only come back empty — and the
        first one logs a server-side error on the way."""
        m = make_manager(self.tmp, self.addCleanup)
        asked = []
        for name in ("_playback_source", "_download_subs",
                     "_download_trickplay", "_download_segments"):
            setattr(m, name, lambda *a, _n=name, **k: asked.append(_n))
        m._add_row("uuid", "srv", self._book())
        m._stream = _fake_stream(b"x" * 10)
        m._download(m.db.get("b"))
        self.assertEqual(asked, [])
        self.assertEqual(m.db.get("b")["status"], STATUS_COMPLETE)

    def test_artwork_is_still_fetched(self):
        # The cover is the one thing a book DOES have, and it is what the
        # offline browser draws.
        m = make_manager(self.tmp, self.addCleanup)
        art = []
        m._download_artwork = lambda *a, **k: art.append(1)
        m._add_row("uuid", "srv", self._book())
        m._stream = _fake_stream(b"x" * 10)
        m._download(m.db.get("b"))
        self.assertEqual(len(art), 1)

    # -- the served filename -----------------------------------------------

    def test_the_served_filename_corrects_a_wrong_extension(self):
        """`Path` is metadata; `Content-Disposition` is the file.

        They normally agree, and when they do not it is the response that is
        right — a server serving a converted or renamed copy would otherwise
        have us write an epub as .pdf, which every reader refuses with a
        corruption error.
        """
        m = make_manager(self.tmp, self.addCleanup)
        m._add_row("uuid", "srv", self._book(path="/l/A Novel.pdf"))
        m._stream = _fake_stream(
            b"x" * 10,
            served={"Content-Disposition": 'attachment; filename="A.epub"'})
        m._download(m.db.get("b"))
        row = m.db.get("b")
        self.assertEqual(row["ext"], "epub")
        self.assertTrue(row["file_path"].endswith("media.epub"))
        self.assertTrue(os.path.exists(os.path.join(m.root, row["file_path"])))
        # And nothing is left behind under the name the metadata claimed.
        self.assertFalse(os.path.exists(
            os.path.join(m._item_dir(row), "media.pdf")))

    def test_a_disposition_that_agrees_changes_nothing(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._add_row("uuid", "srv", self._book())
        m._stream = _fake_stream(
            b"x" * 10,
            served={"Content-Disposition":
                    'attachment; filename="A Novel.epub"'})
        m._download(m.db.get("b"))
        self.assertTrue(m.db.get("b")["file_path"].endswith("media.epub"))

    def test_a_response_with_no_disposition_keeps_the_path_extension(self):
        m = make_manager(self.tmp, self.addCleanup)
        m._add_row("uuid", "srv", self._book())
        m._stream = _fake_stream(b"x" * 10)
        m._download(m.db.get("b"))
        self.assertTrue(m.db.get("b")["file_path"].endswith("media.epub"))

    def test_the_size_is_learned_from_the_response(self):
        # There is no size on the wire for a book, so the row starts at 0
        # and the only source of truth is what actually arrived.
        m = make_manager(self.tmp, self.addCleanup)
        m._add_row("uuid", "srv", self._book())
        self.assertEqual(m.db.get("b")["size_bytes"], 0)
        m._stream = _fake_stream(b"x" * 1234)
        m._download(m.db.get("b"))
        self.assertEqual(m.db.get("b")["size_bytes"], 1234)


class DispositionTest(unittest.TestCase):
    """The header is server-controlled input, so it is parsed rather than
    split on, and only its extension is ever used."""

    def _ext(self, raw):
        return manager_module._disposition_ext(
            {"Content-Disposition": raw} if raw is not None else {})

    def test_a_plain_filename(self):
        self.assertEqual(self._ext('attachment; filename="A Novel.epub"'),
                         "epub")

    def test_jellyfins_own_two_spellings(self):
        # It sends both; the ASCII one is enough, because an extension is
        # ASCII in every format that exists.
        self.assertEqual(
            self._ext('attachment; filename="Adrift.epub"; '
                      "filename*=UTF-8''Adrift.epub"), "epub")

    def test_a_semicolon_inside_a_quoted_name(self):
        # Why this is parsed with the stdlib's message machinery rather than
        # split on ";".
        self.assertEqual(self._ext('attachment; filename="a; b.pdf"'), "pdf")

    def test_a_traversal_attempt_yields_only_an_extension(self):
        # Never a path. The answer is used to build a filename, so a header
        # must not be able to steer where anything is written.
        self.assertEqual(self._ext('attachment; filename="../../evil.epub"'),
                         "epub")

    def test_a_name_with_no_extension(self):
        self.assertIsNone(self._ext('attachment; filename="A Novel"'))

    def test_a_non_alphanumeric_suffix_is_not_an_extension(self):
        self.assertIsNone(self._ext('attachment; filename="x.e/p"'))

    def test_no_header_at_all(self):
        self.assertIsNone(self._ext(None))
        self.assertIsNone(self._ext(""))


class ExpandFolderTest(TmpTest):
    """A multi-file audiobook is a FOLDER and nothing else joins it.

    `SeriesName` is null on audiobooks and `Album` is tag-derived, so an
    untagged rip has no metadata linking its files at all. The folder is
    therefore the download unit, and it is the only container this manager
    expands by listing rather than through an endpoint of its own.
    """

    class Api:
        def __init__(self, items):
            # `_items`: `items` is the method now (GET /Items -- see
            # jellyfin_mpv_shim/items_api), so an attribute of that name
            # would shadow it.
            self._items = items
            self.calls = []

        def items(self, handler="", action="GET", params=None, **_kw):
            self.calls.append(dict(params or {}))
            return {"Items": list(self._items)}

    def test_a_folder_expands_to_its_children(self):
        m = make_manager(self.tmp, self.addCleanup)
        api = self.Api([{"Id": "1", "Type": "AudioBook"},
                        {"Id": "2", "Type": "AudioBook"}])
        self.assertEqual([i["Id"] for i in m._expand(api, "f", "Folder")],
                         ["1", "2"])

    def test_it_does_not_recurse(self):
        """"Download this folder" means this folder. An author directory
        holding forty books must not quietly become forty downloads."""
        m = make_manager(self.tmp, self.addCleanup)
        api = self.Api([])
        m._expand(api, "f", "Folder")
        self.assertNotIn("Recursive", api.calls[0])

    def test_it_asks_for_path(self):
        # The only statement of a Book's format, and the row is written
        # from this response.
        m = make_manager(self.tmp, self.addCleanup)
        api = self.Api([])
        m._expand(api, "f", "Folder")
        self.assertIn("Path", api.calls[0]["Fields"])

    def test_unsupported_children_are_dropped(self):
        m = make_manager(self.tmp, self.addCleanup)
        api = self.Api([{"Id": "1", "Type": "AudioBook"},
                        {"Id": "2", "Type": "Folder"},
                        {"Id": "3", "Type": "Book"},
                        {"Id": "4", "Type": "MusicArtist"}])
        self.assertEqual([i["Id"] for i in m._expand(api, "f", "Folder")],
                         ["1", "3"])


class BookEstimateTest(TmpTest):

    class Api:
        def __init__(self, items):
            self._items = items      # see the note on the Api above

        def items(self, handler="", action="GET", params=None, **_kw):
            return {"Items": list(self._items)}

    def _estimate(self, items):
        m = make_manager(self.tmp, self.addCleanup)
        api = self.Api(items)
        client = FakeClient()
        client.jellyfin = api
        m.get_client = lambda uuid: client
        return m.estimate("uuid", "f", "Folder")

    def test_books_are_counted_as_unsized(self):
        """A size that silently undercounts is worse than one that admits
        what it left out — and for a books-only download the whole figure
        would otherwise read as 0 B."""
        est = self._estimate([
            {"Id": "1", "Type": "Book"},
            {"Id": "2", "Type": "AudioBook", "MediaSources": [{"Size": 500}]},
        ])
        self.assertEqual(est["count"], 2)
        self.assertEqual(est["total_bytes"], 500)
        self.assertEqual(est["unsized_count"], 1)

    def test_an_audiobook_folder_counts_as_audio_only(self):
        # Which is what makes the dialog default to including played items:
        # you do not skip a chapter you have already listened to when
        # downloading the book.
        est = self._estimate([
            {"Id": "1", "Type": "AudioBook", "MediaSources": [{"Size": 1}]},
            {"Id": "2", "Type": "AudioBook", "MediaSources": [{"Size": 1}]},
        ])
        self.assertTrue(est["audio_only"])

    def test_a_folder_of_books_is_not_audio_only(self):
        est = self._estimate([{"Id": "1", "Type": "Book"}])
        self.assertFalse(est["audio_only"])


def _fake_stream(body, served=None):
    """A ``_stream`` that writes ``body`` and reports ``served`` as the
    response headers.

    Takes on_headers, like the real one. A fake that did not would make the
    download a TypeError and the row land in 'error' — which is exactly how
    this signature stays honest.

    ``served`` rather than ``headers``: the real signature already has a
    ``headers`` parameter (the REQUEST's, carrying the Authorization) and
    naming both the same shadowed the closure, so every disposition test
    handed the auth headers to on_headers and quietly asserted nothing.
    """
    def fake(url, dest, item_id, name, expected, stopping=None,
             headers=None, on_headers=None):
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest + ".part", "wb") as fh:
            fh.write(body)
        if on_headers is not None:
            on_headers(served or {})
        return len(body), len(body)
    return fake


def _corrupt_downloads_table(catalog_path):
    """Zero the `downloads` b-tree root page (page 2).

    Not a scattergun: measured, this one 4 KiB page of a 64 KiB catalog is
    enough. The file still opens, `sqlite_master` still reads, the schema
    still migrates -- and every `SELECT * FROM downloads` raises
    `database disk image is malformed`, which `_query` turns into `[]`.
    """
    with open(catalog_path, "rb") as fh:
        raw = bytearray(fh.read())
    page = int.from_bytes(raw[16:18], "big") or 65536
    raw[page:page * 2] = b"\x00" * page
    with open(catalog_path, "wb") as fh:
        fh.write(bytes(raw))


class CorruptCatalogTest(TmpTest):
    """A catalog that cannot be read must not be able to authorise a delete.

    Every read answers `[]` on a sqlite error so callers cannot crash on a bad
    catalog, which makes "unreadable" and "empty" the same value everywhere
    except here, where empty means "none of this media is known".
    """

    def _launch(self, root):
        m = SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        m._open_and_run()
        self.addCleanup(m.stop)
        return m

    def _seed(self, m, root, n):
        for i in range(n):
            iid = "%032x" % i
            item_dir = os.path.join(root, "srv", iid)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
                fh.write(b"x" * 64)
            with open(os.path.join(item_dir, "item.json"), "w") as fh:
                json.dump({"Id": iid, "Type": "Movie", "Name": "Film %d" % i}, fh)
            add_row(m, iid, status=STATUS_COMPLETE,
                    file_path="srv/%s/media.mkv" % iid)

    def test_a_corrupt_catalog_deletes_nothing(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        m = self._launch(root)
        self._seed(m, root, 3)
        m.stop()
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))
        # No backup on disk yet, so there is nothing to restore either --
        # the only correct answer left is to touch nothing.
        os.remove(os.path.join(root, SyncManager.CATALOG_BACKUP))
        self._launch(root)
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3,
                         "an unreadable catalog was read as an empty one and "
                         "the orphan sweep deleted every download")

    def test_the_backup_is_restored_and_the_corrupt_file_kept(self):
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        m = self._launch(root)
        self._seed(m, root, 3)
        m.stop()
        m = self._launch(root)      # the backup now holds all three rows
        m.stop()
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))

        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 3)
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3)
        # Kept, not overwritten: it is still the better copy of anything the
        # backup predates, and `.recover` can read it right up until we drop it.
        self.assertTrue([n for n in os.listdir(root)
                         if n.startswith("catalog.db.corrupt-")])

    def test_a_restore_is_not_a_delayed_wipe(self):
        """Anything downloaded after the snapshot has files and no row, which
        is the orphan shape. The launch after the restore is where a naive fix
        deletes it instead."""
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        m = self._launch(root)
        self._seed(m, root, 2)
        m.stop()
        m = self._launch(root)      # backup holds items 0 and 1
        m.stop()
        m = self._launch(root)
        self._seed(m, root, 3)      # item 2 exists only after the backup
        m.stop()
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))

        m = self._launch(root)
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3)
        m.stop()
        m = self._launch(root)      # the launch that used to sweep item 2
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3)
        rows = {r["item_id"]: r for r in m.db.list()}
        self.assertEqual(len(rows), 3)
        # The two the backup carried, plus the one rebuilt from its own
        # item.json — which is the half that names itself.
        self.assertEqual(rows["%032x" % 2]["name"], "Film 2")
        self.assertEqual(rows["%032x" % 2]["status"], STATUS_COMPLETE)

    def test_a_part_file_survives_the_restore_launch(self):
        """What suppressing the sweep on a restore actually buys.

        A *complete* post-snapshot download is rescued by `_adopt_orphan`
        whether or not the sweep ran, so the test above passes with
        `sweep_orphans` forced True and does not pin it. An interrupted one
        cannot be adopted -- there is no media to promote -- and it is the
        thing a restored, known-stale catalog must not be allowed to delete.
        """
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        m = self._launch(root)
        self._seed(m, root, 1)
        m.stop()
        self._launch(root).stop()          # backup holds item 0
        # An item queued after the snapshot, interrupted mid-download.
        iid = "%032x" % 7
        item_dir = os.path.join(root, "srv", iid)
        os.makedirs(item_dir)
        with open(os.path.join(item_dir, "item.json"), "w") as fh:
            json.dump({"Id": iid, "Type": "Movie", "Name": "Half"}, fh)
        with open(os.path.join(item_dir, "media.mkv.part"), "wb") as fh:
            fh.write(b"x" * 16)
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))

        self._launch(root).stop()
        self.assertTrue(os.path.exists(item_dir),
                        "a part-downloaded item was swept on the strength of "
                        "a catalog that had just been restored from an older "
                        "snapshot and is known not to describe the disk")


class RelocateRefusesANonEmptyFolderTest(TmpTest):
    """The store owns its root: `_move_tree` empties it and the orphan sweep
    deletes item-shaped directories inside it. Both act on data this app never
    wrote if the folder is shared, and the damage surfaces launches later."""

    def _manager(self):
        root = os.path.join(self.tmp, "old")
        os.makedirs(root)
        m = make_manager(root, self.addCleanup)
        m.db.close()
        return m

    def test_a_folder_with_the_users_files_is_refused(self):
        m = self._manager()
        dest = os.path.join(self.tmp, "Videos")
        os.makedirs(os.path.join(dest, "Holidays"))
        ok, message = m.relocate(dest)
        self.assertFalse(ok)
        self.assertTrue(message)
        self.assertTrue(os.path.exists(os.path.join(dest, "Holidays")))

    def test_a_folder_holding_downloads_says_so_specifically(self):
        m = self._manager()
        dest = os.path.join(self.tmp, "other")
        os.makedirs(dest)
        with open(os.path.join(dest, "catalog.db"), "wb") as fh:
            fh.write(b"")
        ok, message = m.relocate(dest)
        self.assertFalse(ok)
        self.assertIn("already contains downloads", message)

    def test_an_empty_folder_is_still_accepted(self):
        m = self._manager()
        dest = os.path.join(self.tmp, "empty")
        os.makedirs(dest)
        ok, message = m.relocate(dest)
        self.addCleanup(m.stop)
        self.assertTrue(ok, message)


class DeleteDuringAnUnwindTest(TmpTest):
    """A delete of the in-flight download is honoured however `_download`
    leaves — every handler ends by writing the row back, and each of them used
    to run over the top of a delete the user had already been told succeeded."""

    def _run_with(self, raise_in_stream):
        m = self.manager = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)

        def fake_stream(url, dest, item_id, name, expected, stopping=None,
                        headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 10)
            # The user deletes it mid-transfer...
            self.assertTrue(m.delete(item_id="a"))
            # ...and then the download ends some other way.
            raise raise_in_stream

        m._stream = fake_stream
        m._download(m.db.get("a"))
        return m

    def test_shutdown_does_not_resurrect_it(self):
        # `stopping()` is tested before the cancel flag in the chunk loop, so
        # quitting during the delete took this path every time.
        m = self._run_with(manager_module._Stopped())
        self.assertIsNone(m.db.get("a"))
        self.assertIsNone(m._next_runnable(),
                          "a deleted download was left queued to resume")

    def test_a_dropped_connection_does_not_resurrect_it(self):
        # This handler re-raises so `_run`'s backoff throttles the retry, so
        # the row has to be checked from outside the raise rather than from
        # the return value.
        holder = {}
        with self.assertRaises(manager_module.requests.RequestException):
            holder["m"] = self._run_with(
                manager_module.requests.RequestException("reset"))
        self.assertIsNone(self.manager.db.get("a"))

    def test_an_unexpected_error_does_not_resurrect_it(self):
        m = self._run_with(RuntimeError("boom"))
        self.assertIsNone(m.db.get("a"))

    def test_the_files_go_too(self):
        m = self._run_with(manager_module._Stopped())
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "srv", "a")))


class PlaylistOwnershipTest(TmpTest):
    """Two things delete a download the user did not ask to delete: the reaper
    and a playlist that owns the item. `enqueue` answered only the first."""

    class _API:
        def __init__(self, items):
            self._items = items

        def get_playlist_items(self, pid, fields=None):
            return {"Items": self._items}

        def get_item(self, iid, fields=None):
            for i in self._items:
                if i.get("Id") == iid:
                    return i
            return {"Name": "My Playlist"}

    def _manager(self, items):
        m = make_manager(self.tmp, self.addCleanup)
        api = self._API(items)

        class C:
            config = type("cfg", (), {"data": {"auth.server-id": "srv"}})()
            jellyfin = api
        m.get_client = lambda uuid: C()
        m._download_playlist_art = lambda *a, **k: None
        return m

    ITEM = {"Id": "X", "Type": "Movie", "Name": "Film",
            "MediaSources": [{"Id": "s", "Container": "mkv"}]}

    def test_an_explicit_download_survives_deleting_the_playlist(self):
        m = self._manager([self.ITEM])
        m.enqueue("u", "P", "Playlist", include_watched=True)
        m.db.update("X", status=STATUS_COMPLETE)
        # The user then browses to the film itself and presses Download. The
        # menu offers it whether or not the item is already downloaded.
        m.enqueue("u", "X", "Movie", include_watched=True)
        self.assertEqual(m.db.playlist_owned_ids("P"), set())
        m.delete(playlist_id="P")
        self.assertIsNotNone(m.db.get("X"),
                             "a film the user downloaded by hand was deleted "
                             "with the playlist that first pulled it in")

    def test_it_still_lists_under_the_playlist(self):
        """Disowned, not unlinked: ownership only answers "may deleting the
        playlist delete this", and the item still belongs to the playlist."""
        m = self._manager([self.ITEM])
        m.enqueue("u", "P", "Playlist", include_watched=True)
        m.db.update("X", status=STATUS_COMPLETE)
        m.enqueue("u", "X", "Movie", include_watched=True)
        self.assertEqual([r["item_id"] for r in m.db.playlist_item_rows("P")],
                         ["X"])

    def test_a_playlist_download_still_owns_what_it_pulls_in(self):
        m = self._manager([self.ITEM])
        m.enqueue("u", "P", "Playlist", include_watched=True)
        self.assertEqual(m.db.playlist_owned_ids("P"), {"X"})

    def test_re_downloading_the_playlist_does_not_reclaim_it(self):
        m = self._manager([self.ITEM])
        m.enqueue("u", "P", "Playlist", include_watched=True)
        m.db.update("X", status=STATUS_COMPLETE)
        m.enqueue("u", "X", "Movie", include_watched=True)
        m.enqueue("u", "P", "Playlist", include_watched=True)
        self.assertEqual(m.db.playlist_owned_ids("P"), set(),
                         "the playlist took back an item the user had claimed")

    # The two below are the guard on the *other* side of this rule. A release
    # placed where a playlist download reaches it disowns the playlist's own
    # members: `_record_playlist` recomputes ownership from `pre_existing`,
    # snapshotted at the top of the enqueue, so a member that already had a
    # row reads as "was here before, and nobody owns it" the moment its claim
    # is dropped first. Both were green before the claim-release moved and
    # must stay green after it.

    def test_re_downloading_a_playlist_keeps_a_member_it_holds(self):
        m = self._manager([self.ITEM])
        m.enqueue("u", "P", "Playlist", include_watched=True)
        m.db.update("X", status=STATUS_COMPLETE)
        self.assertEqual(m.db.playlist_owned_ids("P"), {"X"})
        m.enqueue("u", "P", "Playlist", include_watched=True)
        self.assertEqual(m.db.playlist_owned_ids("P"), {"X"},
                         "re-downloading the playlist disowned the copy it "
                         "had pulled in itself")

    def test_re_downloading_a_playlist_keeps_a_member_still_in_progress(self):
        """The same, through the `_add_row` arm: the member has a row but is
        not complete, so this enqueue queues it again."""
        m = self._manager([self.ITEM])
        m.enqueue("u", "P", "Playlist", include_watched=True)
        self.assertEqual(m.db.playlist_owned_ids("P"), {"X"})
        m.enqueue("u", "P", "Playlist", include_watched=True)
        self.assertEqual(m.db.playlist_owned_ids("P"), {"X"},
                         "re-downloading the playlist disowned a member it "
                         "was still fetching")


class OwnershipNeverOutlivesItsRowTest(TmpTest):
    """`owned=1` says "deleting this playlist may delete this file". A row
    saying that about an item the catalog does not have is a claim on nothing
    -- until something puts a row back, and then it is a claim on a file
    nobody meant to give the playlist.

    `_adopt_orphan` is what puts the row back, and it sets `ORIGIN_USER`
    *specifically* so the reaper cannot delete a download it had no evidence
    was ever scheduled. Leaving the playlist's claim standing hands the same
    file to the other deleter, so the protection is defeated by the sequence
    it was written for.
    """

    XID = "%032x" % 1
    YID = "%032x" % 2
    ITEM = {"Id": XID, "Type": "Movie", "Name": "Film",
            "MediaSources": [{"Id": "s", "Container": "mkv"}]}

    class _API:
        def __init__(self, items):
            self._items = items

        def get_playlist_items(self, pid, fields=None):
            return {"Items": self._items}

        def get_item(self, iid, fields=None):
            # By id first: `_expand` of a single item goes through here, so a
            # stand-in that only ever answers the playlist's name makes an
            # enqueue of a member unreachable (docs/testing.md section 4).
            for i in self._items:
                if i.get("Id") == iid:
                    return i
            return {"Name": "My Playlist"}

    def _manager(self, root):
        os.makedirs(root, exist_ok=True)
        m = make_manager(root, self.addCleanup)
        api = self._API([self.ITEM])

        class C:
            config = type("cfg", (), {"data": {"auth.server-id": "srv"}})()
            jellyfin = api
        m.get_client = lambda uuid: C()
        m._download_playlist_art = lambda *a, **k: None
        return m

    def _on_disk(self, root, iid, describe=True):
        """A download as `_download` leaves it: media plus the manifests that
        let `_adopt_orphan` rebuild the row."""
        item_dir = os.path.join(root, "srv", iid)
        os.makedirs(item_dir, exist_ok=True)
        with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
            fh.write(b"x" * 64)
        if describe:
            with open(os.path.join(item_dir, "item.json"), "w") as fh:
                json.dump({"Id": iid, "Type": "Movie", "Name": iid}, fh)
            with open(os.path.join(item_dir, "source.json"), "w") as fh:
                json.dump({"Id": "s"}, fh)
        return item_dir

    def test_a_delete_racing_the_membership_write_leaves_no_claim(self):
        """Nothing serialises `enqueue` against `delete`: neither takes a
        manager-wide lock, and the browser runs them on a four-worker pool.
        So the delete can land between the row this enqueue committed and the
        membership it is about to write, and `pre_existing` -- snapshotted
        before the loop -- still says the playlist pulled this item in.
        """
        root = os.path.join(self.tmp, "raced")
        m = self._manager(root)

        real_record = m._record_playlist

        def racing_record(*a, **kw):
            m.db.update(self.XID, status=STATUS_COMPLETE)
            m.delete_item(self.XID)          # the other worker's delete
            return real_record(*a, **kw)

        m._record_playlist = racing_record
        m.enqueue("u", "P", "Playlist", include_watched=True)

        self.assertIsNone(m.db.get(self.XID), "the racing delete did not land")
        self.assertEqual(
            m.db.playlist_owned_ids("P"), set(),
            "the playlist owns an item the catalog does not have, so the "
            "next thing to write that row hands it the file")

    def test_adopting_an_orphan_releases_a_stale_playlist_claim(self):
        """Written straight into the tables, because a catalog that reached
        this state under an older build still has it -- the guard above stops
        it being made, not from already being there."""
        root = os.path.join(self.tmp, "stale-claim")
        m = self._manager(root)
        self._stale_claim(m, self.XID)

        self._on_disk(root, self.XID)
        self._on_disk(root, self.YID, describe=False)
        add_row(m, self.YID, status=STATUS_COMPLETE,
                file_path="srv/%s/media.mkv" % self.YID)
        m._reconcile_disk()

        row = m.db.get(self.XID)
        self.assertIsNotNone(row, "the orphan was not adopted at all")
        self.assertEqual(
            self._claims_in_the_table(m), set(),
            "the row _adopt_orphan marked never-auto to protect the file is "
            "still owned by a playlist, which deletes it unconditionally")

    def test_the_protected_file_survives_deleting_that_playlist(self):
        """The end of the chain, which is the only part the user sees."""
        root = os.path.join(self.tmp, "end-to-end")
        m = self._manager(root)
        self._stale_claim(m, self.XID)
        media = os.path.join(self._on_disk(root, self.XID), "media.mkv")
        self._on_disk(root, self.YID, describe=False)
        add_row(m, self.YID, status=STATUS_COMPLETE,
                file_path="srv/%s/media.mkv" % self.YID)
        m._reconcile_disk()

        m._delete_playlist("P")
        self.assertTrue(os.path.exists(media),
                        "deleting the playlist deleted the very file "
                        "_adopt_orphan had gone out of its way to keep")


    def _stale_claim(self, m, item_id):
        """A `playlist_items` row saying `owned=1` over an item the catalog
        does not have. Written straight into the tables, because that is how a
        catalog reaches this state: an older build, or the race the class
        docstring names, and the guard inside `replace_playlist_items` stops
        it being *made*, not from already being there."""
        m.db.upsert_playlist("P", "srv", "u", "My Playlist")
        m.db._conn.execute(
            "INSERT INTO playlist_items (playlist_id, item_id, sort_index, "
            "owned) VALUES (?,?,?,1)", ("P", item_id, 0))
        m.db._conn.commit()
        self.assertEqual(self._claims_in_the_table(m), {item_id},
                         "the stale claim was not seeded")

    @staticmethod
    def _claims_in_the_table(m):
        """`owned=1` rows as the *table* holds them.

        `playlist_owned_ids` requires the download row, so it cannot see the
        state these tests seed -- which is the point of it, and is why the
        premise has to be read here instead. Anything that wants to find
        dangling claims rather than act on them needs its own read like this
        one."""
        return {r[0] for r in m.db._conn.execute(
            "SELECT item_id FROM playlist_items WHERE owned=1")}

    def test_a_scheduled_download_starts_unclaimed(self):
        """The claim is released by whoever *creates the row*, not by whoever
        asked for it.

        `_add_row` runs for every origin, so a scheduled auto-download of an
        item a stale claim still stands over hands the playlist a file nobody
        gave it. It is bounded -- the reaper may take an auto row back anyway,
        so what is lost is a scheduled download rather than one the user chose
        -- but it is the same claim on the same file, reached by the origin
        nobody released it for.
        """
        root = os.path.join(self.tmp, "auto-origin")
        m = self._manager(root)
        self._stale_claim(m, self.XID)

        m.enqueue("u", self.XID, "Movie", include_watched=True,
                  origin=ORIGIN_AUTO_NEXT_UP)

        self.assertIsNotNone(m.db.get(self.XID),
                             "the enqueue wrote no row, so this test proves "
                             "nothing about the row it writes")
        self.assertEqual(
            m.db.playlist_owned_ids("P"), set(),
            "the row this enqueue created is already owned by a playlist "
            "that never downloaded it")

    def test_the_scheduled_downloads_file_survives_deleting_that_playlist(self):
        """The end of that chain, which is the only part the user sees."""
        root = os.path.join(self.tmp, "auto-origin-end-to-end")
        m = self._manager(root)
        self._stale_claim(m, self.XID)

        m.enqueue("u", self.XID, "Movie", include_watched=True,
                  origin=ORIGIN_AUTO_NEXT_UP)
        media = os.path.join(self._on_disk(root, self.XID), "media.mkv")
        m.db.update(self.XID, status=STATUS_COMPLETE,
                    file_path="srv/%s/media.mkv" % self.XID)

        m._delete_playlist("P")
        self.assertTrue(
            os.path.exists(media),
            "deleting the playlist deleted a download it never pulled in")

    def test_adopting_an_orphan_that_calls_itself_a_playlist_releases_it(self):
        """`_adopt_orphan` passed the *manifest's* type to a guard whose
        proposition is about the *request's* type ("this call is the playlist
        download, and `_record_playlist` recomputes ownership below").

        No manifest the downloader writes says `Playlist` -- an expansion
        returns only downloadable members -- but `_reconcile_disk` reads a
        file some other build may have written, and its own docstring says so.
        One that does say it buys the file the exact claim this call exists to
        release.
        """
        root = os.path.join(self.tmp, "playlist-manifest")
        m = self._manager(root)
        self._stale_claim(m, self.XID)

        item_dir = os.path.join(root, "srv", self.XID)
        os.makedirs(item_dir, exist_ok=True)
        with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
            fh.write(b"x" * 64)
        with open(os.path.join(item_dir, "item.json"), "w",
                  encoding="utf-8") as fh:
            json.dump({"Id": self.XID, "Type": "Playlist", "Name": "odd"}, fh)
        # A live row for the same server, so the sweep runs at all.
        self._on_disk(root, self.YID, describe=False)
        add_row(m, self.YID, status=STATUS_COMPLETE,
                file_path="srv/%s/media.mkv" % self.YID)

        m._reconcile_disk()

        self.assertIsNotNone(m.db.get(self.XID),
                             "the orphan was not adopted at all")
        self.assertEqual(
            self._claims_in_the_table(m), set(),
            "the row _adopt_orphan marked never-auto to keep the file is "
            "owned by a playlist, which deletes it unconditionally")


class MoveRollbackTest(TmpTest):
    """A failed move must leave the store exactly as it was.

    `relocate` tells the user "they were left in place"; deferring the delete
    of copied sources is only half of what makes that true. The other half is
    undoing what already got across — otherwise a cross-drive move that dies
    partway leaves a full second copy of every finished entry, and a retry
    skips it ("already there") so the duplicate is never reclaimed.
    """

    def _tree(self, root, names):
        for name in names:
            os.makedirs(os.path.join(root, name), exist_ok=True)
            with open(os.path.join(root, name, "media.mkv"), "wb") as fh:
                fh.write(b"x" * 32)

    def _fail_on(self, m, victim):
        """Force the EXDEV copy path, then fail on one entry."""
        real = m._copy_tree

        def copy(src, dst, state, progress):
            if os.path.basename(src) == victim:
                raise OSError(28, "No space left on device")
            return real(src, dst, state, progress)

        m._copy_tree = copy
        m._rename_fails = True
        return m

    def _move(self, m, old, new, victim):
        self._fail_on(m, victim)
        real_rename = os.rename

        def rename(a, b):
            raise OSError(18, "Invalid cross-device link")

        os.rename = rename
        try:
            with self.assertRaises(OSError):
                m._move_tree(old, new)
        finally:
            os.rename = real_rename

    def test_a_finished_copy_is_not_left_at_both_roots(self):
        old = os.path.join(self.tmp, "old")
        new = os.path.join(self.tmp, "new")
        os.makedirs(old)
        os.makedirs(new)
        self._tree(old, ["srv"])
        with open(os.path.join(old, "catalog.db"), "wb") as fh:
            fh.write(b"x")
        m = SyncManager()
        m.root = old
        self._move(m, old, new, "catalog.db")   # sorted last, so srv is across
        self.assertTrue(os.path.exists(os.path.join(old, "srv", "media.mkv"))
                        or os.path.exists(os.path.join(old, "srv")),
                        "the original was destroyed by a failed move")
        self.assertFalse(os.path.exists(os.path.join(new, "srv")),
                         "a failed move left a full second copy at the "
                         "destination that nothing will ever reclaim")

    def test_the_old_root_is_whole_afterwards(self):
        old = os.path.join(self.tmp, "old")
        new = os.path.join(self.tmp, "new")
        os.makedirs(old)
        os.makedirs(new)
        self._tree(old, ["a", "b"])
        with open(os.path.join(old, "catalog.db"), "wb") as fh:
            fh.write(b"x")
        m = SyncManager()
        m.root = old
        self._move(m, old, new, "catalog.db")
        self.assertEqual(sorted(os.listdir(old)), ["a", "b", "catalog.db"])
        self.assertEqual(os.listdir(new), [])


class WatchedAllImpliesTheFilterTest(TmpTest):
    """`watched_all` used to be a scope that unlocked the whole catalog, with
    the filter left to a second argument — so the one name in the signature
    that reads like a filter deleted everything."""

    def _seed(self, m):
        add_row(m, "seen", status=STATUS_COMPLETE)
        add_row(m, "unseen", status=STATUS_COMPLETE)
        m.db.update("seen", userdata_json=json.dumps({"Played": True}))
        m.db.update("unseen", userdata_json=json.dumps({"Played": False}))

    def test_it_keeps_the_unwatched(self):
        m = make_manager(self.tmp, self.addCleanup)
        self._seed(m)
        m.delete(watched_all=True)
        self.assertIsNone(m.db.get("seen"))
        self.assertIsNotNone(m.db.get("unseen"),
                             "an unwatched download was deleted by the "
                             "library-wide *watched* sweep")

    def test_no_argument_combination_wipes_the_catalog(self):
        m = make_manager(self.tmp, self.addCleanup)
        self._seed(m)
        for kwargs in ({}, {"watched_only": True},
                       {"watched_all": True, "watched_only": False}):
            m.delete(**kwargs)
            self.assertIsNotNone(m.db.get("unseen"), kwargs)


class TheBackupNeverDestroysItselfTest(TmpTest):
    """The backup exists to survive the launch that finds the catalog broken.
    Every one of these is a way that launch used to delete it instead."""

    def _launch(self, root):
        m = SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        m._open_and_run()
        self.addCleanup(m.stop)
        return m

    def _seeded(self, root, n=3):
        os.makedirs(root, exist_ok=True)
        m = self._launch(root)
        for i in range(n):
            iid = "%032x" % i
            item_dir = os.path.join(root, "srv", iid)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
                fh.write(b"x" * 64)
            add_row(m, iid, status=STATUS_COMPLETE,
                    file_path="srv/%s/media.mkv" % iid)
        m.stop()
        self._launch(root).stop()      # the backup now holds those rows
        return os.path.join(root, SyncManager.CATALOG_BACKUP)

    def _backup_rows(self, path):
        db = SyncDB(path)
        try:
            return len(db.list())
        finally:
            db.close()

    def test_a_missing_catalog_is_restored_not_replaced(self):
        root = os.path.join(self.tmp, "root")
        backup = self._seeded(root)
        # Only the catalog. Removing its -wal/-shm here as well was the
        # fixture performing the repair the code is supposed to perform --
        # see test_a_stale_wal_is_not_replayed_over_the_restore.
        os.remove(os.path.join(root, "catalog.db"))
        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 3,
                         "a missing catalog was replaced with an empty one "
                         "while the backup that described the store sat "
                         "beside it")
        m.stop()
        self.assertEqual(self._backup_rows(backup), 3)

    def test_a_failed_restore_does_not_eat_the_backup(self):
        root = os.path.join(self.tmp, "root")
        backup = self._seeded(root)
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))

        real = shutil.copyfile

        def full_disk(*a, **kw):
            raise OSError(28, "No space left on device")

        shutil.copyfile = full_disk
        try:
            m = self._launch(root)
        finally:
            shutil.copyfile = real
        m.stop()
        self.assertEqual(self._backup_rows(backup), 3,
                         "the launch that could not restore the backup "
                         "overwrote it with the empty catalog it had just "
                         "opened, so the next launch had nothing left")
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3)

    def test_and_the_next_launch_then_recovers(self):
        """The point of keeping it: the failure is transient, the backup is
        not."""
        root = os.path.join(self.tmp, "root")
        self._seeded(root)
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))
        real = shutil.copyfile
        shutil.copyfile = lambda *a, **kw: (_ for _ in ()).throw(
            OSError(28, "No space left on device"))
        try:
            self._launch(root).stop()
        finally:
            shutil.copyfile = real
        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 3)

    def test_deleting_every_download_does_not_overwrite_the_backup(self):
        """The route that reaches the empty-catalog guard without any failure
        at all.

        The class's other cases stopped reaching it: they leave the catalog
        *corrupt*, so `_backup_catalog` returns at `not healthy()` and the
        guard below it is never evaluated. It survived being replaced with
        `if False:` against all four of them.
        """
        root = os.path.join(self.tmp, "root")
        backup = self._seeded(root)
        m = self._launch(root)
        for row in list(m.db.list()):
            m.delete_item(row["item_id"])
        self.assertEqual(m.db.list(), [])
        m.stop()

        self._launch(root).stop()      # the launch that would overwrite it
        self.assertEqual(self._backup_rows(backup), 3,
                         "an empty catalog replaced a backup that still "
                         "described the store")

    def test_an_empty_catalog_is_still_backed_up_when_there_is_no_backup(self):
        """The control: this must not turn into "never back up an empty
        catalog", or a fresh install never gets one."""
        root = os.path.join(self.tmp, "root")
        os.makedirs(root)
        self._launch(root).stop()
        self.assertTrue(os.path.exists(
            os.path.join(root, SyncManager.CATALOG_BACKUP)))


class TheCatalogLeavesTheOldRootLastTest(TmpTest):
    def test_the_move_order_is_media_then_backup_then_catalog(self):
        """A move killed outright never runs `_undo_move`, so the order is
        what decides whether the old root can still describe itself.

        Watches the real `os.rename` calls `_move_tree` makes. Sorting a list
        the test built with a key the test wrote passed against the exact bug
        it was named for -- `_move_tree` was never called at all.
        """
        old = os.path.join(self.tmp, "old")
        new = os.path.join(self.tmp, "new")
        os.makedirs(os.path.join(old, "srv"))
        os.makedirs(new)
        for name in ("catalog.db", SyncManager.CATALOG_BACKUP, "other"):
            with open(os.path.join(old, name), "wb") as fh:
                fh.write(b"x")

        moved = []
        real_rename = os.rename

        def record(src, dst, *a, **kw):
            if os.path.dirname(src) == old:
                moved.append(os.path.basename(src))
            return real_rename(src, dst, *a, **kw)

        os.rename = record
        try:
            SyncManager()._move_tree(old, new)
        finally:
            os.rename = real_rename

        self.assertEqual(sorted(moved),
                         sorted(["srv", "other", "catalog.db",
                                 SyncManager.CATALOG_BACKUP]),
                         "not every entry was moved: %r" % (moved,))
        self.assertEqual(moved[-2:],
                         [SyncManager.CATALOG_BACKUP, "catalog.db"],
                         "a kill in this window has to leave the old root "
                         "able to describe itself: media, then the backup, "
                         "then the catalog. Order was %r" % (moved,))


class AskingForItAgainWithdrawsTheCancelTest(TmpTest):
    """`_cancelled` outlives the delete that raised it — the worker only
    honours it between chunks, and a chunk can take up to the 60s read
    timeout. Changing your mind inside that window used to report the item
    queued and then have the unwind delete it underneath."""

    def _manager(self):
        m = make_manager(self.tmp, self.addCleanup)
        item = {"Id": "a", "Type": "Movie", "Name": "Film",
                "MediaSources": [{"Id": "s", "Container": "mkv"}]}

        class C:
            config = type("cfg", (), {"data": {"auth.server-id": "srv"}})()
            jellyfin = type("jf", (), {
                "get_item": staticmethod(lambda i, fields=None: item),
                "download_url": staticmethod(
                    lambda i, include_apikey=True: "http://example/d"),
            })()

        m.get_client = lambda uuid: C()
        return m

    def test_a_re_enqueue_during_the_unwind_survives(self):
        m = self._manager()
        m.enqueue("u", "a", "Movie", include_watched=True)

        def fake_stream(url, dest, item_id, name, expected, stopping=None,
                        headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 8)
            self.assertTrue(m.delete(item_id="a"))
            self.assertEqual(m.enqueue("u", "a", "Movie", include_watched=True), 1)
            raise manager_module.requests.RequestException("reset")

        m._stream = fake_stream
        with self.assertRaises(manager_module.requests.RequestException):
            m._download(m.db.get("a"))
        self.assertIsNotNone(m.db.get("a"),
                             "the download the user re-requested was deleted "
                             "by the unwind of the one they cancelled")
        self.assertIsNotNone(m._next_runnable())

    def test_a_delete_with_no_re_enqueue_still_goes(self):
        """The control, so the withdrawal cannot become "never cancel"."""
        m = self._manager()
        m.enqueue("u", "a", "Movie", include_watched=True)

        def fake_stream(url, dest, item_id, name, expected, stopping=None,
                        headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 8)
            self.assertTrue(m.delete(item_id="a"))
            raise manager_module.requests.RequestException("reset")

        m._stream = fake_stream
        with self.assertRaises(manager_module.requests.RequestException):
            m._download(m.db.get("a"))
        self.assertIsNone(m.db.get("a"))


class RelocateRefusesItsOwnSubdirectoryTest(TmpTest):
    """An empty folder INSIDE the current download folder passes both the
    equality check and the non-empty check, and `_copy_tree` then walks into
    the destination it is creating."""

    def test_a_nested_destination_is_refused(self):
        root = os.path.join(self.tmp, "offline")
        os.makedirs(os.path.join(root, "srv", ORPHAN_ID))
        m = make_manager(root, self.addCleanup)
        m.db.close()
        dest = os.path.join(root, "moved")
        ok, message = m.relocate(dest)
        self.assertFalse(ok)
        self.assertIn("inside", message)
        # And nothing was touched on the way to finding out.
        self.assertTrue(os.path.isdir(os.path.join(root, "srv", ORPHAN_ID)))
        self.assertFalse(os.path.exists(dest))

    def test_a_sibling_is_still_accepted(self):
        root = os.path.join(self.tmp, "offline")
        os.makedirs(root)
        m = make_manager(root, self.addCleanup)
        m.db.close()
        ok, message = m.relocate(os.path.join(self.tmp, "beside"))
        self.addCleanup(m.stop)
        self.assertTrue(ok, message)


class TheRollbackNeverOverwritesTest(TmpTest):
    """`_undo_move` puts a failed move back, and its safety rests on the old
    name still being the empty slot it vacated.

    Nothing in this process can break that -- `_relocating` shuts the
    mutators -- but another process or the user can, and `os.replace` would
    destroy what they wrote without a sound. Refusing costs nothing: the
    caller reports the move failed either way, and both copies survive.
    """

    def _raced_move(self, recreate):
        old = os.path.join(self.tmp, "old")
        new = os.path.join(self.tmp, "new")
        os.makedirs(old)
        os.makedirs(new)
        for name in ("a", "b"):
            with open(os.path.join(old, name), "w") as fh:
                fh.write("ORIGINAL-" + name)

        m = SyncManager()
        real_rename = os.rename
        calls = []
        moved_first = {}

        def raced(src, dst):
            calls.append(src)
            if len(calls) == 1:
                real_rename(src, dst)
                moved_first["path"] = src
                if recreate:
                    with open(src, "w") as fh:
                        fh.write("NEW USER DATA")
                return
            raise OSError(18, "force the copy path")   # EXDEV

        def full_disk(*a, **kw):
            raise OSError(28, "No space left on device")

        os.rename = raced
        try:
            with self.assertRaises(OSError):
                m._copy_tree = full_disk
                m._move_tree(old, new)
        finally:
            os.rename = real_rename
        return old, new, moved_first["path"]

    def test_a_source_recreated_during_the_move_is_not_replaced(self):
        old, new, first = self._raced_move(recreate=True)
        with open(first) as fh:
            self.assertEqual(fh.read(), "NEW USER DATA",
                             "the rollback destroyed a file written into the "
                             "store while the move was running")
        self.assertTrue(
            os.path.exists(os.path.join(new, os.path.basename(first))),
            "and it did not keep the moved copy either, so that entry is gone")

    def test_an_untouched_source_is_still_put_back(self):
        """The control, so the refusal cannot become "never roll back"."""
        old, new, first = self._raced_move(recreate=False)
        with open(first) as fh:
            self.assertEqual(fh.read(),
                             "ORIGINAL-" + os.path.basename(first))
        self.assertFalse(
            os.path.exists(os.path.join(new, os.path.basename(first))),
            "the move was undone but a duplicate was left at the new root")


def _zero_page_one(catalog_path):
    """Zero the file header. sqlite refuses the file outright."""
    with open(catalog_path, "r+b") as fh:
        fh.write(b"\x00" * 16)


def _garbage_sqlite_master(catalog_path):
    """Wreck the schema table, which `_corrupt_downloads_table` keeps intact."""
    with open(catalog_path, "r+b") as fh:
        fh.seek(100)
        fh.write(b"\xff" * 3996)


def _truncate_mid_page(catalog_path):
    with open(catalog_path, "r+b") as fh:
        fh.truncate(2048)


def _make_empty_file(catalog_path):
    """A zero-byte catalog: a truncated write, or a copy that never finished."""
    with open(catalog_path, "wb"):
        pass


def _replace_with_text(catalog_path):
    with open(catalog_path, "wb") as fh:
        fh.write(b"this is not a database\n" * 200)


class ADeleteThatRemovedNothingIsNotADeleteTest(TmpTest):
    """`_remove_files` is `rmtree(ignore_errors=True)`, so it cannot raise --
    and "cannot raise" is not "succeeded".

    A locked item directory is the ordinary Windows case: the media open in a
    player, an indexer or a scanner holding it. Nothing crashes; the files
    simply stay. Dropping the row anyway hands the next launch an item
    directory with no row, which is the orphan shape -- so `_adopt_orphan`
    rebuilds it, and marks it `user` because a row it invented has no evidence
    of having been scheduled. The download the user deleted comes back, and
    comes back in the one state the reaper will never remove.
    """

    def _launch(self, root):
        m = SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        m._open_and_run()
        self.addCleanup(m.stop)
        return m

    def _store(self, root, ids, origin=ORIGIN_USER):
        """A store whose downloads describe themselves, as `_download` leaves
        them -- `item.json` beside the media is what `_adopt_orphan` reads."""
        os.makedirs(root, exist_ok=True)
        m = self._launch(root)
        for iid in ids:
            item_dir = os.path.join(root, "srv", iid)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
                fh.write(b"x" * 64)
            with open(os.path.join(item_dir, "item.json"), "w") as fh:
                json.dump({"Id": iid, "Type": "Movie", "Name": iid}, fh)
            with open(os.path.join(item_dir, "source.json"), "w") as fh:
                json.dump({"Id": "ms"}, fh)
            add_row(m, iid, status=STATUS_COMPLETE, origin=origin,
                    file_path="srv/%s/media.mkv" % iid)
        return m

    @staticmethod
    @contextlib.contextmanager
    def _locked_files():
        """Every removal silently does nothing, which is what `rmtree` with
        `ignore_errors=True` looks like from the caller's side."""
        real = shutil.rmtree
        shutil.rmtree = lambda *a, **kw: None
        try:
            yield
        finally:
            shutil.rmtree = real

    #: A sibling download, so `srv` stays a server directory the catalog
    #: names. The orphan sweep only walks those, so with one download the
    #: leftover directory is never even looked at.
    OTHER = "%032x" % 8

    def test_a_delete_that_removed_no_files_keeps_the_row(self):
        root = os.path.join(self.tmp, "kept")
        iid = "%032x" % 7
        m = self._store(root, [iid, self.OTHER])

        with self._locked_files():
            removed = m.delete_item(iid)

        self.assertFalse(removed,
                         "a delete that removed nothing reported success")
        self.assertIsNotNone(m.db.get(iid),
                             "the row went while its files stayed, which is "
                             "the orphan shape")

    def test_a_delete_that_removed_no_files_does_not_resurrect_it(self):
        """The end state, which is worse than the leak: back in the library
        and no longer reapable."""
        root = os.path.join(self.tmp, "resurrected")
        iid = "%032x" % 7
        m = self._store(root, [iid, self.OTHER], origin=ORIGIN_AUTO_NEXT_UP)
        with self._locked_files():
            m.delete_item(iid)
        m.stop()

        m2 = self._launch(root)
        row = m2.db.get(iid)
        self.assertIsNotNone(row, "the download vanished from the catalog "
                                  "while its files stayed on disk")
        self.assertEqual(row.get("origin"), ORIGIN_AUTO_NEXT_UP,
                         "the deleted download was re-adopted as a user "
                         "download, which the reaper will never remove")

    def test_a_bulk_delete_that_removed_no_files_keeps_its_rows(self):
        root = os.path.join(self.tmp, "bulk")
        ids = ["%032x" % i for i in range(3)]
        m = self._store(root, ids)
        for iid in ids:
            m.db.update(iid, series_id="show")

        with self._locked_files():
            m.delete(series_id="show")

        self.assertEqual(len(m.db.list()), 3,
                         "every row went while every file stayed")

    def test_a_delete_that_works_still_removes_both(self):
        """The control, so the guard cannot become "never delete"."""
        root = os.path.join(self.tmp, "control")
        iid = "%032x" % 7
        m = self._store(root, [iid, self.OTHER])

        self.assertTrue(m.delete_item(iid))
        self.assertIsNone(m.db.get(iid))
        self.assertFalse(os.path.isdir(os.path.join(root, "srv", iid)))


class EveryDamagedCatalogGetsAVerdictTest(TmpTest):
    """`_open_catalog` promises one of three verdicts for whatever is at the
    catalog path. It used to promise that for one modelled corruption.

    The guard is `healthy()`, and it is asked *after* `SyncDB` has opened the
    file writable -- which is not a read: the constructor runs
    `executescript(_SCHEMA)` and the migration. So a file damaged past the
    schema raises straight out of this method, and a zero-byte one has the
    tables created inside it and comes back **trusted and empty**, which is
    the one answer the whole restore path exists to prevent.

    `_corrupt_downloads_table` -- the fixture the rest of the restore suite is
    built on -- reaches neither: it zeroes the `downloads` root page and
    deliberately leaves `sqlite_master` readable so the schema still migrates.
    """

    def _launch(self, root):
        m = SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        m._open_and_run()
        self.addCleanup(m.stop)
        return m

    def _seeded(self, root, n=3):
        os.makedirs(root, exist_ok=True)
        m = self._launch(root)
        for i in range(n):
            iid = "%032x" % i
            item_dir = os.path.join(root, "srv", iid)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
                fh.write(b"x" * 64)
            add_row(m, iid, status=STATUS_COMPLETE,
                    file_path="srv/%s/media.mkv" % iid)
        m.stop()
        self._launch(root).stop()      # the backup now holds those rows
        return m

    def test_every_damaged_catalog_is_restored_from_the_backup(self):
        for name, damage in (("empty file", _make_empty_file),
                             ("zeroed header", _zero_page_one),
                             ("garbage schema", _garbage_sqlite_master),
                             ("truncated", _truncate_mid_page),
                             ("not a database", _replace_with_text),
                             ("zeroed downloads", _corrupt_downloads_table)):
            with self.subTest(name):
                root = os.path.join(self.tmp, "damaged-" + name.replace(" ", "-"))
                self._seeded(root)
                damage(os.path.join(root, "catalog.db"))

                m = self._launch(root)
                self.assertEqual(len(m.db.list()), 3,
                                 "the backup was never restored, so the store "
                                 "is described by nothing")
                self.assertEqual(
                    len(os.listdir(os.path.join(root, "srv"))), 3,
                    "a download was swept on the strength of a catalog that "
                    "could not be read")

    def test_a_catalog_that_reads_is_still_trusted(self):
        """The control. A guard that answers "restore" to everything would
        pass the test above and re-list the whole store on every launch."""
        root = os.path.join(self.tmp, "intact")
        self._seeded(root)
        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 3)
        self.assertFalse(
            glob.glob(os.path.join(root, "catalog.db.corrupt-*")),
            "an intact catalog was set aside and restored over")

    def test_a_catalog_that_will_not_open_and_has_no_backup_sweeps_nothing(self):
        """The end of the road: nothing readable, nothing to restore from.

        There is still one wrong answer available -- open it writable, let
        sqlite create the tables, and hand the sweep an empty catalog that
        says none of this media is known.
        """
        root = os.path.join(self.tmp, "no-backup")
        self._seeded(root)
        _zero_page_one(os.path.join(root, "catalog.db"))
        os.remove(os.path.join(root, SyncManager.CATALOG_BACKUP))

        m = self._launch(root)
        self.assertFalse(m.db.healthy(),
                         "a catalog that would not open came back healthy")
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3,
                         "the sweep ran on the strength of a catalog that "
                         "was never read")

    def test_a_backup_that_will_not_open_either_sweeps_nothing(self):
        """The restore promotes whatever the backup is -- `copyfile` is happy
        to copy a damaged one -- so the open that follows it needs the same
        answer as the open before it."""
        root = os.path.join(self.tmp, "both-damaged")
        self._seeded(root)
        _zero_page_one(os.path.join(root, "catalog.db"))
        _zero_page_one(os.path.join(root, SyncManager.CATALOG_BACKUP))

        m = self._launch(root)
        self.assertFalse(m.db.healthy(),
                         "the restored copy of a damaged backup came back "
                         "healthy")
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3,
                         "the sweep ran after a restore that restored nothing")

    def test_an_emptied_catalog_is_not_a_damaged_one(self):
        """Deleting every download leaves a valid catalog with no rows. That
        is *readable*, and restoring the backup over it would resurrect
        exactly the downloads the user just removed."""
        root = os.path.join(self.tmp, "emptied")
        self._seeded(root)
        db = SyncDB(os.path.join(root, "catalog.db"))
        for i in range(3):
            db.delete("%032x" % i)
        db.close()

        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 0,
                         "an emptied catalog was mistaken for a damaged one "
                         "and the backup was restored over it")


class TheRestoreIsOnePathTest(TmpTest):
    """The catalog is restored from its backup in exactly one function, for
    both emergencies -- unreadable and gone.

    Written as two branches it drifted immediately: the missing-catalog one
    got the copy and neither the staging nor the sidecar handling, so a full
    disk made the loss permanent and a stale `-wal` replayed the very pages
    the restore was recovering from. Each test here is one step of that
    function, reached down whichever branch can reach it.
    """

    def _launch(self, root):
        m = SyncManager()
        m.root = root
        m.get_client = lambda uuid: None
        m._open_and_run()
        self.addCleanup(m.stop)
        return m

    def _seeded(self, root, n=3):
        os.makedirs(root, exist_ok=True)
        m = self._launch(root)
        for i in range(n):
            iid = "%032x" % i
            item_dir = os.path.join(root, "srv", iid)
            os.makedirs(item_dir, exist_ok=True)
            with open(os.path.join(item_dir, "media.mkv"), "wb") as fh:
                fh.write(b"x" * 64)
            add_row(m, iid, status=STATUS_COMPLETE,
                    file_path="srv/%s/media.mkv" % iid)
        m.stop()
        self._launch(root).stop()      # the backup now holds those rows
        return os.path.join(root, SyncManager.CATALOG_BACKUP)

    @staticmethod
    def _full_disk(src, dst, *a, **kw):
        # copyfile opens the destination before it writes, so the failure
        # leaves a zero-byte file behind — which is the whole problem.
        with open(dst, "wb"):
            pass
        raise OSError(28, "No space left on device")

    def _with_a_full_disk(self, fn):
        real = shutil.copyfile
        shutil.copyfile = self._full_disk
        try:
            fn()
        finally:
            shutil.copyfile = real

    def test_a_stale_wal_is_not_replayed_over_the_restore(self):
        """The `-wal` beside a catalog that has GONE belongs to the catalog
        that went, and sqlite applies it to whatever takes that name next."""
        root = os.path.join(self.tmp, "wal")
        self._seeded(root)
        catalog = os.path.join(root, "catalog.db")
        db = SyncDB(catalog)
        holder = type("m", (), {"db": db, "root": root})()
        for i in range(400):
            add_row(holder, "%032x" % (i + 500))

        # Take a copy of the live WAL, *then* close, then put it back. A
        # clean close checkpoints the WAL away, and the state under test is a
        # WAL that outlived its catalog. Closing first is also the more
        # faithful shape -- the process that owned the catalog is gone -- and
        # it is required on Windows, which will not unlink a file that
        # somebody still holds open.
        wal = catalog + "-wal"
        self.assertTrue(os.path.exists(wal), "no WAL was written to save")
        with open(wal, "rb") as fh:
            stale = fh.read()
        self.assertTrue(stale, "the WAL was empty, so it proves nothing")
        db.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(catalog + suffix)
            except OSError:
                pass
        with open(wal, "wb") as fh:
            fh.write(stale)
        self.assertFalse(os.path.exists(catalog),
                         "the catalog did not actually go away")

        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 3,
                         "the stale WAL of the catalog that went missing was "
                         "replayed over the backup we had just restored")

    def _sidecar_fails_with(self, errnum):
        """Make the -wal/-shm step fail the way a locked file does, and leave
        every other removal and rename alone."""
        real_remove, real_replace = os.remove, os.replace

        def remove(path, *a, **kw):
            if path.endswith(("-wal", "-shm")):
                raise OSError(errnum, os.strerror(errnum), path)
            return real_remove(path, *a, **kw)

        def replace(src, dst, *a, **kw):
            if src.endswith(("-wal", "-shm")):
                raise OSError(errnum, os.strerror(errnum), src)
            return real_replace(src, dst, *a, **kw)

        os.remove, os.replace = remove, replace
        self.addCleanup(setattr, os, "remove", real_remove)
        self.addCleanup(setattr, os, "replace", real_replace)

        def unlock():
            os.remove, os.replace = real_remove, real_replace
        return unlock

    def test_a_sidecar_that_will_not_move_aborts_the_restore(self):
        """The sidecar step is not best-effort: the promote that follows it
        happens on the strength of it, so a failure there must abort.

        ENOENT is the one tolerable failure -- there is usually no `-wal` at
        all -- which is why the handler existed. Anything else is a sidecar
        that is still lying beside the name we are about to promote onto.
        """
        for name, errnum, aborts in (("locked", errno.EACCES, True),
                                     ("busy", errno.EBUSY, True),
                                     ("absent", errno.ENOENT, False)):
            with self.subTest(name):
                root = os.path.join(self.tmp, "sidecar-" + name)
                backup = self._seeded(root)
                catalog = os.path.join(root, "catalog.db")
                os.remove(catalog)
                with open(catalog + "-wal", "wb") as fh:
                    fh.write(b"\x00" * 32)
                self._sidecar_fails_with(errnum)

                m = SyncManager()
                m.root = root
                ok, _aside = m._restore_from_backup(catalog, backup)

                self.assertEqual(ok, not aborts)
                self.assertEqual(os.path.exists(catalog), not aborts,
                                 "the backup was promoted over a sidecar that "
                                 "is still there")
                self.assertFalse(os.path.exists(catalog + ".restoring"),
                                 "the staged copy was left behind")

    def test_a_sidecar_that_will_not_move_leaves_every_download_alone(self):
        """End to end, against a WAL that really does replay.

        A dummy sidecar cannot fail this test -- sqlite ignores one it cannot
        parse -- so the stale WAL here is a real one, captured while the
        catalog held a single row. Promoted over the three-row backup it reads
        back clean and integrity-checks ok, which is what makes the loss
        silent: `healthy()` says yes, `_backup_catalog` overwrites the good
        backup with it, and the launch after that sweeps the media whose rows
        went missing.
        """
        root = os.path.join(self.tmp, "sidecar-e2e")
        self._seeded(root)
        catalog = os.path.join(root, "catalog.db")
        ids = ["%032x" % i for i in range(3)]

        # A WAL from when the catalog held one row...
        db = SyncDB(catalog)
        for iid in ids[1:]:
            db.delete(iid)
        with open(catalog + "-wal", "rb") as fh:
            stale = fh.read()
        db.close()
        self.assertTrue(stale, "the WAL was empty, so it proves nothing")
        # ...and a catalog that has since gone back to three and been backed up.
        db = SyncDB(catalog)
        holder = type("m", (), {"db": db, "root": root})()
        for iid in ids[1:]:
            add_row(holder, iid, status=STATUS_COMPLETE,
                    file_path="srv/%s/media.mkv" % iid)
        db.close()
        self._launch(root).stop()

        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(catalog + suffix)
            except OSError:
                pass
        with open(catalog + "-wal", "wb") as fh:
            fh.write(stale)

        unlock = self._sidecar_fails_with(errno.EACCES)
        self._launch(root).stop()
        unlock()          # the lock is transient; the next launch is clean

        m = self._launch(root)
        self.assertEqual(len(os.listdir(os.path.join(root, "srv"))), 3,
                         "a download was deleted on the strength of a "
                         "catalog restored over a live sidecar")
        self.assertEqual(len(m.db.list()), 3,
                         "the catalog no longer describes every download")

    def test_a_failed_restore_leaves_nothing_to_mistake_for_empty(self):
        """An empty catalog is *readable*, so a launch that leaves one behind
        is telling every later launch there is nothing to recover."""
        for name, break_it in (("missing", os.remove),
                               ("corrupt", _corrupt_downloads_table)):
            with self.subTest(name):
                root = os.path.join(self.tmp, "failed-" + name)
                self._seeded(root)
                break_it(os.path.join(root, "catalog.db"))
                self._with_a_full_disk(lambda: self._launch(root).stop())

                m = self._launch(root)   # the retry launch, disk now fine
                self.assertEqual(len(m.db.list()), 3,
                                 "the restore was never retried")
                self.assertEqual(
                    len(os.listdir(os.path.join(root, "srv"))), 3,
                    "media was swept on the strength of an empty catalog")

    def test_a_failure_between_the_two_renames_is_recoverable(self):
        """The window the staging opens: the old catalog is already aside and
        the staged copy has not landed. Nothing may create a catalog there."""
        root = os.path.join(self.tmp, "gap")
        self._seeded(root)
        _corrupt_downloads_table(os.path.join(root, "catalog.db"))

        real_replace = os.replace

        def fail_the_promote(src, dst, *a, **kw):
            if str(src).endswith(".restoring"):
                raise OSError(28, "No space left on device")
            return real_replace(src, dst, *a, **kw)

        os.replace = fail_the_promote
        try:
            self._launch(root).stop()
        finally:
            os.replace = real_replace

        m = self._launch(root)
        self.assertEqual(len(m.db.list()), 3,
                         "the launch after a failure between the renames "
                         "found a catalog it saw nothing wrong with")

    def test_a_restored_catalog_is_reconciled_but_never_swept(self):
        """A restore is *behind* the disk, so the sweep must not run -- and
        the reconcile still must, or a COMPLETE row whose file is gone is
        never requeued. Asking `os.path.exists` before the restore reported a
        recovered catalog as a first run and skipped both."""
        root = os.path.join(self.tmp, "behind")
        self._seeded(root)
        os.remove(os.path.join(root, "catalog.db"))
        # Media the backup does not know about: the shape a sweep deletes.
        stranger = os.path.join(root, "srv", "%032x" % 99)
        os.makedirs(stranger)
        with open(os.path.join(stranger, "media.mkv"), "wb") as fh:
            fh.write(b"x" * 64)
        # And a row whose file the user removed: the shape a reconcile fixes.
        os.remove(os.path.join(root, "srv", "%032x" % 0, "media.mkv"))

        m = self._launch(root)
        self.assertTrue(os.path.isdir(stranger),
                        "the orphan sweep ran against a catalog that is "
                        "older than the disk")
        self.assertEqual(m.db.get("%032x" % 0)["status"], STATUS_PENDING,
                         "the reconcile was skipped entirely, so a download "
                         "whose file is gone was never requeued")


class RelocateAnswersInsteadOfRaisingTest(TmpTest):
    """`relocate` is documented to return (ok, message); every refusal it can
    reach has to arrive that way, because `settings/general.py` catches the
    exception and shows the one generic "moving failed"."""

    def test_a_second_drive_is_not_containment(self):
        """`ntpath.commonpath` *raises* across drives. Cross-drive is the
        entire reason the EXDEV copy path exists, so raising here made the
        one move that needs it impossible on Windows."""
        import ntpath
        m = make_manager(self.tmp, self.addCleanup)
        m.db.close()
        m.root = r"C:\Users\Izzie\AppData\Roaming\jellyfin-mpv-shim\offline"

        real = (os.path.commonpath, os.path.abspath, os.path.realpath,
                os.path.expanduser)
        os.path.commonpath = ntpath.commonpath
        os.path.abspath = lambda p: p
        os.path.realpath = lambda p: p
        os.path.expanduser = lambda p: p
        # `relocate` gets past containment and really tries the move, and
        # with abspath stubbed out a Windows path is a *relative* name here.
        # Run from the temp dir or it lands in the repo -- which it did.
        here = os.getcwd()
        os.chdir(self.tmp)
        try:
            result = m.relocate(r"D:\Media\JellyfinDownloads")
        except Exception as exc:            # noqa: BLE001 - that is the bug
            self.fail("relocate raised %s instead of answering: %s"
                      % (type(exc).__name__, exc))
        finally:
            os.chdir(here)
            (os.path.commonpath, os.path.abspath, os.path.realpath,
             os.path.expanduser) = real
        self.assertIsInstance(result, tuple)
        self.assertEqual(len(result), 2)
        # It got past containment: whatever it says next, it is not "that
        # folder is inside the current download folder".
        self.assertNotIn("inside", (result[1] or "").lower())

    def test_a_store_reached_through_a_link_is_still_the_store(self):
        """The other half: the old root is itself reached through a link, so
        the destination looks unrelated until both sides are resolved."""
        real = os.path.join(self.tmp, "real")
        os.makedirs(os.path.join(real, "inside"))
        seen_as = os.path.join(self.tmp, "seen-as")
        try:
            os.symlink(real, seen_as)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        m = make_manager(seen_as, self.addCleanup)
        m.db.close()
        ok, message = m.relocate(os.path.join(real, "inside"))
        self.assertFalse(ok, "a folder inside the store was accepted because "
                             "the store was named through a link")
        self.assertIn("inside", message.lower())

    def test_a_symlink_back_into_the_store_is_still_containment(self):
        """The refusal is about where the bytes land. Compared textually, a
        link resolving inside the store passed, and `_copy_tree` then walked
        into the destination it was creating."""
        root = os.path.join(self.tmp, "offline")
        os.makedirs(os.path.join(root, "inside"))
        link = os.path.join(self.tmp, "link")
        try:
            os.symlink(os.path.join(root, "inside"), link)
        except (OSError, NotImplementedError):
            self.skipTest("no symlinks here")
        m = make_manager(root, self.addCleanup)
        m.db.close()
        ok, message = m.relocate(link)
        self.assertFalse(ok, "a link resolving inside the store was accepted")
        self.assertIn("inside", message.lower())


class TheDeleteWinsEverywhereTest(TmpTest):
    """One table, because "the delete wins" is a property of *leaving*
    `_download` and not of any one exit from it.

    It used to be written out at three sites with three different
    combinations of check, discard, remove-files, delete-row and notify. The
    handler that skipped the re-check deleted a download the user had asked
    for again -- and the per-fix test written beside each site could not see
    it, because each one asserted its own site.
    """

    ITEM = {"Id": "a", "Type": "Movie", "Name": "Film",
            "MediaSources": [{"Id": "s", "Container": "mkv"}]}

    def _manager(self):
        m = make_manager(self.tmp, self.addCleanup)
        item = self.ITEM

        class C:
            config = type("cfg", (), {"data": {"auth.server-id": "srv"}})()
            jellyfin = type("jf", (), {
                "get_item": staticmethod(lambda i, fields=None: item),
                "download_url": staticmethod(
                    lambda i, include_apikey=True: "http://example/d"),
            })()

        m.get_client = lambda uuid: C()
        return m

    #: name -> what `_stream` does. Between them these reach every way out of
    #: `_download`: the commit point, each handler, and the plain return.
    EXITS = {
        "completes": lambda m, t: (8, 8),
        "ends_short": lambda m, t: (4, 8),
        "chunk_sees_the_cancel": lambda m, t: _raise(manager_module._Cancelled()),
        "shutdown": lambda m, t: _raise(manager_module._Stopped()),
        "connection_dropped": lambda m, t: _raise(
            manager_module.requests.RequestException("reset")),
        "server_error": lambda m, t: _raise(_http_error(503)),
        "gone_from_the_server": lambda m, t: _raise(_http_error(404)),
        "something_unexpected": lambda m, t: _raise(RuntimeError("boom")),
    }

    #: name -> what the user does while the download is in flight, and
    #: whether a delete is owed when `_download` returns.
    TIMINGS = {
        "no_delete": (lambda m: None, False),
        "deleted_mid_flight": (lambda m: m.delete(item_id="a"), True),
        "deleted_then_asked_again": (
            lambda m: (m.delete(item_id="a"),
                       m.enqueue("u", "a", "Movie", include_watched=True)),
            False),
    }

    def _run_one(self, exit_name, timing_name):
        m = self._manager()
        m.enqueue("u", "a", "Movie", include_watched=True)
        act, owed = self.TIMINGS[timing_name]
        stream = self.EXITS[exit_name]

        def fake_stream(url, dest, item_id, name, expected, stopping=None,
                        headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 8)
            act(m)                       # the user, mid-transfer
            return stream(m, self)

        m._stream = fake_stream
        try:
            m._download(m.db.get("a"))
        except (manager_module.requests.RequestException, RuntimeError):
            pass                          # re-raised on purpose for the backoff
        return m, owed

    def test_every_exit_honours_a_delete_and_none_honours_a_withdrawn_one(self):
        for exit_name in self.EXITS:
            for timing_name in self.TIMINGS:
                with self.subTest(exit=exit_name, timing=timing_name):
                    m, owed = self._run_one(exit_name, timing_name)
                    row = m.db.get("a")
                    if owed:
                        self.assertIsNone(
                            row, "the delete the user was told had succeeded "
                                 "did not survive this exit")
                        self.assertFalse(
                            os.path.exists(os.path.join(m.root, "srv", "a")),
                            "the row went but the bytes stayed")
                    else:
                        self.assertIsNotNone(
                            row, "the item was deleted although no delete was "
                                 "owed on the way out")
                    # True of every cell: nothing is left flagged or claimed.
                    self.assertEqual(m._cancelled, set())
                    self.assertIsNone(m._active_item)

    def test_a_cancel_that_beat_the_worker_is_not_downloaded_first(self):
        """The cancel that arrives before the worker reaches the row.

        The outcome alone cannot see this check: without it the item is
        fetched in full and *then* deleted at the commit point, which lands
        in the same place. What the entry check is for is not starting -- so
        that is what this asserts.
        """
        for withdrawn in (False, True):
            with self.subTest(withdrawn=withdrawn):
                m = self._manager()
                m.enqueue("u", "a", "Movie", include_watched=True)
                row = m.db.get("a")
                with m._active_lock:
                    m._cancelled.add("a")   # flagged as the worker will see it
                if withdrawn:
                    m._uncancel("a")
                started = []

                def fake_stream(*a, **k):
                    started.append(True)
                    return 8, 8

                m._stream = fake_stream
                m._download(row)
                self.assertEqual(bool(started), withdrawn,
                                 "a download the user had already deleted was "
                                 "transferred in full before being thrown away"
                                 if not withdrawn else
                                 "the withdrawn cancel stopped the download")
                if withdrawn:
                    self.assertIsNotNone(m.db.get("a"))
                else:
                    self.assertIsNone(m.db.get("a"))
                self.assertEqual(m._cancelled, set())
                self.assertIsNone(m._active_item)

    def test_the_row_delete_is_atomic_with_the_sample(self):
        """Sampling the cancel under `_active_lock` and deleting the row after
        releasing it leaves a window: an `enqueue` can withdraw the cancel and
        write a fresh row in it, and that row is what gets deleted -- the
        failure `_uncancel` exists to prevent, one window along.

        Asserted structurally rather than by racing it: with the delete inside
        the critical section there is no interleaving to reproduce, which is
        the point.
        """
        m = self._manager()
        m.enqueue("u", "a", "Movie", include_watched=True)
        held = []
        real_delete = m.db.delete

        def watched_delete(item_id):
            held.append(m._active_lock.locked())
            return real_delete(item_id)

        m.db.delete = watched_delete

        def fake_stream(url, dest, item_id, name, expected, stopping=None,
                        headers=None, on_headers=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 8)
            m.delete(item_id="a")
            raise manager_module.requests.RequestException("reset")

        m._stream = fake_stream
        with self.assertRaises(manager_module.requests.RequestException):
            m._download(m.db.get("a"))
        self.assertIsNone(m.db.get("a"))
        self.assertTrue(held, "the cancelled row was never deleted at all")
        self.assertTrue(all(held),
                        "the row was deleted outside `_active_lock`, so an "
                        "enqueue can slip a new row into the gap and have it "
                        "deleted instead")


class AnEnqueueOnlyWithdrawsWhatItKeepsTest(TmpTest):
    """`enqueue` withdrawing a delete says "this item will be present offline
    when I am done". Done unconditionally at the top of the loop, it also
    spoke for every item the loop then goes on to *decline*.

    The user-visible failure: delete a stalled in-flight episode, then press
    Download on the series it belongs to. The episode is watched, so the
    enqueue queues nothing at all -- and the deleted download resumed and
    completed anyway.
    """

    WATCHED = {"Id": "a", "Type": "Episode", "Name": "Ep", "SeriesId": "ser",
               "UserData": {"Played": True},
               "MediaSources": [{"Id": "s", "Container": "mkv"}]}

    def _manager(self):
        m = make_manager(self.tmp, self.addCleanup)
        ep = self.WATCHED

        class C:
            config = type("cfg", (), {"data": {"auth.server-id": "srv"}})()
            jellyfin = type("jf", (), {
                "get_item": staticmethod(lambda i, fields=None: ep),
                "get_episodes": staticmethod(
                    lambda i, fields=None, season_id=None: {"Items": [ep]}),
                "download_url": staticmethod(
                    lambda i, include_apikey=True: "http://example/d"),
            })()

        m.get_client = lambda uuid: C()
        return m

    def test_an_enqueue_that_queues_nothing_withdraws_nothing(self):
        m = self._manager()
        m.enqueue("u", "a", "Episode", include_watched=True)
        with m._active_lock:
            m._active_item = "a"           # the worker is on it
        self.assertTrue(m.delete(item_id="a"))
        self.assertIn("a", m._cancelled)

        self.assertEqual(m.enqueue("u", "ser", "Series"), 0,
                         "the fixture no longer declines the item, so this "
                         "test cannot see the bug it is named for")
        self.assertIn("a", m._cancelled,
                      "an enqueue that queued nothing withdrew the user's "
                      "delete of an in-flight download")

    def test_an_enqueue_that_does_keep_it_still_withdraws(self):
        """The control, so the fix cannot become "never withdraw"."""
        m = self._manager()
        m.enqueue("u", "a", "Episode", include_watched=True)
        with m._active_lock:
            m._active_item = "a"
        self.assertTrue(m.delete(item_id="a"))
        self.assertIn("a", m._cancelled)

        m.enqueue("u", "ser", "Series", include_watched=True)
        self.assertNotIn("a", m._cancelled,
                         "asking for it again did not withdraw the delete")


class AReapItDeclinesIsNotADeleteTest(TmpTest):
    """`delete_item(only_if_auto=True)` flags the item for the worker *before*
    it checks whether it may delete it. Declining has to withdraw the flag, or
    the worker carries out the delete the reaper just refused to make -- which
    is the single thing `only_if_auto` exists to prevent."""

    def test_declining_to_reap_withdraws_the_cancel(self):
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", status=STATUS_COMPLETE, origin=ORIGIN_USER)
        with m._active_lock:
            m._active_item = "a"           # the worker is on it right now
        self.assertFalse(m.delete_item("a", only_if_auto=True),
                         "the reaper took a row the user had claimed")
        self.assertEqual(m._cancelled, set(),
                         "the reaper declined the delete and left the flag "
                         "that makes the worker do it anyway")
        self.assertIsNotNone(m.db.get("a"))


def _raise(exc):
    raise exc


def _http_error(status):
    exc = manager_module.requests.HTTPError("HTTP %d" % status)
    exc.response = type("r", (), {"status_code": status})()
    return exc
