"""Unit tests for the mpvtk browser's strip compositor (mpvtk_browser.strips).
Headless (PIL only) — covers geometry/regions, content-keyed caching and
recompositing on decoration/poster changes, LRU eviction, and both the
file and in-memory storage backends.
"""

import os
import struct
import tempfile
import unittest

from PIL import Image

from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore
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

    def test_wide_geom_dimensions(self):
        s = self._store(geom=TileGeom(tile_w=240, tile_h=135, caption_h=44))
        out = s.strip([Tile(key="a", poster=_poster(size=(240, 135)))])
        self.assertEqual(out["iw"], 240)
        self.assertEqual(out["ih"], 135 + 44)


if __name__ == "__main__":
    unittest.main()
