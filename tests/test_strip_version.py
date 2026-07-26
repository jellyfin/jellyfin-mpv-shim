"""Entries must carry a distinct content version even when the libmpv
MemoryStore hands out a recycled malloc address.

The renderer skips re-issuing an overlay whose argument string is
unchanged (renderer.lua's ov_argstr / renumber_overlays). src is part of
that string, and on the libmpv backend src is a raw address that the
allocator WILL reuse once a freed buffer leaves the graveyard. Without a
version stamp, a recycled address makes a brand-new entry look identical
to the departed one, the re-issue is skipped, and mpv keeps compositing
the previous buffer's copied content -- a poster that never refreshes.
"""

import unittest

from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile


class _AliasingStore:
    """MemoryStore stand-in that always returns the SAME src.

    Real address reuse is probabilistic (measured ~40 collisions in 60
    strip-sized allocations); pinning it to always collide makes the
    regression deterministic.
    """

    def __init__(self):
        self.adds = 0

    def add(self, img):
        self.adds += 1
        return "&140737488355328", img.width, img.height

    def remove(self, src):
        pass


def _tiles(title):
    return [Tile(key="k1", title=title)]


class StripVersionTest(unittest.TestCase):
    def setUp(self):
        self.mem = _AliasingStore()
        self.store = StripStore(mem_store=self.mem)

    def test_recycled_address_still_yields_a_new_version(self):
        a = self.store.strip(_tiles("First"))
        b = self.store.strip(_tiles("Second"))
        # The premise: the backend really did alias the two entries.
        self.assertEqual(a["src"], b["src"], "test premise: src must collide")
        self.assertEqual(self.mem.adds, 2, "both strips should composite")
        # The fix: they are still distinguishable to the renderer.
        self.assertNotEqual(
            a["v"], b["v"],
            "aliased src with equal v -> renderer skips the re-issue and "
            "shows the previous strip's pixels",
        )

    def test_bitmap_entries_are_versioned_too(self):
        from PIL import Image as PILImage

        img = PILImage.new("RGBA", (8, 8), (0, 0, 0, 255))
        a = self.store.bitmap("art-1", img)
        b = self.store.bitmap("art-2", img)
        self.assertEqual(a["src"], b["src"], "test premise: src must collide")
        self.assertNotEqual(a["v"], b["v"])

    def test_cache_hit_keeps_its_version(self):
        """A cache hit must NOT bump v: same buffer, same content, so the
        renderer should keep skipping the re-issue (that skip is the whole
        point of the overlay cache during scrolling)."""
        a = self.store.strip(_tiles("First"))
        again = self.store.strip(_tiles("First"))
        self.assertEqual(a["v"], again["v"])
        self.assertEqual(self.mem.adds, 1, "cache hit should not recomposite")


if __name__ == "__main__":
    unittest.main()
