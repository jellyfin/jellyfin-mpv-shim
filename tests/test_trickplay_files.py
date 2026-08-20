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
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

import jellyfin_mpv_shim.trickplay as trickplay  # noqa: E402


class FakePlayer:
    def __init__(self):
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
        # Module-level, and _publish now parks a file in it -- so without
        # this a test inherits the previous one's deferred removal and the
        # counts below stop meaning what they say.
        with trickplay._pending_lock:
            trickplay._pending_unlink[:] = []
        self.addCleanup(lambda: trickplay._pending_unlink.__setitem__(
            slice(None), []))

    def _write(self, path, data=b"x" * 16):
        with open(path, "wb") as fh:
            fh.write(data)

    def test_each_generation_gets_a_fresh_path(self):
        """The whole fix: the previous inode is never written again."""
        a = self.tp._next_file()
        b = self.tp._next_file()
        self.assertNotEqual(a, b)

    def _bins(self):
        return sorted(f for f in os.listdir(self.dir.name)
                      if f.endswith(".bin"))

    def test_a_retired_file_survives_exactly_one_more_publish(self):
        """The grace period, and why it is not a leak.

        ``script_message`` returns when the message is queued to lua, not
        when lua has run it, so a render firing in between still reads the
        OLD path. Unlinking as the new file is published is what turns that
        into a missing thumbnail -- once per window swap now, rather than
        once per video. So a retired file goes one publish later, and the
        disk holds at most two.
        """
        # Written as each is published, not up front: the point of the
        # assertions below is what is ON DISK, so a file that exists before
        # anything published it is noise in every one of them.
        a, b, c = (self.tp._next_file() for _ in range(3))

        self._write(a)
        self.tp._publish(a)
        self.assertEqual(self._bins(), [os.path.basename(a)])

        self._write(b)
        self.tp._publish(b)
        self.assertTrue(os.path.exists(a),
                        "the previous window went before lua could have "
                        "been told to stop reading it")
        self.assertTrue(os.path.exists(b), "the live file was removed")

        self._write(c)
        self.tp._publish(c)
        self.assertFalse(os.path.exists(a), "the old generation leaked")
        self.assertEqual(self._bins(),
                         sorted(map(os.path.basename, (b, c))),
                         "more than one window is being held over")

    def test_retiring_the_current_file_takes_the_held_one_with_it(self):
        """Teardown has to drain the grace period, or the last two windows
        of every session are left for the next run's cleanup."""
        a, b = (self.tp._next_file() for _ in range(2))
        self._write(a)
        self.tp._publish(a)
        self._write(b)
        self.tp._publish(b)
        self.assertEqual(len(self._bins()), 2)

        self.tp._retire_current()
        self.assertEqual(self._bins(), [], "a window survived teardown")

    def test_clear_tells_the_renderer_before_removing_the_file(self):
        """overlay-remove has to land before the bytes behind it go away."""
        a = self.tp._next_file()
        self._write(a)
        self.tp._publish(a)
        self.tp.clear()
        self.assertIn(("shim-trickplay-clear",), self.player.messages)
        self.assertFalse(os.path.exists(a))

    def test_a_second_worker_never_reuses_the_first_worker_path(self):
        """A new TrickPlay is built on every mpv re-creation. With the
        counter on the instance it restarted at 0, so the second worker's
        first file was the first worker's first file -- and stop(join=False)
        lets a straggler publish into the NEW mpv, whose renderer mmaps
        whatever it is handed. open("wb") over that is a SIGBUS in mpv."""
        first = [self.tp._next_file() for _ in range(3)]
        second = trickplay.TrickPlay.__new__(trickplay.TrickPlay)
        second._file_lock = threading.Lock()
        second._current = None
        again = [second._next_file() for _ in range(3)]
        self.assertFalse(set(first) & set(again),
                         "the second worker reused %r"
                         % sorted(set(first) & set(again)))

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

    def _tile(self, w, h, cells=None):
        """A mosaic whose every cell is filled with its own index.

        A solid colour would let `skip` be ignored and still pass: the
        assertions would be counting bytes, which is not the field these
        tests are named after. `cells` is (tile_width, tile_height); the
        red channel of a cell carries its position in the tile.
        """
        from PIL import Image

        img = Image.new("RGBA", (w, h), (0, 0, 0, 255))
        if cells:
            tw, th = cells
            cw, ch = w // tw, h // th
            for i in range(tw * th):
                y, x = divmod(i, tw)
                img.paste((i + 1, 2, 3, 255),
                          (x * cw, y * ch, (x + 1) * cw, (y + 1) * ch))
        else:
            img.paste((1, 2, 3, 255), (0, 0, w, h))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    @staticmethod
    def _first_bytes(data, frame_bytes):
        """The red channel of each written frame's first pixel (BGRA)."""
        return [data[i * frame_bytes + 2]
                for i in range(len(data) // frame_bytes)]

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

    def test_skip_drops_frames_off_the_front(self):
        """A window rarely starts on a tile boundary, and the frames before
        it are the ones not worth keeping -- decoded, a tile is tens of
        megabytes.

        Asserts WHICH frames landed, not how many: a count alone passes
        with `skip` ignored, since the same number of frames comes out
        either way.
        """
        from jellyfin_mpv_shim import bifdecode

        fh = io.BytesIO()
        written = bifdecode.decompress_tiles(
            4, 4, 2, 2, 2, [self._tile(8, 8, cells=(2, 2))], fh, skip=1)
        self.assertEqual(written, 2)
        self.assertEqual(self._first_bytes(fh.getvalue(), 4 * 4 * 4), [2, 3],
                         "the file starts at the wrong frame")

    def test_skip_spans_a_tile_boundary(self):
        """`skip` counts frames of the STREAM, not of the first tile: a
        window can start in the second tile it was handed."""
        from jellyfin_mpv_shim import bifdecode

        tile = self._tile(8, 8, cells=(2, 2))
        fh = io.BytesIO()
        written = bifdecode.decompress_tiles(
            4, 4, 2, 2, 3, [tile] * 2, fh, skip=5)
        self.assertEqual(written, 3)
        # Stream frames 5,6,7 -> cells 2,3,4 of the second tile.
        self.assertEqual(self._first_bytes(fh.getvalue(), 4 * 4 * 4),
                         [2, 3, 4],
                         "skip did not carry across the tile boundary")

    def test_a_tile_past_the_count_is_never_FETCHED(self):
        """`tiles` is a generator that downloads one mosaic per step, so the
        `count` bound has to be checked before the next one is pulled -- a
        plain `for` loop asks the server for a tile to discover it has
        already written every frame it was asked for."""
        from jellyfin_mpv_shim import bifdecode

        seen = []

        def tiles():
            for i in range(4):
                seen.append(i)
                yield self._tile(8, 8)

        bifdecode.decompress_tiles(4, 4, 2, 2, 4, tiles(), io.BytesIO())
        self.assertEqual(seen, [0], "decoded tiles the window does not need")


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
        seen, versions, seen_lock = [], [], threading.Lock()
        start = threading.Event()

        def store_many():
            start.wait(5)
            for _ in range(50):
                src, _w, _h, v = store._store(img)
                with seen_lock:
                    seen.append(src)
                    versions.append(v)

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
        self.assertEqual(len(versions), len(set(versions)),
                         "two strips share one content version — on the "
                         "libmpv path (recycled addresses) that makes them "
                         "indistinguishable to the renderer's overlay cache")


class _WorkerVideo:
    """A video with no server-side trickplay, so the worker takes the chapter
    path — the one with the guard that used to kill the thread."""

    def __init__(self, name, on_images=None):
        self.name = name
        self._on_images = on_images

    def get_bif(self, *_a, **_kw):
        return None

    def get_chapters(self):
        return [{"start": 0}, {"start": 10}]

    def get_chapter_images(self, *_a, **_kw):
        # A generator in the real one, so the window this guard covers spans
        # the whole download — which is why a skip lands here so easily.
        if self._on_images is not None:
            self._on_images()
        return iter(())


class _WorkerPlayer:
    def __init__(self, video):
        self.video = video
        self.messages = []
        self.lock = threading.Lock()

    def has_video(self):
        return self.video is not None

    def get_video(self):
        return self.video

    def script_message(self, *args):
        with self.lock:
            self.messages.append(args)


class _BifVideo:
    """A video whose server HAS trickplay, modelled down to the tile shape.

    The manifest fields are the ones the worker reads and the tile mosaics
    are real images of the size the manifest promises, because the whole
    question here is which tiles were asked for and which frames of them
    reached the file -- a stand-in that answered with bytes would make every
    assertion below a proxy for itself. Each frame is filled with its own
    GLOBAL index, so the file says what it holds.
    """

    def __init__(self, width=4, height=4, tile_w=2, tile_h=2, count=16,
                 interval=10000):
        self.width, self.height = width, height
        self.tile_w, self.tile_h = tile_w, tile_h
        self.count, self.interval = count, interval
        #: (start, count) of every tile request, in order.
        self.requested = []

    def get_bif(self, *_a, **_kw):
        return {"Width": self.width, "Height": self.height,
                "TileWidth": self.tile_w, "TileHeight": self.tile_h,
                "ThumbnailCount": self.count, "Interval": self.interval}

    def get_chapters(self):
        return []

    def get_chapter_images(self, *_a, **_kw):
        return iter(())

    def _tile(self, index):
        from PIL import Image

        per = self.tile_w * self.tile_h
        img = Image.new("RGBA", (self.width * self.tile_w,
                                 self.height * self.tile_h))
        for cell in range(per):
            y, x = divmod(cell, self.tile_w)
            frame = index * per + cell
            img.paste((frame % 256, 0, 0, 255),
                      (x * self.width, y * self.height,
                       (x + 1) * self.width, (y + 1) * self.height))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def get_hls_tile_images(self, width, count, start=0):
        self.requested.append((start, count))
        for i in range(start, start + count):
            yield self._tile(i)


class WindowedTrickplayTest(unittest.TestCase):
    """Only the frames around the seek position reach disk.

    A decoded frame is `width * height * 4` bytes, so a two-hour film at the
    server's default thumbnail width is ~130 MB of them and one at 640px is
    around 500 MB, all for one preview bubble. The worker fetches the tiles
    the window falls in and writes only the window's frames out of them.

    (Not "and mpv mmaps it, so that is resident memory": mpv reads out the
    one rectangle it needs on any build since 3cd66d2fd7. The file is disk
    and the decode is RAM -- see docs/artwork-pipeline.md section 11.)
    """

    #: 4x4 frames -> 64 bytes each, tiles of 2x2 -> 4 frames per tile.
    FRAME_BYTES = 4 * 4 * 4

    def setUp(self):
        try:
            import PIL  # noqa: F401
        except ImportError:
            self.skipTest("Pillow not available")
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        orig = trickplay._img_path
        trickplay._img_path = lambda seq: os.path.join(
            self.dir.name, "raw_images.%d.bin" % seq)
        self.addCleanup(lambda: setattr(trickplay, "_img_path", orig))
        # Five frames' worth, so a 16-frame video has to be windowed.
        orig_budget = trickplay.WINDOW_BUDGET_BYTES
        trickplay.WINDOW_BUDGET_BYTES = 5 * self.FRAME_BYTES
        self.addCleanup(lambda: setattr(trickplay, "WINDOW_BUDGET_BYTES",
                                        orig_budget))
        orig_fast = trickplay.settings.trickplay_fast_mode
        trickplay.settings.trickplay_fast_mode = False
        self.addCleanup(lambda: setattr(trickplay.settings,
                                        "trickplay_fast_mode", orig_fast))

    def _worker(self, video):
        player = _WorkerPlayer(video)
        tp = trickplay.TrickPlay(player)
        self.addCleanup(lambda: tp.stop(join=False))
        tp.start()
        return tp, player

    def _fetch(self, tp, player, position, timeout=5.0):
        """Ask for a window and wait for the message it publishes, if any."""
        before = len(player.messages)
        tp.request_at(position)
        deadline = time.monotonic() + timeout
        while len(player.messages) == before and time.monotonic() < deadline:
            time.sleep(0.005)
        with player.lock:
            if len(player.messages) == before:
                return None
            return player.messages[-1]

    def _frames(self, path):
        """The global frame index written into each frame of the file.

        Frames are BGRA, so the red channel this fixture fills with the frame
        number is the third byte of the first pixel.
        """
        with open(path, "rb") as fh:
            data = fh.read()
        self.assertEqual(len(data) % self.FRAME_BYTES, 0,
                         "the file is not a whole number of frames")
        return [data[i * self.FRAME_BYTES + 2]
                for i in range(len(data) // self.FRAME_BYTES)]

    def test_only_the_window_is_written_and_it_says_where_it_starts(self):
        video = _BifVideo()
        tp, player = self._worker(video)
        # 90s at a 10s cadence is frame 9; a five-frame budget centres on it.
        msg = self._fetch(tp, player, 90)
        self.assertIsNotNone(msg, "the window was never published")
        kind, count, mult, w, h, path, first, total = msg
        self.assertEqual(kind, "shim-trickplay-bif")
        self.assertEqual((int(count), int(first), int(total)), (5, 7, 16))
        self.assertEqual(self._frames(path), [7, 8, 9, 10, 11],
                         "the file does not hold the frames it claims")

    def test_the_tiles_fetched_are_the_ones_the_window_falls_in(self):
        """Tiles are the download unit and do not divide the window: frames
        7..11 live in tiles 1 and 2, and tile 1 is fetched whole for the one
        frame of it that is wanted."""
        video = _BifVideo()
        tp, player = self._worker(video)
        self._fetch(tp, player, 90)
        self.assertEqual(video.requested, [(1, 2)],
                         "the whole video was downloaded, or the wrong part")

    def test_the_window_boundary_is_half_open(self):
        """`first <= frame < first + asked`, both ends.

        A `<=` on the upper bound claims a frame the file does not hold, so
        the consumer draws nothing there and never gets a window that would
        fix it. Asserted directly, because every other test in this class
        works comfortably inside the window and none of them moves if the
        comparison slips.
        """
        video = _BifVideo()
        tp, _player = self._worker(video)
        tp._window = (video, 7, 5, 16)          # frames 7..11
        self.assertFalse(tp._covers(video, 6), "reached below the window")
        self.assertTrue(tp._covers(video, 7))
        self.assertTrue(tp._covers(video, 11))
        self.assertFalse(tp._covers(video, 12), "claimed a frame past the end")
        self.assertFalse(tp._covers(object(), 9),
                         "another video's window counted as this one's")

    def test_a_position_already_loaded_asks_the_server_for_nothing(self):
        video = _BifVideo()
        tp, player = self._worker(video)
        self._fetch(tp, player, 90)                 # frames 7..11
        self.assertEqual(len(video.requested), 1)
        # 100s is frame 10, inside the window. Nothing to fetch and nothing
        # to publish -- the renderer is already pointed at these bytes.
        self.assertIsNone(self._fetch(tp, player, 100, timeout=0.5),
                          "a covered position published a new file")
        self.assertEqual(video.requested, [(1, 2)],
                         "a covered position went back to the server")

    def test_moving_out_of_the_window_loads_the_new_part(self):
        video = _BifVideo()
        tp, player = self._worker(video)
        self._fetch(tp, player, 90)
        msg = self._fetch(tp, player, 10)           # frame 1, well before it
        self.assertIsNotNone(msg, "scrubbing out of the window loaded nothing")
        self.assertEqual(int(msg[6]), 0, "the window did not move")
        self.assertEqual(self._frames(msg[5]), [0, 1, 2, 3, 4])

    def test_the_window_is_clamped_to_the_video(self):
        """Centring alone would run off both ends -- and a first frame below
        zero is a negative offset into the tile stream."""
        video = _BifVideo()
        tp, player = self._worker(video)
        self.assertEqual(int(self._fetch(tp, player, 0)[6]), 0)
        # 150s is frame 15, the last one; the window ends there rather than
        # promising frames 13..17 of a 16-frame video.
        msg = self._fetch(tp, player, 150)
        self.assertEqual((int(msg[6]), int(msg[1])), (11, 5))
        self.assertEqual(self._frames(msg[5]), [11, 12, 13, 14, 15])

    def test_a_video_that_fits_is_loaded_whole(self):
        """The budget is a ceiling, not a quota: an ordinary episode never
        pays for windowing, and its previews are all instant."""
        video = _BifVideo(count=4)
        tp, player = self._worker(video)
        msg = self._fetch(tp, player, 20)
        self.assertEqual((int(msg[1]), int(msg[6]), int(msg[7])), (4, 0, 4))
        self.assertEqual(video.requested, [(0, 1)])

    def test_fast_mode_loads_the_whole_video(self):
        """**[iw]**: "unless the user requests the trickplay fast mode where
        it just hands the entire file to mpv where it can seek faster"."""
        trickplay.settings.trickplay_fast_mode = True
        video = _BifVideo()
        tp, player = self._worker(video)
        msg = self._fetch(tp, player, 90)
        self.assertEqual((int(msg[1]), int(msg[6]), int(msg[7])), (16, 0, 16))
        self.assertEqual(video.requested, [(0, 4)],
                         "fast mode did not fetch every tile")
        self.assertEqual(self._frames(msg[5]), list(range(16)))

    def test_a_short_tile_run_is_not_asked_for_again(self):
        """The loop this closes: the frames a short run did not deliver sit
        inside the window every request for them produces, so treating them
        as "not covered" re-fetches and re-publishes -- which clears the
        consumer's one-ask-per-window marker, so it asks again on the next
        rendered frame, at render cadence, forever.

        Three requests, not one: a single-step test cannot see a loop.
        """
        video = _BifVideo()
        # The last tile 404s, so frames 12..15 never arrive.
        real = video.get_hls_tile_images

        def short(width, count, start=0):
            for i, tile in enumerate(real(width, count, start=start)):
                if start + i >= 3:
                    return
                yield tile

        video.get_hls_tile_images = short

        tp, player = self._worker(video)
        # 150s is frame 15 -- inside the window [11, 16), missing from it.
        msg = self._fetch(tp, player, 150)
        self.assertIsNotNone(msg, "nothing was published at all")
        self.assertEqual(int(msg[1]), 1, "the short run over-reported")
        self.assertEqual(self._frames(msg[5]), [11])
        fetches = len(video.requested)

        for attempt in range(3):
            self.assertIsNone(
                self._fetch(tp, player, 150, timeout=0.5),
                "the short window was re-published on attempt %d -- that is "
                "the loop" % attempt)
        self.assertEqual(len(video.requested), fetches,
                         "the shortfall was re-fetched: %r" % video.requested)

    def test_a_failed_window_does_not_downgrade_the_item_to_chapters(self):
        """A dropped tile mid-film used to be a one-way door.

        The bif block's ``except`` fell through to the chapter images, which
        publish ``shim-trickplay-chapters`` -- and on that branch NEITHER
        consumer can ask for a window again (renderer.lua takes ``tp.times``;
        thumbfast.lua gates the whole window block on ``img_is_bif``). So one
        504 on one tile meant chapter stills for the rest of the item, with
        Python still perfectly able to serve windows that nothing would ever
        request. Before windowing this path could only run at playback start,
        where falling back to chapters IS the design.
        """
        video = _BifVideo()
        video.get_chapters = lambda: [{"start": 0}, {"start": 60}]
        real = video.get_hls_tile_images
        boom = []

        def flaky(width, count, start=0):
            if boom:
                raise OSError("504 on a tile")
            for tile in real(width, count, start=start):
                yield tile

        video.get_hls_tile_images = flaky

        tp, player = self._worker(video)
        self.assertEqual(self._fetch(tp, player, 20)[0], "shim-trickplay-bif")
        live = tp._current

        boom.append(True)
        self.assertIsNone(self._fetch(tp, player, 150, timeout=0.7),
                          "the failed window published something")
        self.assertNotIn(
            "shim-trickplay-chapters",
            [m[0] for m in player.messages],
            "a failed window downgraded the item to chapter previews")
        self.assertEqual(tp._current, live,
                         "the live window was replaced by the failure")
        self.assertEqual(
            [f for f in os.listdir(self.dir.name) if f.endswith(".bin")],
            [os.path.basename(live)],
            "the half-written window was left on disk")

        # And it recovers: the next request after the server comes back is
        # served normally. Three, because the failure this pins is permanent.
        boom.clear()
        for attempt in range(3):
            msg = self._fetch(tp, player, 150)
            self.assertIsNotNone(msg, "no window after failure #%d" % attempt)
            self.assertEqual(msg[0], "shim-trickplay-bif")
            tp._window = None      # force the next pass to re-fetch

    def test_the_first_fetch_lands_on_the_resume_position(self):
        """`fetch_thumbnails` takes where playback actually starts. On a
        resumed item that is not zero, and it is the first place anybody
        scrubs."""
        video = _BifVideo()
        tp, player = self._worker(video)
        before = len(player.messages)
        tp.fetch_thumbnails(90)
        deadline = time.monotonic() + 5
        while len(player.messages) == before and time.monotonic() < deadline:
            time.sleep(0.005)
        self.assertEqual(int(player.messages[-1][6]), 7,
                         "the first window ignored the resume position")


class WindowMathTest(unittest.TestCase):
    """`_window_for` on its own, at the sizes a real server produces."""

    def setUp(self):
        orig = trickplay.settings.trickplay_fast_mode
        trickplay.settings.trickplay_fast_mode = False
        self.addCleanup(lambda: setattr(trickplay.settings,
                                        "trickplay_fast_mode", orig))

    #: Measured off a real library: a 128-minute film at the server's
    #: default trickplay width. 770 frames * 320 * 134 * 4 = 132 MB.
    REAL = {"Width": 320, "Height": 134, "TileWidth": 10, "TileHeight": 10,
            "ThumbnailCount": 770, "Interval": 10000}

    def test_the_window_shrinks_as_the_thumbnails_grow(self):
        """The budget is bytes, not minutes, and that is the point: what is
        scarce is the memory mpv maps, so a library configured for 640px
        previews gets a shorter window rather than a bigger file."""
        small = dict(self.REAL)
        big = dict(self.REAL, Width=640, Height=268)
        _f1, c1 = trickplay.TrickPlay._window_for(small, 3600)
        _f2, c2 = trickplay.TrickPlay._window_for(big, 3600)
        self.assertLess(c2, c1)
        for data, count in ((small, c1), (big, c2)):
            self.assertLessEqual(
                count * data["Width"] * data["Height"] * 4,
                trickplay.WINDOW_BUDGET_BYTES,
                "the window is over budget")

    def test_a_degenerate_manifest_does_not_divide_by_zero(self):
        self.assertEqual(
            trickplay.TrickPlay._window_for(
                dict(self.REAL, ThumbnailCount=0), 10), (0, 0))
        self.assertEqual(
            trickplay.TrickPlay._window_for(
                dict(self.REAL, Width=0), 10)[1], 770)


class DeferredUnlinkTest(unittest.TestCase):
    """Windows refuses to unlink a file mpv still has mapped. Windowing turns
    that from a once-per-video event into a once-per-scrub one, so a refused
    removal is remembered and retried instead of being left for the next
    run's cleanup."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        with trickplay._pending_lock:
            trickplay._pending_unlink[:] = []
        self.addCleanup(lambda: trickplay._pending_unlink.__setitem__(
            slice(None), []))

    def test_a_refused_removal_is_retried_on_the_next_publish(self):
        path = os.path.join(self.dir.name, "raw_images.1.bin")
        with open(path, "wb") as fh:
            fh.write(b"x")

        real_remove = os.remove
        refuse = [True]

        def maybe_remove(p):
            if refuse[0] and p == path:
                raise OSError(32, "in use")
            real_remove(p)

        os.remove = maybe_remove
        self.addCleanup(lambda: setattr(os, "remove", real_remove))

        trickplay._unlink(path)
        self.assertTrue(os.path.isfile(path), "the fixture did not refuse")
        self.assertIn(path, trickplay._pending_unlink)

        refuse[0] = False
        trickplay._sweep_unlinks()
        self.assertFalse(os.path.isfile(path),
                         "the refused removal was never retried")
        self.assertEqual(trickplay._pending_unlink, [])

    def test_a_removal_that_keeps_failing_is_kept_not_dropped(self):
        path = os.path.join(self.dir.name, "raw_images.2.bin")
        with open(path, "wb") as fh:
            fh.write(b"x")
        real_remove = os.remove
        os.remove = lambda p: (_ for _ in ()).throw(OSError(32, "in use"))
        self.addCleanup(lambda: setattr(os, "remove", real_remove))
        trickplay._unlink(path)
        for _ in range(3):
            trickplay._sweep_unlinks()
        self.assertEqual(trickplay._pending_unlink, [path],
                         "a still-mapped file was forgotten, or duplicated")


class WorkerSurvivesAVideoChangeTest(unittest.TestCase):
    """`run()` leaves its loop only when `halt` is set.

    Every guard in it cancels *this fetch* — the video moved on, the frames
    are for the wrong item. One of them used to `break`, which ends the
    worker: the thread is created once per mpv instance, nothing checks
    is_alive() or restarts it, and fetch_thumbnails() is a bare trigger.set(),
    so scrub previews stopped for the life of that mpv with no log line.
    """

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        orig = trickplay._img_path
        trickplay._img_path = lambda seq: os.path.join(
            self.dir.name, "raw_images.%d.bin" % seq)
        self.addCleanup(lambda: setattr(trickplay, "_img_path", orig))
        orig_decompress = trickplay.bifdecode.decompress_bif
        trickplay.bifdecode.decompress_bif = lambda images, fh: (
            list(images), fh.write(b"x" * 8), {"width": 8, "height": 8})[-1]
        self.addCleanup(lambda: setattr(trickplay.bifdecode, "decompress_bif",
                                        orig_decompress))

    def _worker(self, player):
        tp = trickplay.TrickPlay(player)
        self.addCleanup(lambda: tp.stop(join=False))
        tp.start()
        return tp

    def _fetch(self, tp, player, timeout=5.0):
        """Trigger one pass and wait for its message, if any."""
        before = len(player.messages)
        tp.fetch_thumbnails()
        deadline = time.monotonic() + timeout
        while len(player.messages) == before and time.monotonic() < deadline:
            time.sleep(0.005)
        return len(player.messages) > before

    def test_a_video_change_mid_fetch_cancels_the_fetch_not_the_worker(self):
        player = _WorkerPlayer(None)
        # The swap happens *inside* the fetch, which is the whole point: a
        # stand-in that answers the same video throughout cannot fail this.
        first = _WorkerVideo("first",
                             on_images=lambda: setattr(player, "video",
                                                       _WorkerVideo("second")))
        player.video = first
        tp = self._worker(player)

        self.assertFalse(self._fetch(tp, player, timeout=1.0),
                         "the skipped fetch published anyway")
        self.assertTrue(tp.is_alive(), "the worker died on a video change")

        # And it still works afterwards — three more times, because the
        # failure this pins is permanent, not a one-off stumble.
        player.video = _WorkerVideo("stable")
        for attempt in range(3):
            self.assertTrue(self._fetch(tp, player),
                            "no thumbnails after skip #%d" % attempt)
            self.assertEqual(player.messages[-1][0], "shim-trickplay-chapters")

    def test_halt_is_still_the_way_out(self):
        player = _WorkerPlayer(_WorkerVideo("only"))
        tp = self._worker(player)
        self.assertTrue(self._fetch(tp, player))
        tp.stop()
        self.assertFalse(tp.is_alive(), "stop() no longer stops the worker")


if __name__ == "__main__":
    unittest.main()
