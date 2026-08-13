"""The corner badges are drawn through an antialiased mask.

PIL has no antialiased fill, so `ImageDraw.ellipse` and
`rounded_rectangle` put a hard staircase on every curve. That is what the
badge discs and the unplayed chip used to be, sitting next to a Material
glyph that IS antialiased (`vector._SS`) -- which is most of why the marks
read as thin and hand-drawn rather than as the furniture jellyfin-web puts
in the same corner [iw].

Asserted on the COVERAGE VALUES rather than on the look, because coverage
is the whole of the difference: a binary mask and a smooth one draw the
same circle to within a pixel, and the only thing that separates them is
whether the pixels on the boundary are allowed to be partly there.
"""

import unittest

from PIL import Image, ImageDraw

from jellyfin_mpv_shim.mpvtk_browser import strips, theme
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile, TileGeom


def _partial(img):
    """Alphas of every partly-covered pixel, in no order.

    Measured against the transparent background the badge is drawn on, so a
    pixel is "partial" when its alpha is neither 0 nor 255. A hard fill
    produces none at all, which is the contrast every assertion below is.
    """
    return [a for _, _, _, a in img.getdata() if 0 < a < 255]


class BadgeAntialiasingTest(unittest.TestCase):
    def _blank(self, w=60, h=60):
        return Image.new("RGBA", (w, h), (0, 0, 0, 0))

    def test_a_badge_disc_has_partial_coverage_at_its_edge(self):
        img = self._blank()
        StripStore._paint_glyph_badge(img, ImageDraw.Draw(img), 30, 30,
                                      "check", theme.ACCENT)
        levels = set(_partial(img))
        # A hard ellipse yields none of these at all. A spread of them,
        # not one: a single value would be one stray pixel rather than a
        # ramp running round the curve.
        self.assertGreater(len(levels), 8,
                           "disc edge is not antialiased: %r" % sorted(levels))

    def test_the_unplayed_chip_has_partial_coverage_at_its_corners(self):
        img = self._blank(120, 60)
        StripStore._paint_count_chip(img, ImageDraw.Draw(img), 90, 17,
                                     "12", 14)
        # Counted rather than levelled, unlike the disc: a chip is mostly
        # straight edges that land on whole pixels, so only its four r=6
        # corners can be partial at all -- a couple of dozen pixels over a
        # handful of values, where a hard rounded rect has none.
        partial = _partial(img)
        self.assertGreater(len(partial), 10,
                           "chip corners are not antialiased")
        self.assertGreater(len(set(partial)), 2)

    def test_the_chip_is_still_square_where_it_should_be(self):
        """The AA is on the corners, not on the whole shape: a chip whose
        straight edges came out soft would be a blurred chip, not an
        antialiased one."""
        img = self._blank(120, 60)
        StripStore._paint_count_chip(img, ImageDraw.Draw(img), 90, 17,
                                     "12", 14)
        # The row through the chip's middle crosses no curve.
        row = [img.getpixel((x, 15))[3] for x in range(img.width)]
        self.assertFalse([a for a in row if 0 < a < 255],
                         "the chip's straight edges are soft")

    def test_a_translucent_badge_keeps_both_its_alpha_and_its_edge(self):
        """The type marker is drawn at 210, not 255. Scaling the coverage by
        that has to leave a RAMP -- an implementation that reached for a
        constant alpha would flatten the edge back to a staircase, and one
        that ignored it would draw an opaque chip."""
        img = self._blank()
        StripStore._paint_kind(img, ImageDraw.Draw(img), 30, 30, "movie")
        alphas = {a for _, _, _, a in img.getdata() if a}
        self.assertIn(210, alphas, "the marker's own alpha was lost")
        self.assertGreater(len({a for a in alphas if a < 210}), 8,
                           "no ramp below the marker's alpha")

    def test_the_mask_cache_is_keyed_by_shape_not_by_call(self):
        """Every tile in a row draws the same discs, so the supersampled
        mask must be built once. Without this the AA is a per-tile cost in
        the compositor's inner loop."""
        strips._aa_masks.clear()
        img = self._blank()
        dr = ImageDraw.Draw(img)
        for cx in (15, 30, 45):
            StripStore._paint_glyph_badge(img, dr, cx, 20, "check",
                                          theme.ACCENT)
        self.assertEqual(len(strips._aa_masks), 1)


class ChipCornerPitchTest(unittest.TestCase):
    """The unplayed chip sits IN the top-right corner now, and it is the one
    badge in that stack whose width depends on its text -- so the badge to
    its left has to clear a measured width rather than the disc pitch."""

    def _paint(self, **tile):
        g = TileGeom().physical()
        img = Image.new("RGBA", (g.tile_w, g.strip_h), (0, 0, 0, 0))
        StripStore(cache_dir=None, mem_store=None)._paint_decorations(
            img, ImageDraw.Draw(img), 0, Tile(key="k", **tile), g)
        return img

    @staticmethod
    def _spans(img, y, colour):
        """x positions on row ``y`` whose colour matches, as spans."""
        xs = [x for x in range(img.width)
              if img.getpixel((x, y))[3] > 200
              and max(abs(a - b) for a, b in
                      zip(img.getpixel((x, y))[:3], colour)) <= 6]
        return xs

    def test_a_three_digit_chip_does_not_run_into_the_marker_beside_it(self):
        # 123 unwatched episodes on a folder: routine on an anime series,
        # and the width where a fixed pitch stops being enough.
        img = self._paint(badge=123, kind="folder")
        accent = self._spans(img, 15, theme.rgb(theme.ACCENT))
        marker = self._spans(img, 15, theme.rgb(theme.WINDOW_BG))
        self.assertTrue(accent, "no chip")
        self.assertTrue(marker, "no type marker")
        self.assertLess(max(marker), min(accent),
                        "the type marker is under the unplayed chip")

    def test_a_watched_folder_shows_the_tick_instead_of_a_count(self):
        """jellyfin-web returns one or the other from
        `getPlayedIndicatorHtml`; nothing can set both here, and the elif
        that says so must not swallow the tick when there is no count."""
        img = self._paint(watched=True)
        self.assertTrue(self._spans(img, 17, theme.rgb(theme.ACCENT)))


if __name__ == "__main__":
    unittest.main()
