"""The theme system: selecting a theme must not change the stock look.

The whole feature is opt-in, and the guarantee that makes it safe is that
``default`` reproduces what shipped before it existed. These tests pin that
guarantee (palette values, no glow, no rounded cards, stock sizes) as well as
the switch itself, so a new theme cannot quietly redefine the default.
"""
import unittest

from jellyfin_mpv_shim.mpvtk_browser import hud, theme, themes
from jellyfin_mpv_shim.mpvtk_browser.strips import (LANDSCAPE_GEOM,
                                                    POSTER_GEOM)


class ThemeRegistryTest(unittest.TestCase):
    def tearDown(self):
        theme.apply("default")

    def test_every_theme_declares_the_same_keys(self):
        """A theme read for a key it does not define falls back silently, so
        a missing key shows up as a half-applied look rather than an error."""
        expected = set(themes.DEFAULT)
        for name, t in themes.THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(t), expected)

    def test_every_theme_defines_the_whole_palette(self):
        expected = set(themes.DEFAULT["palette"])
        for name, t in themes.THEMES.items():
            with self.subTest(theme=name):
                self.assertEqual(set(t["palette"]), expected)

    def test_an_unknown_name_falls_back_to_default(self):
        self.assertIs(themes.get("no-such-theme"), themes.DEFAULT)
        self.assertIs(themes.get(None), themes.DEFAULT)
        self.assertIs(themes.get(""), themes.DEFAULT)

    def test_choices_offers_every_theme(self):
        ids = [i for i, _label in themes.choices()]
        self.assertEqual(sorted(ids), sorted(themes.THEMES))


class DefaultThemeIsTheStockLookTest(unittest.TestCase):
    """The opt-in guarantee: selecting no theme adds no theme decoration.

    Not quite "renders as it always did" any more — the carousel page arrows
    are now composited bitmaps for every theme, because the ASS-button version
    they replaced could only sit over a poster strip by punching a hard-edged
    notch out of it. That is a fix to the shared widget, not a look the
    default opted into, which is why there is no flag left to assert here.
    """

    def tearDown(self):
        theme.apply("default")

    def test_the_default_palette_is_the_jellyfin_dark_palette(self):
        theme.apply("default")
        self.assertEqual(theme.ACCENT, "00a4dc")
        self.assertEqual(theme.WINDOW_BG, "15171a")
        self.assertEqual(theme.TEXT_FG, "e8e8e8")

    def test_the_default_theme_asks_for_no_extra_decoration(self):
        d = theme.apply("default")
        self.assertFalse(d["glow"])       # no blurred accent halo
        self.assertFalse(d["rounded"])    # square cards, letterboxed art
        self.assertFalse(d["accent_buttons"])   # plain top-bar buttons
        self.assertEqual(d["poster_scale"], 1.0)
        self.assertEqual(d["heading_size"], 24)
        self.assertEqual(d["tile_landscape"],
                         (LANDSCAPE_GEOM.tile_w, LANDSCAPE_GEOM.tile_h))
        # None = "leave the caption font alone", i.e. it scales as before.
        self.assertIsNone(d["tile_title_size"])
        self.assertIsNone(d["tile_sub_size"])

    def test_the_default_page_arrow_is_neutral_overlay_grey(self):
        """It floats on artwork, not in chrome, so it takes the same dark
        translucent grey as the HUD's Skip Intro chip rather than a palette
        colour — a tinted disc reads as a coloured sticker on a poster."""
        d = themes.DEFAULT
        self.assertEqual(d["arrow_bg"], hud._SKIP_BG)
        self.assertNotEqual(d["arrow_bg"], d["palette"]["BUTTON_BG"])
        self.assertLess(d["arrow_alpha"], 255)   # translucent, not a chip
        r, g, b = theme.rgb(d["arrow_bg"])
        self.assertEqual((r, g, b), (r, r, r), "grey: no hue at all")


class ApplyTest(unittest.TestCase):
    def tearDown(self):
        theme.apply("default")

    def test_apply_swaps_the_module_palette(self):
        """Consumers read ``theme.X`` at draw time and know nothing about
        themes; apply() is what makes that work."""
        theme.apply("default")
        stock = theme.ACCENT
        theme.apply("nebula")
        self.assertNotEqual(theme.ACCENT, stock)
        self.assertEqual(theme.ACCENT, themes.NEBULA["palette"]["ACCENT"])
        theme.apply("default")
        self.assertEqual(theme.ACCENT, stock)

    def test_active_reports_the_applied_theme(self):
        self.assertEqual(theme.apply("nebula")["name"], "Nebula")
        self.assertEqual(theme.active()["name"], "Nebula")

    def test_the_toolkit_is_told_the_accent_and_the_glow_flag(self):
        from jellyfin_mpv_shim.mpvtk import theme as tk

        for name in ("default", "nebula"):
            with self.subTest(theme=name):
                cfg = theme.apply(name)
                theme.apply_to_toolkit(glow=cfg["glow"])
                self.assertEqual(tk.ACCENT, cfg["palette"]["ACCENT"])
                self.assertEqual(tk.GLOW, cfg["glow"])
                # The renderer is handed the flag, not asked to infer it.
                self.assertEqual(tk.palette()["glow"], cfg["glow"])


class CoverSizeTest(unittest.TestCase):
    def test_scaling_a_geometry_keeps_the_gap(self):
        """Tiles grow; the space between them does not, or a row of larger
        covers drifts out of rhythm with the rest of the page."""
        big = POSTER_GEOM.scaled(1.4)
        self.assertEqual(big.tile_w, round(POSTER_GEOM.tile_w * 1.4))
        self.assertEqual(big.tile_h, round(POSTER_GEOM.tile_h * 1.4))
        self.assertEqual(big.gap, POSTER_GEOM.gap)

    def test_a_scale_of_one_is_the_same_object(self):
        self.assertIs(POSTER_GEOM.scaled(1.0), POSTER_GEOM)
        self.assertIs(POSTER_GEOM.scaled(None), POSTER_GEOM)

    def test_scaling_never_rounds_a_dimension_away(self):
        tiny = POSTER_GEOM.scaled(0.001)
        self.assertGreaterEqual(tiny.tile_w, 1)
        self.assertGreaterEqual(tiny.tile_h, 1)


if __name__ == "__main__":
    unittest.main()
