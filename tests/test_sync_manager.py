"""Reliability tests for the offline-sync download manager.

These exercise the resume/promote/size logic, the cancel-before-complete race,
the startup orphan sweep, and shutdown (worker join + catalog close) without
touching the network — `_stream` is stubbed or fed a fake `requests` response,
and the side-artwork/subtitle downloads are no-ops.
"""

import json
import os
import shutil
import tempfile
import threading
import time
import unittest

from jellyfin_mpv_shim.sync import manager as manager_module
from jellyfin_mpv_shim.sync.manager import SyncManager
from jellyfin_mpv_shim.sync.db import (SyncDB, STATUS_PENDING, STATUS_COMPLETE)


class FakeJellyfin:
    def download_url(self, item_id):
        return "http://example/download/%s" % item_id


class FakeClient:
    def __init__(self):
        self.jellyfin = FakeJellyfin()


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


def make_manager(root, cleanup=None):
    m = SyncManager()
    m.root = root
    m.db = SyncDB(os.path.join(root, "catalog.db"))
    if cleanup is not None:
        cleanup(lambda: m.db.close())
    m.get_client = lambda uuid: FakeClient()
    # Stub the side downloads / playback-info so _download stays offline.
    m._playback_source = lambda *a, **k: None
    m._download_artwork = lambda *a, **k: None
    m._download_subs = lambda *a, **k: None
    m._download_trickplay = lambda *a, **k: None
    m._download_series_art = lambda *a, **k: None
    m._download_season_art = lambda *a, **k: None
    return m


def add_row(m, item_id, server_id="srv", status=STATUS_PENDING, size_bytes=0,
            file_path=None):
    m.db.upsert({
        "item_id": item_id,
        "server_id": server_id,
        "server_uuid": "uuid",
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

        def fake_stream(url, dest, item_id, name, expected):
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

        def fake_stream(url, dest, item_id, name, expected):
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

        def fake_stream(url, dest, item_id, name, expected):
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
        orphan = os.path.join(self.tmp, "srv", "orphan")
        os.makedirs(orphan)
        m._reconcile_disk()
        self.assertFalse(os.path.exists(orphan))

    def test_series_and_season_caches_preserved(self):
        m = make_manager(self.tmp, self.addCleanup)
        series = os.path.join(self.tmp, "srv", "series", "s1")
        season = os.path.join(self.tmp, "srv", "season", "e1")
        os.makedirs(series)
        os.makedirs(season)
        m._reconcile_disk()
        self.assertTrue(os.path.exists(series))
        self.assertTrue(os.path.exists(season))

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

    def test_refuse_target_with_existing_catalog(self):
        old = os.path.join(self.tmp, "old")
        os.makedirs(old)
        m = make_manager(old, self.addCleanup)
        self._seed_download(m)
        target = os.path.join(self.tmp, "taken")
        os.makedirs(target)
        open(os.path.join(target, "catalog.db"), "w").close()
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
