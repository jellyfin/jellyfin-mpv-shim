"""Trickplay frame-file lifetime, and the contention fixes around it.

mpv MMAPS the frame file it is handed for overlay-add. That makes three
things unsafe, and all three were live:

  * rewriting one fixed path in place — `open(p, "wb")` truncates the inode
    mpv still has mapped, so the mapping extends past EOF (SIGBUS);
  * unlinking the file without telling the renderer first;
  * reporting more frames than were written, so mpv seeks past EOF.

These tests pin the fixes without needing an mpv.
"""

import io
import os
import sys
import tempfile
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

import jellyfin_mpv_shim.trickplay as trickplay  # noqa: E402


class FakePlayer:
    def __init__(self):
        self.trickplay_meta = None
        self.messages = []

    def script_message(self, *args):
        self.messages.append(args)


class FrameFileLifetimeTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self._orig = trickplay._img_path
        trickplay._img_path = lambda seq: os.path.join(
            self.dir.name, "raw_images.%d.bin" % seq)
        self.addCleanup(lambda: setattr(trickplay, "_img_path", self._orig))
        self.player = FakePlayer()
        self.tp = trickplay.TrickPlay.__new__(trickplay.TrickPlay)
        self.tp.player = self.player
        self.tp._seq = 0
        self.tp._current = None
        self.tp._file_lock = threading.Lock()

    def _write(self, path, data=b"x" * 16):
        with open(path, "wb") as fh:
            fh.write(data)

    def test_each_generation_gets_a_fresh_path(self):
        """The whole fix: the previous inode is never written again."""
        a = self.tp._next_file()
        b = self.tp._next_file()
        self.assertNotEqual(a, b)

    def test_publishing_retires_only_the_previous_file(self):
        a = self.tp._next_file()
        self._write(a)
        self.tp._publish(a)
        self.assertTrue(os.path.exists(a), "the live file was removed")

        b = self.tp._next_file()
        self._write(b)
        self.tp._publish(b)
        self.assertFalse(os.path.exists(a), "the old generation leaked")
        self.assertTrue(os.path.exists(b))

    def test_clear_tells_the_renderer_before_removing_the_file(self):
        """overlay-remove has to land before the bytes behind it go away."""
        a = self.tp._next_file()
        self._write(a)
        self.tp._publish(a)
        self.tp.clear()
        self.assertIn(("shim-trickplay-clear",), self.player.messages)
        self.assertFalse(os.path.exists(a))
        self.assertIsNone(self.player.trickplay_meta)

    def test_retiring_twice_is_harmless(self):
        a = self.tp._next_file()
        self._write(a)
        self.tp._publish(a)
        self.tp._retire_current()
        self.tp._retire_current()      # must not raise

    def test_unlink_tolerates_a_locked_or_missing_file(self):
        trickplay._unlink(None)
        trickplay._unlink(os.path.join(self.dir.name, "nope.bin"))

    def test_cleanup_removes_only_frame_files(self):
        keep = os.path.join(self.dir.name, "conf.json")
        self._write(keep)
        stale = [self.tp._next_file() for _ in range(3)]
        for p in stale:
            self._write(p)

        trickplay.cleanup_stale_files()

        for p in stale:
            self.assertFalse(os.path.exists(p), "stale frame file survived")
        self.assertTrue(os.path.exists(keep),
                        "cleanup removed an unrelated file")


class ShortTileRunTest(unittest.TestCase):
    """decompress_tiles must report frames WRITTEN, not frames promised: mpv
    seeks to frame * w * h * 4 inside a mapping of the file."""

    def _tile(self, w, h):
        from PIL import Image

        buf = io.BytesIO()
        Image.new("RGBA", (w, h), (1, 2, 3, 255)).save(buf, format="PNG")
        return buf.getvalue()

    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not available")

    def test_a_full_run_reports_every_frame(self):
        from jellyfin_mpv_shim import bifdecode

        fh = io.BytesIO()
        written = bifdecode.decompress_tiles(
            4, 4, 2, 2, 4, [self._tile(8, 8)], fh)
        self.assertEqual(written, 4)
        self.assertEqual(len(fh.getvalue()), 4 * 4 * 4 * 4)

    def test_a_short_source_reports_what_it_wrote(self):
        """A 404 on a late tile used to leave the manifest count intact,
        which sent mpv reading past EOF."""
        from jellyfin_mpv_shim import bifdecode

        fh = io.BytesIO()
        # Promised 8 frames, but only one 2x2 tile (4 frames) arrives.
        written = bifdecode.decompress_tiles(
            4, 4, 2, 2, 8, [self._tile(8, 8)], fh)
        self.assertEqual(written, 4, "the short run over-reported its count")
        # And the count matches the bytes actually on disk.
        self.assertEqual(len(fh.getvalue()), written * 4 * 4 * 4)

    def test_no_tiles_reports_zero(self):
        from jellyfin_mpv_shim import bifdecode

        self.assertEqual(
            bifdecode.decompress_tiles(4, 4, 2, 2, 8, [], io.BytesIO()), 0)


class StripCounterRaceTest(unittest.TestCase):
    """Two threads sharing a counter value produced two live cache entries on
    one path with different iw/ih — and the renderer bounds its crop by
    iw/ih, so one of them reads past the end of the other's file."""

    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not available")
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)

    def _store_fixture(self):
        from PIL import Image

        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        store = StripStore.__new__(StripStore)
        store.mem = None
        store._counter = 0
        store._lock = threading.Lock()
        store.dir = self.dir.name
        return store, Image.new("RGBA", (4, 4), (1, 2, 3, 255))

    def test_store_takes_the_lock_around_the_counter(self):
        """The load-bearing assertion.

        The stress test below cannot prove this — CPython almost never
        preempts inside the three bytecodes of `+= 1`, so it passes with the
        fix reverted (verified). This checks the fix directly instead.

        Also guards the deadlock direction: _lock is a plain Lock, so this
        only works because both _store call sites (bitmap, _compose) are
        deliberately outside it.
        """
        store, img = self._store_fixture()

        class RecordingLock:
            def __init__(self, inner):
                self.inner, self.acquired = inner, 0

            def __enter__(self):
                self.acquired += 1
                return self.inner.__enter__()

            def __exit__(self, *exc):
                return self.inner.__exit__(*exc)

        store._lock = RecordingLock(threading.Lock())
        store._store(img)
        self.assertEqual(store._lock.acquired, 1,
                         "the counter increment is not under the lock")

    def test_concurrent_stores_never_share_a_path(self):
        """Stress smoke test: drives the real _store from several threads, as
        the cast compositor (a pool worker) does against the loop thread.

        Probabilistic by nature — see the deterministic test above for the
        assertion that actually pins the fix.
        """
        store, img = self._store_fixture()
        seen, seen_lock = [], threading.Lock()
        start = threading.Event()

        def store_many():
            start.wait(5)
            for _ in range(50):
                src, _w, _h = store._store(img)
                with seen_lock:
                    seen.append(src)

        threads = [threading.Thread(target=store_many, daemon=True)
                   for _ in range(4)]
        for t in threads:
            t.start()
        start.set()
        for t in threads:
            t.join(20)

        self.assertEqual(len(seen), 200, "a worker did not finish")
        self.assertEqual(len(seen), len(set(seen)),
                         "two strips share one filename — the cache would "
                         "hold two entries on one path with different iw/ih")


if __name__ == "__main__":
    unittest.main()
