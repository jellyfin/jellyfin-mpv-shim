"""Unit tests for mpvtk_browser.thumbnails — the PIL-yielding thumbnail
store (the mpvtk data layer's image loader). Covers the byte-bounded LRU,
local-file decode, the pump() delivery of decoded PIL images, and the
thread-safe notify hook. No network, no display.
"""

import os
import tempfile
import threading
import unittest

from PIL import Image

from jellyfin_mpv_shim.mpvtk_browser.thumbnails import (
    MemoryCache,
    ThumbnailStore,
    make_key,
    _image_bytes,
)


class TestMemoryCache(unittest.TestCase):
    def test_lru_eviction_by_bytes(self):
        cache = MemoryCache(max_bytes=250, sizer=lambda v: 100)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.put("c", 3)  # 300 > 250 -> evict LRU ("a")
        self.assertIsNone(cache.get("a"))
        self.assertEqual(cache.get("b"), 2)
        self.assertEqual(cache.get("c"), 3)

    def test_get_marks_recently_used(self):
        cache = MemoryCache(max_bytes=250, sizer=lambda v: 100)
        cache.put("a", 1)
        cache.put("b", 2)
        cache.get("a")           # a now MRU
        cache.put("c", 3)        # evicts LRU, which is now "b"
        self.assertEqual(cache.get("a"), 1)
        self.assertIsNone(cache.get("b"))

    def test_single_oversized_entry_survives(self):
        cache = MemoryCache(max_bytes=10, sizer=lambda v: 1000)
        cache.put("big", object())
        self.assertEqual(len(cache), 1)

    def test_image_bytes_uses_bands(self):
        self.assertEqual(_image_bytes(Image.new("RGB", (10, 20))), 10 * 20 * 3)
        self.assertEqual(_image_bytes(Image.new("RGBA", (10, 20))), 10 * 20 * 4)


class TestThumbnailStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-thumb-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_local_png(self, size=(120, 80), color=(200, 40, 40)):
        path = os.path.join(self.tmp, "poster.png")
        Image.new("RGB", size, color).save(path)
        return path

    def test_make_key_stable_and_distinct(self):
        k1 = make_key("item1", "Primary", "tagA", 280)
        self.assertEqual(k1, make_key("item1", "Primary", "tagA", 280))
        self.assertNotEqual(k1, make_key("item1", "Primary", "tagB", 280))

    def test_local_decode_delivers_pil_via_pump(self):
        store = ThumbnailStore(os.path.join(self.tmp, "cache"))
        self.addCleanup(store.shutdown)
        path = self._make_local_png((120, 80))
        got = []
        done = threading.Event()

        def cb(img):
            got.append(img)
            done.set()

        store.request(make_key("i", "Primary", "t", 60), path, (60, 60), cb)
        # request submits to the pool; wait for the worker, then drain.
        for _ in range(200):
            if store.pump():
                break
            if done.wait(0.01):
                store.pump()
                break
        self.assertEqual(len(got), 1)
        img = got[0]
        self.assertIsInstance(img, Image.Image)
        # thumbnail(box) fits within the box, preserving aspect (120x80 -> 60x40)
        self.assertLessEqual(img.width, 60)
        self.assertLessEqual(img.height, 60)

    def test_mem_hit_is_synchronous(self):
        store = ThumbnailStore(os.path.join(self.tmp, "cache"))
        self.addCleanup(store.shutdown)
        key = make_key("i", "Primary", "t", 60)
        sentinel = Image.new("RGB", (10, 10))
        store._mem.put(key, sentinel)
        got = []
        store.request(key, "http://unused", (60, 60), got.append)
        self.assertEqual(got, [sentinel])  # no pump needed

    def test_notify_fires_when_result_ready(self):
        woke = threading.Event()
        store = ThumbnailStore(os.path.join(self.tmp, "cache"),
                               notify=woke.set)
        self.addCleanup(store.shutdown)
        path = self._make_local_png()
        store.request(make_key("i", "P", "t", 60), path, (60, 60),
                      lambda img: None)
        self.assertTrue(woke.wait(2.0), "notify() should wake the owner")
        store.pump()

    def test_set_notify_attaches_after_construction(self):
        """The store is usually built before its owner exists — the browser
        is handed a ready-made one — so the constructor argument alone isn't
        enough. It used to reach in and assign _notify directly."""
        woke = threading.Event()
        store = ThumbnailStore(os.path.join(self.tmp, "cache"))
        self.addCleanup(store.shutdown)
        store.set_notify(woke.set)
        path = self._make_local_png()
        store.request(make_key("i", "P", "t", 60), path, (60, 60),
                      lambda img: None)
        self.assertTrue(woke.wait(2.0), "a late-attached notify never fired")
        store.pump()

    def test_cancel_before_worker_skips_callback(self):
        store = ThumbnailStore(os.path.join(self.tmp, "cache"))
        self.addCleanup(store.shutdown)
        key = make_key("i", "P", "t", 60)
        path = self._make_local_png()
        got = []
        store.request(key, path, (60, 60), got.append)
        store.cancel(key)
        # Even after draining, a fully-cancelled key delivers nothing.
        for _ in range(50):
            store.pump()
        self.assertEqual(got, [])


if __name__ == "__main__":
    unittest.main()
