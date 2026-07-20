"""The thumbnail store's byte-bounded in-memory LRU (MemoryCache).

The cache has no UI dependency, so its eviction policy is exercisable
without a display. An explicit sizer gives entries known byte sizes; the
real store sizes decoded images as width*height*4.

(Ported from the Tk browser's identical cache when that browser was
removed. The eviction policy is the same and so are these tests.)
"""
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


if __name__ == "__main__":
    unittest.main()
