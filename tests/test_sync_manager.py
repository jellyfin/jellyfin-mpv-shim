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
        m.db.close()
        orphan = os.path.join(root, "srv", "nobody")
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
