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
