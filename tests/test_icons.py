"""Unit tests for the library-browser icon rasterizer.

These exercise the pure-Python SVG-path -> alpha-mask pipeline without a Tk
display (PhotoImage creation is not covered here -- it needs a root and is
exercised by the integration browser UI tests).
"""

import unittest

try:
    from PIL import Image  # noqa: F401
    from jellyfin_mpv_shim.library_browser import icons
    from jellyfin_mpv_shim.library_browser._icon_paths import ICON_PATHS
    HAVE_PIL = True
except ImportError:
    HAVE_PIL = False


@unittest.skipUnless(HAVE_PIL, "Pillow not installed")
class IconRasterizerTest(unittest.TestCase):
    def test_every_icon_parses_and_rasterizes(self):
        for name in ICON_PATHS:
            mask = icons.get_mask(name, 32)
            self.assertEqual(mask.size, (32, 32))
            self.assertEqual(mask.mode, "L")
            # Every glyph covers *some* pixels and leaves *some* empty.
            extrema = mask.getextrema()
            self.assertGreater(extrema[1], 0, "%s rendered blank" % name)
            self.assertLess(extrema[0], 255, "%s filled the whole canvas" % name)

    def test_nonzero_winding_makes_holes(self):
        # The settings gear has a hole in its centre; the filled heart does not.
        gear = icons.get_mask("settings", 40).load()
        heart = icons.get_mask("favorite", 40).load()
        self.assertEqual(gear[20, 20], 0, "gear centre should be a hole")
        self.assertGreater(heart[20, 20], 0, "heart centre should be filled")

    def test_search_ring_is_hollow(self):
        # The magnifier is drawn with SVG arc (A) commands; the ring interior
        # must be transparent (regression: arcs were skipped, filling it solid).
        m = icons.get_mask("search", 40).load()
        c = round(9.5 * 40 / 24)  # ring centre on the 24-grid -> 40px
        self.assertEqual(m[c, c], 0, "search ring should be hollow")

    def test_favorite_border_is_hollow(self):
        # The outlined heart must be empty in the middle (proves the outline is
        # filled with the winding rule, not flooded solid).
        border = icons.get_mask("favorite_border", 40).load()
        self.assertEqual(border[20, 22], 0)

    def test_mask_is_cached(self):
        a = icons.get_mask("play_arrow", 24)
        b = icons.get_mask("play_arrow", 24)
        self.assertIs(a, b)

    def test_alias_resolves(self):
        # A semantic alias and its Material name share the same mask.
        self.assertIs(icons.get_mask("back", 20), icons.get_mask("arrow_back", 20))

    def test_tint_applies_color(self):
        img = icons.get_image("play_arrow", 24, "#00a4dc")
        self.assertEqual(img.mode, "RGBA")
        # Find an opaque pixel and check it carries the requested RGB.
        px = img.load()
        found = False
        for y in range(24):
            for x in range(24):
                r, g, b, a = px[x, y]
                if a > 200:
                    self.assertEqual((r, g, b), (0x00, 0xA4, 0xDC))
                    found = True
                    break
            if found:
                break
        self.assertTrue(found)

    def test_unknown_icon_raises(self):
        with self.assertRaises(KeyError):
            icons.get_mask("definitely_not_an_icon", 20)


if __name__ == "__main__":
    unittest.main()
