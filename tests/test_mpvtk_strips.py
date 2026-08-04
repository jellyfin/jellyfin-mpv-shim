"""Unit tests for the mpvtk browser's strip compositor (mpvtk_browser.strips).
Headless (PIL only) — covers geometry/regions, content-keyed caching and
recompositing on decoration/poster changes, LRU eviction, and both the
file and in-memory storage backends.
"""

import os
import struct
import tempfile
import threading
import unittest

from PIL import Image, ImageDraw

from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore
from jellyfin_mpv_shim.mpvtk_browser import theme
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile, TileGeom


def _poster(color=(120, 30, 30), size=(140, 210)):
    return Image.new("RGB", size, color)


class TestStripStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-strips-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self, mem=False, geom=None):
        return StripStore(
            cache_dir=None if mem else self.tmp,
            mem_store=MemoryStore() if mem else None,
            geom=geom,
        )

    def test_regions_and_dimensions(self):
        s = self._store()
        g = TileGeom()
        tiles = [Tile(key="a", title="A", poster=_poster()),
                 Tile(key="b", title="B", poster=_poster())]
        out = s.strip(tiles)
        self.assertEqual(out["ih"], g.strip_h)
        self.assertEqual(out["iw"], 2 * g.tile_w + g.gap)
        self.assertEqual([r["key"] for r in out["regions"]], ["a", "b"])
        # second region starts a tile+gap over
        self.assertEqual(out["regions"][1]["x"], g.tile_w + g.gap)
        self.assertEqual(out["regions"][0]["h"], g.strip_h)

    def test_content_key_cache_hit(self):
        s = self._store()
        tiles = [Tile(key="a", title="A", poster=_poster(), poster_tag="p1")]
        a = s.strip(tiles)
        b = s.strip([Tile(key="a", title="A", poster=_poster(),
                          poster_tag="p1")])
        self.assertEqual(s.hits, 1)
        self.assertEqual(a["src"], b["src"])

    def test_decoration_change_recomposites(self):
        s = self._store()
        base = dict(key="a", title="A", poster=_poster(), poster_tag="p1")
        a = s.strip([Tile(**base)])
        b = s.strip([Tile(**dict(base, watched=True))])
        c = s.strip([Tile(**dict(base, progress=0.5))])
        srcs = {a["src"], b["src"], c["src"]}
        self.assertEqual(len(srcs), 3, "each decoration set is a distinct strip")
        self.assertEqual(s.misses, 3)

    def test_poster_arrival_recomposites(self):
        s = self._store()
        # No poster yet (placeholder), then the real poster lands.
        a = s.strip([Tile(key="a", title="A", poster=None, poster_tag="")])
        b = s.strip([Tile(key="a", title="A", poster=_poster(),
                          poster_tag="p1")])
        self.assertNotEqual(a["src"], b["src"])
        self.assertEqual(s.misses, 2)

    def test_lru_eviction_frees_files(self):
        s = self._store()
        s.MAX_ENTRIES = 3
        srcs = []
        for i in range(5):
            out = s.strip([Tile(key="k%d" % i, title="T%d" % i,
                                poster=_poster(), poster_tag="p%d" % i)])
            srcs.append(out["src"])
        # Only the last 3 survive on disk; the first 2 were evicted+removed.
        self.assertFalse(os.path.exists(srcs[0]))
        self.assertFalse(os.path.exists(srcs[1]))
        self.assertTrue(os.path.exists(srcs[-1]))

    def test_file_backend_writes_valid_bgra(self):
        g = TileGeom()
        s = self._store()
        out = s.strip([Tile(key="a", title="A", poster=_poster())])
        # premultiplied BGRA = 4 bytes/pixel; file size must match iw*ih*4.
        self.assertEqual(os.path.getsize(out["src"]),
                         out["iw"] * out["ih"] * 4)

    def test_memory_backend_uses_address_src(self):
        s = self._store(mem=True)
        out = s.strip([Tile(key="a", title="A", poster=_poster())])
        self.assertTrue(out["src"].startswith("&"),
                        "libmpv backend must use an &<addr> src")
        # clear() releases the buffer through the store without error
        s.clear()

    def test_placeholder_when_no_poster(self):
        # A tile with no poster still composites (no crash) and is clickable.
        s = self._store()
        out = s.strip([Tile(key="a", title="A", poster=None)])
        self.assertEqual(out["regions"][0]["key"], "a")

    # -- async composite path (grid) --------------------------------------

    def test_async_returns_placeholder_then_real(self):
        s = self._store()
        got = threading.Event()
        s.set_notify(got.set)
        tiles = [Tile(key="a", title="A", poster=_poster(), poster_tag="p1")]
        first = s.strip(tiles, async_=True)
        # Immediately: a placeholder with the real hit-regions/geometry.
        self.assertTrue(first.get("placeholder"))
        self.assertEqual([r["key"] for r in first["regions"]], ["a"])
        self.assertEqual(first["iw"], TileGeom().tile_w)
        # The worker composites off-thread and notifies.
        self.assertTrue(got.wait(5), "async composite should notify")
        second = s.strip(tiles, async_=True)
        self.assertFalse(second.get("placeholder"),
                         "the real strip replaces the placeholder")
        self.assertNotEqual(second["src"], first["src"])
        self.assertEqual(s.hits, 1)

    def test_async_placeholder_shares_blank_per_shape(self):
        s = self._store()
        p1 = s.strip([Tile(key="a", poster=_poster()),
                      Tile(key="b", poster=_poster())], async_=True)
        p2 = s.strip([Tile(key="c", poster=_poster()),
                      Tile(key="d", poster=_poster())], async_=True)
        self.assertTrue(p1.get("placeholder") and p2.get("placeholder"))
        # Same shape (2 tiles) -> one shared blank bitmap...
        self.assertEqual(p1["src"], p2["src"])
        # ...but each row keeps its own hit-regions.
        self.assertEqual([r["key"] for r in p2["regions"]], ["c", "d"])
        s.shutdown()

    def test_async_memory_backend(self):
        s = self._store(mem=True)
        got = threading.Event()
        s.set_notify(got.set)
        tiles = [Tile(key="a", poster=_poster())]
        ph = s.strip(tiles, async_=True)
        self.assertTrue(ph["src"].startswith("&"))
        self.assertTrue(got.wait(5))
        real = s.strip(tiles, async_=True)
        self.assertTrue(real["src"].startswith("&"))
        self.assertFalse(real.get("placeholder"))
        s.shutdown()
        s.clear()

    def test_closed_store_composites_inline(self):
        # After shutdown the pool is gone; an async request must still return a
        # finished strip (composited inline), never a stranded placeholder.
        s = self._store()
        s.shutdown()
        out = s.strip([Tile(key="z", poster=_poster())], async_=True)
        self.assertFalse(out.get("placeholder"))
        self.assertEqual(out["regions"][0]["key"], "z")

    def test_sync_default_is_unchanged(self):
        # async_ defaults off: the inline path returns the real strip at once.
        s = self._store()
        out = s.strip([Tile(key="a", poster=_poster())])
        self.assertFalse(out.get("placeholder"))
        self.assertIn("regions", out)

    def test_wide_geom_dimensions(self):
        s = self._store(geom=TileGeom(tile_w=240, tile_h=135, caption_h=44))
        out = s.strip([Tile(key="a", poster=_poster(size=(240, 135)))])
        self.assertEqual(out["iw"], 240)
        self.assertEqual(out["ih"], 135 + 44)


class TestCoverCrop(unittest.TestCase):
    """Artwork fills its tile in EVERY theme, not just the rounded ones.

    The crop used to ride on the theme's ``rounded`` flag, so the stock look
    letterboxed whatever the server had not already cropped for us -- offline
    artwork (a local file, no server-side resize) and chapter thumbnails
    (fetched by max width, so they keep the video's aspect). ``rounded`` now
    decides the card's SHAPE and nothing about the picture inside it.

    Painted with a wide poster in a portrait tile: whether it was cropped or
    letterboxed is the colour of the bands above and below it.
    """

    def _paint(self, rounded=False, **tile):
        g = TileGeom().physical()
        img = Image.new("RGBA", (g.tile_w, g.strip_h), (0, 0, 0, 0))
        store = StripStore(cache_dir=None, mem_store=None)
        orig = theme.active
        if rounded:
            theme.active = lambda: dict(orig() or {}, rounded=True)
        try:
            store._paint_poster(
                img, ImageDraw.Draw(img), 0,
                Tile(key="k", title="T", poster=_poster(size=(400, 100)),
                     **tile),
                g)
        finally:
            theme.active = orig
        return img, g

    def _bands(self, img, g):
        """The strips the letterbox would put above and below the art. Taken
        mid-width, which is inside a rounded card's silhouette as well as a
        square one's."""
        return [img.getpixel((g.tile_w // 2, 3))[:3],
                img.getpixel((g.tile_w // 2, g.tile_h - 4))[:3]]

    def test_the_stock_theme_crops_rather_than_letterboxing(self):
        img, g = self._paint()
        for got in self._bands(img, g):
            self.assertEqual(got, (120, 30, 30))
        # Square card, so the art reaches the corners too.
        self.assertEqual(img.getpixel((3, 3))[:3], (120, 30, 30))

    def test_the_rounded_theme_still_crops(self):
        img, g = self._paint(rounded=True)
        for got in self._bands(img, g):
            self.assertEqual(got, (120, 30, 30))

    def test_a_rounded_card_still_clips_the_art_to_its_corners(self):
        """Cropping the art to the tile and then pasting it square would fill
        the corner pixels the silhouette leaves transparent."""
        img, _g = self._paint(rounded=True)
        self.assertEqual(img.getpixel((1, 1))[3], 0)

    def test_contain_still_draws_the_artwork_whole(self):
        """A wordmark standing in for missing artwork: cropping it takes the
        name off both ends, and that is the caller's call, not the theme's."""
        img, g = self._paint(contain=True)
        card = theme.rgb(theme.CARD_BG)[:3]
        for got in self._bands(img, g):
            self.assertEqual(got, card)


class TestBitmapConcurrentMiss(unittest.TestCase):
    """bitmap() drops its lock across _store(), so two callers can both miss
    and both allocate. cast.py's compositor runs on the browser's shared pool
    and resubmits the same (data, size) on a resize tick, so this is reachable
    rather than theoretical -- and the loser's buffer used to be overwritten in
    the cache without being freed, stranding a full-window BGRA (~8 MB at
    1080p) that nothing would ever evict.

    _blank_strip already re-checked and freed; bitmap() did not.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-bitmap-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _race(self, store, n=6):
        """Fire n concurrent bitmap() calls for one key, all released at once
        so they overlap inside _store()."""
        img = Image.new("RGB", (64, 48), (10, 20, 30))
        go = threading.Barrier(n)
        out = []
        lock = threading.Lock()

        def work():
            go.wait()
            entry = store.bitmap("same-key", img)
            with lock:
                out.append(entry)

        threads = [threading.Thread(target=work) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    def test_file_backend_leaves_no_orphan_files(self):
        store = StripStore(cache_dir=self.tmp)
        entries = self._race(store)
        # Every caller gets the SAME entry -- the winner's.
        self.assertEqual(len({e["src"] for e in entries}), 1)
        # And exactly one bitmap exists on disk: the losers freed theirs.
        self.assertEqual(
            [n for n in os.listdir(self.tmp) if n.endswith(".bgra")],
            [os.path.basename(entries[0]["src"])])

    def test_memory_backend_leaves_no_orphan_buffers(self):
        mem = MemoryStore()
        store = StripStore(cache_dir=None, mem_store=mem)
        entries = self._race(store)
        self.assertEqual(len({e["src"] for e in entries}), 1)
        store.clear()
        # clear() frees what the cache holds. Anything still resident is a
        # buffer the race allocated and nothing owns.
        self.assertEqual(_live_buffers(mem), 0)

    def test_the_surviving_entry_is_the_cached_one(self):
        # The winner must be what a later lookup returns, or callers holding
        # the returned dict and callers re-requesting disagree about iw/ih --
        # which is the SIGBUS the renderer's crop bound warns about.
        store = StripStore(cache_dir=self.tmp)
        entries = self._race(store)
        again = store.bitmap("same-key", Image.new("RGB", (64, 48)))
        self.assertIs(again, entries[0])


def _live_buffers(mem):
    """Buffers a MemoryStore still holds, however it names its container."""
    for attr in ("_buffers", "_bufs", "_store", "_images"):
        got = getattr(mem, attr, None)
        if isinstance(got, dict):
            return len(got)
    raise AssertionError("MemoryStore's buffer container was renamed; "
                         "update this helper")


if __name__ == "__main__":
    unittest.main()


class TestMixedScriptCaptions(unittest.TestCase):
    """A caption that mixes scripts is drawn with a face per run.

    The end-to-end half of `test_mpvtk_pilfont`: this goes through the real
    compositor, because the bug reached users as "the year under my Japanese
    posters is a row of boxes" and the fix is only worth anything if
    `_paint_caption` is the thing doing it.

    Detection is by counting *distinct* glyph shapes. Tofu is one shape
    repeated, so "(2013)" drawn by a face without Latin coverage has far
    fewer distinct columns of ink than the six characters deserve — no
    reference render and no font names needed.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="strips-i18n-")
        self.store = StripStore(cache_dir=self.dir, geom=TileGeom())
        from jellyfin_mpv_shim.mpvtk import pilfont
        self.pilfont = pilfont

    def caption_band(self, title):
        """The strip below the artwork, where the caption is drawn."""
        geom = TileGeom()
        out = self.store.strip([Tile(key="k", title=title, poster=_poster())])
        path = out["src"]
        if not isinstance(path, str) or not os.path.isfile(path):
            self.skipTest("this backend does not write a readable bitmap")
        with open(path, "rb") as fh:
            raw = fh.read()
        w, h = out["iw"], out["ih"]
        img = Image.frombytes("RGBA", (w, h), raw[-w * h * 4:], "raw", "BGRA")
        return img.crop((0, geom.tile_h, min(w, geom.tile_w), h))

    def test_a_mixed_caption_draws_its_latin_run(self):
        """Two titles differing only in the digits must draw differently.

        The sharpest available detector, and it needs no font names and no
        reference render: tofu is one shape repeated, so if the Latin run is
        being drawn by a face without Latin coverage, "(2013)" and "(2014)"
        composite to *identical* bitmaps. A counting heuristic does not
        catch this -- the CJK half supplies plenty of variety on its own,
        which is exactly how the first version of this test passed with the
        bug reintroduced.
        """
        a, b = "進撃の巨人 (2013)", "進撃の巨人 (2014)"
        if self.pilfont.font_for(a, 20) is self.pilfont.font("latin", 20):
            self.skipTest("no separate CJK face installed on this host")
        # assertTrue on the comparison, not assertNotEqual on the bytes:
        # these are ~50 KB bitmaps and unittest prints both operands.
        self.assertTrue(
            self.caption_band(a).tobytes() != self.caption_band(b).tobytes(),
            "changing the digits changed nothing on screen, so they are "
            "being drawn as identical .notdef boxes")

    def test_the_control_case_differs_too(self):
        """A guard on the detector: two plainly different Latin captions must
        of course differ, or the assertion above proves nothing."""
        self.assertTrue(self.caption_band("Blade 2013").tobytes()
                        != self.caption_band("Blade 2014").tobytes())

    def glyph_shapes(self, band):
        """How many distinct non-blank pixel columns the band contains."""
        grey = band.convert("L")
        cols = []
        for x in range(grey.width):
            col = bytes(grey.getpixel((x, y)) > 60
                        for y in range(grey.height))
            if any(col):
                cols.append(col)
        return len(set(cols))

    def test_a_latin_caption_still_draws(self):
        # The control: whatever the host has, plain Latin must be fine.
        self.assertGreater(self.glyph_shapes(self.caption_band("Blade 2049")),
                           12)
