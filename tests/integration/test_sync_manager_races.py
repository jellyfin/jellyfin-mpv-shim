"""Concurrency/race tests for the offline-sync download manager.

These extend tests/test_sync_manager.py (whose fakes we reuse) into the
*threaded* territory: a real background worker, real temp files + SQLite, and a
deleter/stopper racing the worker. Where the fast suite simulates the
delete-at-commit window by mutating ``_cancelled`` inline, here we drive it from
a separate thread synchronised with a ``Barrier`` so the interleaving is forced,
not assumed.

Injectable seams keep it hermetic: ``_stream`` is replaced with a fake that can
pause at a chosen instant, and ``requests`` is never really called.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

from jellyfin_mpv_shim.sync import manager as manager_module  # noqa: E402
from jellyfin_mpv_shim.sync.db import (  # noqa: E402
    STATUS_PENDING, STATUS_COMPLETE, STATUS_ERROR)

# Reuse the fast suite's fakes/builders verbatim.
from tests.test_sync_manager import make_manager, add_row  # noqa: E402


class DeleteAtCommitRaceTest(h.TmpDirTest):
    def test_delete_landing_at_commit_window_is_honoured(self):
        # AUDIT RACE (S4): a delete that lands after the final chunk is written
        # but before the row is marked COMPLETE must win — the item ends deleted,
        # never left COMPLETE. We pin the interleaving with a barrier: the fake
        # stream finishes the .part, then blocks until the deleter has run
        # delete_item (which flags _cancelled because _active_item is set), then
        # returns into the commit check.
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        item_dir = m._item_dir(m.db.get("a"))

        at_finish = threading.Barrier(2)

        def fake_stream(url, dest, item_id, name, expected,
                        stopping=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 100)
            # Signal "last chunk written"; wait for the deleter to act.
            at_finish.wait(5)
            at_finish.wait(5)
            return 100, 100

        m._stream = fake_stream

        worker = threading.Thread(target=lambda: m._download(m.db.get("a")))
        worker.start()

        at_finish.wait(5)                 # stream has written the full .part
        m.delete_item("a")                # delete races in at the finish line
        at_finish.wait(5)                 # let the stream return into commit
        worker.join(5)

        self.assertFalse(worker.is_alive())
        self.assertIsNone(m.db.get("a"), "delete lost to a COMPLETE row")
        self.assertFalse(os.path.exists(item_dir), "files left after delete")
        self.assertNotIn("a", m._cancelled)
        self.assertIsNone(m._active_item)


class ShortReadStallEscalationTest(h.TmpDirTest):
    def test_repeated_short_read_escalates_pending_then_error(self):
        # A server that truncates at the same offset every time would resume
        # from the same size forever. After a few no-progress short reads the
        # row must escalate PENDING -> ERROR rather than retry indefinitely.
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        m._stream = lambda *a, **k: (40, 100)   # same truncation each attempt

        # The stall counter starts at 0 and increments once per no-progress
        # attempt, escalating to ERROR when it reaches 3 (the 4th attempt).
        for _ in range(3):
            m._download(m.db.get("a"))
            self.assertEqual(m.db.get("a")["status"], STATUS_PENDING)
        m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_ERROR)

    def test_short_read_that_makes_progress_stays_pending(self):
        # If each attempt advances, it must keep resuming (never escalate).
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        sizes = iter([30, 60, 90])
        m._stream = lambda *a, **k: (next(sizes), 100)
        for _ in range(3):
            m._download(m.db.get("a"))
            self.assertEqual(m.db.get("a")["status"], STATUS_PENDING)


class TransientErrorResumeTest(h.TmpDirTest):
    def test_request_exception_keeps_row_pending_and_reraises(self):
        # AUDIT: a dropped connection / read timeout is transient. The row must
        # stay PENDING (so the .part resumes) and the exception must propagate so
        # _run's backoff throttles the retry — it must NOT be marked ERROR.
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)

        def boom(*a, **k):
            raise manager_module.requests.ConnectionError("connection reset")

        m._stream = boom
        with self.assertRaises(manager_module.requests.RequestException):
            m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_PENDING)
        self.assertIsNone(m._active_item)

    def test_http_4xx_is_permanent_error(self):
        # Contrast: a 4xx (gone/forbidden) is permanent -> ERROR, not resumed.
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)

        def http404(*a, **k):
            err = manager_module.requests.HTTPError("404")
            err.response = type("R", (), {"status_code": 404})()
            raise err

        m._stream = http404
        m._download(m.db.get("a"))
        self.assertEqual(m.db.get("a")["status"], STATUS_ERROR)


class StopMidDownloadTest(h.TmpDirTest):
    def test_stop_joins_worker_midflight_and_closes_db(self):
        # AUDIT: stop() must join the worker so a write isn't killed mid-flight,
        # leave the interrupted item PENDING (resume next launch, .part kept),
        # and close the catalog. We park the fake stream until _stop flips.
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)

        streaming = threading.Event()

        def parked_stream(url, dest, item_id, name, expected,
                          stopping=None):
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest + ".part", "wb") as fh:
                fh.write(b"x" * 20)     # a partial download on disk
            streaming.set()
            # Mirror _stream's real per-chunk stop check.
            while not m._stop:
                time.sleep(0.005)
            raise manager_module._Stopped()

        m._stream = parked_stream
        m._stop = False
        m._worker = threading.Thread(target=m._run, daemon=True)
        m._worker.start()

        self.assertTrue(streaming.wait(5), "worker never began the download")
        m.stop()

        self.assertFalse(m._worker.is_alive(), "worker not joined by stop()")
        self.assertEqual(m.db._conn, None, "catalog left open after stop()")
        # The .part must survive so the resume path can pick it up next launch.
        part = m._item_dir({"server_id": "srv", "item_id": "a"})
        self.assertTrue(os.path.exists(os.path.join(part, "media.mkv.part")))

    def test_interrupted_download_resumes_from_part(self):
        # A download interrupted by a transient error leaves a .part; the next
        # attempt must resume by appending with a Range header, not restart.
        m = make_manager(self.tmp, self.addCleanup)
        add_row(m, "a", size_bytes=100)
        item_dir = m._item_dir(m.db.get("a"))
        os.makedirs(item_dir, exist_ok=True)
        dest = os.path.join(item_dir, "media.mkv")
        with open(dest + ".part", "wb") as fh:
            fh.write(b"a" * 40)          # 40 bytes already fetched

        seen_headers = {}

        class Resp:
            status_code = 206
            headers = {"Content-Length": "60"}

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def raise_for_status(self):
                pass

            def iter_content(self, size):
                yield b"b" * 60

        def fake_get(url, **kwargs):
            seen_headers.update(kwargs.get("headers") or {})
            return Resp()

        orig = manager_module.requests.get
        manager_module.requests.get = fake_get
        try:
            size, total = m._stream(dest, dest, "a", "a", 100)
        finally:
            manager_module.requests.get = orig

        self.assertEqual(seen_headers.get("Range"), "bytes=40-")
        self.assertEqual(size, 100)
        with open(dest + ".part", "rb") as fh:
            self.assertEqual(fh.read(), b"a" * 40 + b"b" * 60)


if __name__ == "__main__":
    unittest.main()
