"""The thumbnail store's byte-bounded in-memory LRU (MemoryCache).

The cache has no UI dependency, so its eviction policy is exercisable
without a display. An explicit sizer gives entries known byte sizes; the
real store sizes decoded images as width*height*4.

(Ported from the Tk browser's identical cache when that browser was
removed. The eviction policy is the same and so are these tests.)
"""
import os
import unittest

from jellyfin_mpv_shim.mpvtk_browser.thumbnails import MemoryCache


def sizer(value):
    # Test values carry their own byte size.
    return value


class MemoryCacheTest(unittest.TestCase):
    def test_get_miss_returns_none(self):
        c = MemoryCache(100, sizer)
        self.assertIsNone(c.get("absent"))

    def test_put_get_roundtrip_and_bytes(self):
        c = MemoryCache(100, sizer)
        c.put("a", 30)
        self.assertEqual(c.get("a"), 30)
        self.assertEqual(c.nbytes, 30)
        self.assertEqual(len(c), 1)

    def test_evicts_by_byte_budget_not_count(self):
        # Budget 100 bytes: three 40-byte entries can't all fit even though the
        # count is small — the oldest is evicted.
        c = MemoryCache(100, sizer)
        c.put("a", 40)
        c.put("b", 40)
        c.put("c", 40)  # 120 > 100 -> evict "a"
        self.assertIsNone(c.get("a"))
        self.assertEqual(c.get("b"), 40)
        self.assertEqual(c.get("c"), 40)
        self.assertEqual(c.nbytes, 80)
        self.assertEqual(len(c), 2)

    def test_lru_order_uses_recency(self):
        c = MemoryCache(100, sizer)
        c.put("a", 40)
        c.put("b", 40)
        c.get("a")       # touch "a" so "b" becomes least-recently-used
        c.put("c", 40)   # 120 > 100 -> evict "b" (LRU), not "a"
        self.assertEqual(c.get("a"), 40)
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.get("c"), 40)

    def test_reinsert_updates_size_without_double_counting(self):
        c = MemoryCache(1000, sizer)
        c.put("a", 40)
        c.put("a", 70)  # replace, not add
        self.assertEqual(c.get("a"), 70)
        self.assertEqual(c.nbytes, 70)
        self.assertEqual(len(c), 1)

    def test_oversized_single_entry_survives(self):
        # A single entry larger than the whole budget must not be evicted the
        # moment it lands (its caller still holds/needs it).
        c = MemoryCache(10, sizer)
        c.put("big", 500)
        self.assertEqual(c.get("big"), 500)
        self.assertEqual(len(c), 1)
        # A subsequent entry evicts the oversized one back down toward budget.
        c.put("small", 5)
        self.assertIsNone(c.get("big"))
        self.assertEqual(c.get("small"), 5)


class StoreMemoryBoundWiringTest(unittest.TestCase):
    """Regression: library_image_cache_mb was only applied to the DISK cache;
    the in-memory decoded-image budget silently stayed at the hardcoded
    default, so a long browse session ballooned RAM no matter what the user
    configured. The store must honour max_mem_mb, and the sizer must count
    the real resident cost of a decoded image."""

    def _store(self, **kw):
        import tempfile
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import ThumbnailStore

        tmp = tempfile.mkdtemp(prefix="jms-thumbtest-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        store = ThumbnailStore(tmp, **kw)
        self.addCleanup(store.shutdown)
        return store

    def test_max_mem_mb_reaches_the_memory_cache(self):
        store = self._store(max_mem_mb=7)
        self.assertEqual(store._mem._max_bytes, 7 * 1024 * 1024)

    def test_the_sizer_counts_the_real_decoded_size(self):
        """A sizer that under-reports makes max_mem_mb a fiction. The Tk
        store counted ~8 bytes/px (PhotoImage master + display copy); this
        one holds PIL images, so it is width * height * bands."""
        from PIL import Image as PILImage
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import _image_bytes

        self.assertEqual(_image_bytes(PILImage.new("RGBA", (10, 20))),
                         10 * 20 * 4)
        self.assertEqual(_image_bytes(PILImage.new("RGB", (10, 20))),
                         10 * 20 * 3)

    def test_an_unsizable_object_is_zero_rather_than_a_crash(self):
        """Eviction runs on the loop thread; raising there would take the
        UI down over a cache accounting detail."""
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import _image_bytes

        self.assertEqual(_image_bytes(object()), 0)



class DiskCacheBoundTest(unittest.TestCase):
    """The disk half, which had no bound at all in practice.

    ``_prune_disk`` was called exactly once, from ``__init__``, against a
    directory that had just been created -- so it measured an empty dir and
    the cache then grew for the rest of the session. On a RAM-backed cache
    dir (the default: see rawimage.cache_dir) that is a browse session
    filling the tmpfs the whole login session shares.
    """

    def _store(self, **kw):
        import shutil
        import tempfile
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import ThumbnailStore

        tmp = tempfile.mkdtemp(prefix="jms-thumbtest-")
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        store = ThumbnailStore(tmp, **kw)
        self.addCleanup(store.shutdown)
        return store

    def _fill(self, store, count, size, start=0):
        import os
        import time

        now = time.time()
        for i in range(start, start + count):
            path = os.path.join(store.cache_dir, "%04d.img" % i)
            with open(path, "wb") as fh:
                fh.write(b"\0" * size)
            # Distinct mtimes, oldest first: the prune is an LRU over them
            # and same-second files would evict in listdir order. Recent
            # ones, or the age reaper takes the lot before the size bound
            # has anything to say.
            stamp = now - (start + count - i)
            os.utime(path, (stamp, stamp))
        return now

    def _held(self, store):
        import os

        return sum(os.path.getsize(os.path.join(store.cache_dir, n))
                   for n in os.listdir(store.cache_dir))

    def test_writing_past_the_budget_prunes_the_oldest(self):
        import os

        store = self._store(max_disk_mb=1)
        store.MIN_DISK_BYTES = 0
        self._fill(store, 40, 64 * 1024)          # 2.5 MiB in a 1 MiB cache
        store._prune_disk()
        self.assertLessEqual(self._held(store), 1024 * 1024)
        kept = sorted(os.listdir(store.cache_dir))
        self.assertNotIn("0000.img", kept, "the oldest entry survived")
        self.assertIn("0039.img", kept, "the newest entry was evicted")

    def test_a_prune_is_due_once_enough_has_been_written(self):
        store = self._store(max_disk_mb=1)
        store.MIN_DISK_BYTES = 0
        self._fill(store, 40, 64 * 1024)
        store._note_written(store.PRUNE_EVERY // 2)
        self.assertGreater(self._held(store), 1024 * 1024,
                           "pruned before enough had been written")
        store._note_written(store.PRUNE_EVERY)
        self.assertLessEqual(self._held(store), 1024 * 1024,
                             "the write budget never triggered a prune")

    def _with_free(self, store, free):
        """Answer ``free`` bytes for the cache's filesystem."""
        import shutil

        real = shutil.disk_usage
        self.addCleanup(setattr, shutil, "disk_usage", real)
        shutil.disk_usage = lambda path: type("U", (), {"free": free})

    def test_a_roomy_disk_gets_the_whole_budget(self):
        store = self._store(max_disk_mb=1024)
        self._with_free(store, 500 * 1024 ** 3)
        self.assertEqual(store._disk_budget(0), 1024 * 1024 * 1024)

    def test_the_budget_gives_way_to_free_space(self):
        # A budget the filesystem cannot honour is not a budget. Five per
        # cent of what is left, so it shrinks continuously rather than
        # waiting for a "low disk" threshold to trip.
        store = self._store(max_disk_mb=1024)
        self._with_free(store, 2 * 1024 ** 3)
        self.assertEqual(store._disk_budget(0),
                         int(2 * 1024 ** 3 * 0.05))

    def test_what_the_cache_already_holds_counts_as_available(self):
        # Otherwise the cache ratchets its own allowance down every time it
        # grows: each entry written is free space spent, so the share
        # shrinks, so it prunes what it just wrote.
        store = self._store(max_disk_mb=1024)
        self._with_free(store, 1024 ** 3)
        self.assertGreater(store._disk_budget(512 * 1024 ** 2),
                           store._disk_budget(0))

    def test_the_budget_actually_prunes_on_a_tight_disk(self):
        # 2.5 MiB held with 1 MiB left: the cache gives most of it back
        # rather than sitting on it. (The quarter-of-available floor is what
        # binds down here, not the 5% share -- below it the cache would be
        # too small to be a cache, and that is its own kind of waste.)
        store = self._store(max_disk_mb=4096)
        self._fill(store, 40, 64 * 1024)
        self._with_free(store, 1024 * 1024)
        store._prune_disk()
        self.assertLessEqual(self._held(store), 1024 * 1024)

    def test_the_floor_holds_where_there_is_room_for_it(self):
        # A cache too small to hold a screenful re-fetches every tile on
        # every scroll, which costs more than the space it saves.
        store = self._store(max_disk_mb=4096)
        self._with_free(store, 400 * 1024 * 1024)   # 5% = 20 MiB
        self.assertEqual(store._disk_budget(0), store.MIN_DISK_BYTES)

    def test_the_floor_gives_way_on_a_nearly_full_disk(self):
        # ...but holding 24 MiB of posters where 40 MiB is all that is left
        # is the app being the problem.
        store = self._store(max_disk_mb=4096)
        self._with_free(store, 40 * 1024 * 1024)
        self.assertEqual(store._disk_budget(0), 10 * 1024 * 1024)

    def test_artwork_nobody_has_asked_for_in_a_month_is_reaped(self):
        """The size bound alone lets a persistent cache sit at its ceiling
        forever, full of entries nothing can reach: the key folds in the
        image tag and the pixel size, so changing the Cover Size or the
        theme's tile shape orphans every entry made for the old one rather
        than replacing it. An orphan is only recognisable by nobody having
        read it, so age is the only reaper there is."""
        import os
        import time

        store = self._store(max_disk_mb=4096)
        self._fill(store, 4, 1024)
        stale = os.path.join(store.cache_dir, "ancient.img")
        with open(stale, "wb") as fh:
            fh.write(b"\0" * 1024)
        old = time.time() - store.MAX_AGE_SECS - 60
        os.utime(stale, (old, old))
        store._prune_disk()
        self.assertFalse(os.path.exists(stale))
        self.assertEqual(len(os.listdir(store.cache_dir)), 4,
                         "the reaper took entries that were still in use")

    def test_a_read_keeps_an_entry_alive(self):
        # _load_remote touches on a hit, which is what makes the age bound
        # "unused for a month" rather than "fetched a month ago".
        import os
        import time

        store = self._store()
        path = os.path.join(store.cache_dir, "warm.img")
        with open(path, "wb") as fh:
            fh.write(b"\0" * 16)
        os.utime(path, (time.time() - store.MAX_AGE_SECS - 60,) * 2)
        os.utime(path, None)                     # ...and then it is read
        store._prune_disk()
        self.assertTrue(os.path.exists(path))


class DiskCacheLocationTest(unittest.TestCase):
    """Artwork is long-lived, so it does not belong in scratch space.

    Every entry is keyed by the server's own image tag: last week's poster
    is still this week's poster, and it was being thrown away on exit and
    re-fetched, a whole library at a time, on every launch.
    """

    def test_it_prefers_somewhere_that_survives_a_restart(self):
        import shutil
        import tempfile
        from jellyfin_mpv_shim.mpvtk_browser import thumbnails

        home = tempfile.mkdtemp(prefix="jms-cachehome-")
        self.addCleanup(shutil.rmtree, home, ignore_errors=True)
        old = os.environ.get("XDG_CACHE_HOME")
        os.environ["XDG_CACHE_HOME"] = home
        self.addCleanup(lambda: os.environ.__setitem__("XDG_CACHE_HOME", old)
                        if old is not None
                        else os.environ.pop("XDG_CACHE_HOME", None))
        path, budget = thumbnails.disk_cache("jms-test-app")
        self.assertTrue(path.startswith(home + os.sep), path)
        self.assertEqual(budget, thumbnails.DEFAULT_DISK_MB)

    def test_nowhere_persistent_falls_back_to_a_smaller_scratch_cache(self):
        # A read-only home, a sandbox, an unknown platform. The cache then
        # lands in RAM and dies with the session, so it gets less room.
        from unittest import mock
        from jellyfin_mpv_shim.mpvtk_browser import thumbnails

        with mock.patch("jellyfin_mpv_shim.conffile.get_cache_dir",
                        return_value=None):
            path, budget = thumbnails.disk_cache("jms-test-app")
        self.addCleanup(__import__("shutil").rmtree, path, ignore_errors=True)
        self.assertEqual(budget, thumbnails.SCRATCH_DISK_MB)
        self.assertLess(budget, thumbnails.DEFAULT_DISK_MB)




class RouteChangeTrimTest(unittest.TestCase):
    """Decoded artwork is dropped when the screen changes.

    It is the most expensive thing the app holds per picture and it has
    exactly one job -- compositing tile strips -- which is finished the
    moment the row is composited. Holding it to a 96 MiB ceiling instead
    means holding it until something else wants the room.

    Driven off the async epoch rather than off navigate(), because
    navigate() is reachable from mpv's event thread and from the websocket
    while this cache is loop-thread-only. See
    MpvtkBrowser._shed_caches_on_screen_change.
    """

    class _Thumbs:
        def __init__(self):
            self.trims = 0

        def trim_memory(self, max_bytes=None):
            self.trims += 1

    class _Strips:
        def __init__(self):
            self.armed = 0
            self.pressure = None

        def trim_soon(self):
            self.armed += 1

        def set_memory_pressure(self, tight):
            self.pressure = tight

    def _browser(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import FakeController, FakeSource

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b.thumbs = self._Thumbs()
        return b

    def test_a_screen_change_trims_once(self):
        b = self._browser()
        b._shed_caches_on_screen_change()
        self.assertEqual(b.thumbs.trims, 0,
                         "shed before a screen had been left at all")
        b._screen_seq += 1                    # a navigation
        b._shed_caches_on_screen_change()
        self.assertEqual(b.thumbs.trims, 1)
        b._screen_seq += 1
        b._shed_caches_on_screen_change()
        self.assertEqual(b.thumbs.trims, 2)

    def test_an_in_place_reload_is_not_a_screen_change(self):
        """The first version keyed off the async epoch, which means "cancel
        what is in flight" -- and four things bump it without leaving the
        screen: a sort or filter change, the collections toggle, a retry
        after a failure, and a server switch that keeps its place. Shedding
        on those cut the cache for the page still on screen, so toggling a
        sort re-decoded the visible screenful for nothing."""
        b = self._browser()
        b._bump_epoch()                      # a reload, not a navigation
        b._shed_caches_on_screen_change()
        self.assertEqual(b.thumbs.trims, 0,
                         "shed the artwork of the screen still on show")

    def test_frames_on_one_screen_do_not_trim(self):
        # This runs on every frame. A repaint is not a navigation, and a
        # Live TV screen that re-reads itself deliberately does not bump the
        # epoch -- so a guide repainting every few seconds must not keep
        # throwing its own artwork away.
        b = self._browser()
        b._screen_seq += 1
        for _ in range(5):
            b._shed_caches_on_screen_change()
        self.assertEqual(b.thumbs.trims, 1, "trimmed on a plain repaint")

    def test_a_short_machine_also_sheds_the_composited_rows(self):
        """The trade the memory probe exists for. Normally the strips stay:
        they are what makes going back instant, and back is the most common
        move there is. On a machine that is actually short of RAM the rows
        behind you are worth more as memory than as a fast trip back."""
        from unittest import mock

        b = self._browser()
        b.strips = self._Strips()
        b._screen_seq += 1
        with mock.patch("jellyfin_mpv_shim.mpvtk_browser.app.memory_is_tight",
                        return_value=True):
            b._shed_caches_on_screen_change()
        self.assertEqual(b.strips.armed, 1)
        self.assertIs(b.strips.pressure, True,
                      "the cache budget did not follow the machine")

    def test_a_roomy_machine_keeps_them_so_back_stays_instant(self):
        from unittest import mock

        b = self._browser()
        b.strips = self._Strips()
        b._screen_seq += 1
        with mock.patch("jellyfin_mpv_shim.mpvtk_browser.app.memory_is_tight",
                        return_value=False):
            b._shed_caches_on_screen_change()
        self.assertEqual(b.strips.armed, 0,
                         "recomposited a screenful to reclaim memory nobody "
                         "was short of")
        self.assertEqual(b.thumbs.trims, 1,
                         "the decoded images go either way")
        self.assertIs(b.strips.pressure, False)


class MemoryTrimTest(unittest.TestCase):
    def test_trim_drops_the_least_recently_used_to_a_target(self):
        c = MemoryCache(1000, sizer)
        c.put("a", 40)
        c.put("b", 40)
        c.put("c", 40)
        c.get("a")               # most recent
        c.trim(50)
        self.assertEqual(c.get("a"), 40, "trimmed the most recently used")
        self.assertIsNone(c.get("b"))
        self.assertEqual(c.nbytes, 40)

    def test_trim_may_empty_the_cache(self):
        # Unlike put()'s eviction, which keeps the entry that just landed
        # because its caller still wants it.
        c = MemoryCache(1000, sizer)
        c.put("a", 40)
        c.trim(0)
        self.assertEqual(len(c), 0)

if __name__ == "__main__":
    unittest.main()
