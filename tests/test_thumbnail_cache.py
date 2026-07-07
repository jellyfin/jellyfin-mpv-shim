"""Tests for the thumbnail store's byte-bounded in-memory LRU (MemoryCache).

The cache is deliberately Tk-free so its eviction policy can be exercised
without a display. We pass an explicit sizer so entries have known byte sizes;
the real store sizes PhotoImages as width*height*4.

The ThumbnailStore itself, MediaTile cancellation, and the Tk views need a
running display and are not unit-tested here.
"""
import unittest

from jellyfin_mpv_shim.library_browser.thumbnails import MemoryCache


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
    Tk's real ~8-bytes-per-pixel resident cost (4B master + display copy)."""

    def _store(self, **kw):
        import tempfile
        from jellyfin_mpv_shim.library_browser.thumbnails import ThumbnailStore

        tmp = tempfile.mkdtemp(prefix="jms-thumbtest-")
        self.addCleanup(__import__("shutil").rmtree, tmp, ignore_errors=True)
        store = ThumbnailStore(tmp, **kw)
        self.addCleanup(store.shutdown)
        return store

    def test_max_mem_mb_reaches_the_memory_cache(self):
        store = self._store(max_mem_mb=7)
        self.assertEqual(store._mem._max_bytes, 7 * 1024 * 1024)

    def test_photo_bytes_counts_eight_bytes_per_pixel(self):
        from jellyfin_mpv_shim.library_browser.thumbnails import ThumbnailStore

        class FakePhoto:
            def width(self):
                return 10

            def height(self):
                return 20

        self.assertEqual(ThumbnailStore._photo_bytes(FakePhoto()), 10 * 20 * 8)


if __name__ == "__main__":
    unittest.main()
