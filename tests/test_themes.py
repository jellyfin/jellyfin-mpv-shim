"""The theme system: selecting a theme must not change the stock look.

The whole feature is opt-in, and the guarantee that makes it safe is that
``default`` reproduces what shipped before it existed. These tests pin that
guarantee (palette values, no glow, no rounded cards, stock sizes) as well as
the switch itself, so a new theme cannot quietly redefine the default.
"""
import json
import os
import tempfile
import unittest

from jellyfin_mpv_shim.mpvtk_browser import hud, theme, themes
from jellyfin_mpv_shim.mpvtk_browser.strips import (LANDSCAPE_GEOM,
                                                    POSTER_GEOM)


class ThemeRegistryTest(unittest.TestCase):
    def tearDown(self):
        theme.apply("default")

    def test_every_theme_resolves_to_the_full_set_of_keys(self):
        """Whatever a theme file happens to state, what comes out is complete.
        This is what makes a partial theme file safe: an absent key is the
        default's value, never a missing one and never the previously applied
        theme's."""
        expected = set(themes.DEFAULT)
        for name, t in themes.load().items():
            with self.subTest(theme=name):
                self.assertEqual(set(t), expected)

    def test_every_theme_resolves_to_the_whole_palette(self):
        expected = set(themes.DEFAULT["palette"])
        for name, t in themes.load().items():
            with self.subTest(theme=name):
                self.assertEqual(set(t["palette"]), expected)

    def test_an_unknown_name_falls_back_to_default(self):
        for name in ("no-such-theme", None, ""):
            with self.subTest(name=name):
                self.assertEqual(themes.get(name)["name"],
                                 themes.DEFAULT["name"])

    def test_choices_offers_every_theme_with_default_first(self):
        opts = themes.choices()
        self.assertEqual(opts[0][1], "default")
        self.assertEqual(sorted(v for _l, v in opts),
                         sorted(themes.load()))

    def test_nebula_ships_as_a_theme_file(self):
        """It used to be a dict in this module. Shipping it through the same
        loader the user's own themes go through is what stops the file format
        being a second-class path nothing exercises."""
        builtin, _user = themes.theme_dirs()
        self.assertTrue(os.path.isfile(os.path.join(builtin, "nebula.json")))
        self.assertIn("nebula", themes.load(force=True))


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
        nebula = theme.apply("nebula")
        self.assertNotEqual(theme.ACCENT, stock)
        self.assertEqual(theme.ACCENT, nebula["palette"]["ACCENT"])
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


class ThemeFileTest(unittest.TestCase):
    """Parsing one theme file. ``resolve`` is the whole contract: a theme file
    is user-editable, so it is hostile input in the boring sense — half-typed,
    copy-pasted from a blog post, written against a newer version."""

    def test_a_partial_theme_is_a_complete_theme(self):
        """The point of merging over the default: you should be able to
        recolour one thing without restating the other thirty."""
        t = themes.resolve({"name": "Red", "palette": {"ACCENT": "cc2222"}})
        self.assertEqual(t["name"], "Red")
        self.assertEqual(t["palette"]["ACCENT"], "cc2222")
        self.assertEqual(t["palette"]["WINDOW_BG"],
                         themes.DEFAULT["palette"]["WINDOW_BG"])
        self.assertEqual(t["poster_scale"], themes.DEFAULT["poster_scale"])

    def test_an_unknown_key_is_dropped_not_absorbed(self):
        """The failure the old globals loop could not detect. A typo used to
        define a NEW name and leave the real one at its previous value."""
        with self.assertLogs("mpvtk_browser.themes", "WARNING"):
            t = themes.resolve({"ACCNET": "ff0000", "bogus": 1})
        self.assertNotIn("ACCNET", t)
        self.assertNotIn("bogus", t)
        self.assertEqual(set(t), set(themes.DEFAULT))

    def test_an_unknown_palette_colour_is_dropped(self):
        with self.assertLogs("mpvtk_browser.themes", "WARNING"):
            t = themes.resolve({"palette": {"ACCNET": "ff0000"}})
        self.assertNotIn("ACCNET", t["palette"])
        self.assertEqual(t["palette"]["ACCENT"],
                         themes.DEFAULT["palette"]["ACCENT"])

    def test_a_bad_value_costs_only_that_key(self):
        """One mistyped colour must not throw away the rest of the look."""
        with self.assertLogs("mpvtk_browser.themes", "WARNING"):
            t = themes.resolve({"palette": {"ACCENT": "not-a-colour",
                                            "TEXT_FG": "112233"},
                                "arrow_mode": "sideways",
                                "poster_scale": 1.5})
        self.assertEqual(t["palette"]["ACCENT"],
                         themes.DEFAULT["palette"]["ACCENT"])
        self.assertEqual(t["palette"]["TEXT_FG"], "112233")
        self.assertEqual(t["arrow_mode"], themes.DEFAULT["arrow_mode"])
        self.assertEqual(t["poster_scale"], 1.5)

    def test_leading_hashes_are_accepted_and_normalised(self):
        """Every colour picker on earth hands you "#rrggbb"."""
        t = themes.resolve({"palette": {"ACCENT": "#ff8800"},
                            "arrow_bg": "#111111",
                            "browse_bg": "202020"})
        self.assertEqual(t["palette"]["ACCENT"], "ff8800")
        self.assertEqual(t["arrow_bg"], "111111")
        # ...except browse_bg, which is an mpv option and wants the hash.
        self.assertEqual(t["browse_bg"], "#202020")

    def test_tile_landscape_arrives_as_a_json_list(self):
        t = themes.resolve({"tile_landscape": [300, 200]})
        self.assertEqual(t["tile_landscape"], (300, 200))

    def test_junk_at_the_top_level_is_survivable(self):
        with self.assertLogs("mpvtk_browser.themes", "WARNING"):
            t = themes.resolve(["not", "an", "object"])
        self.assertEqual(t["name"], themes.DEFAULT["name"])


class ThemeDirectoryTest(unittest.TestCase):
    """Loading, and the shadowing rule."""

    def setUp(self):
        self.builtin = tempfile.TemporaryDirectory()
        self.user = tempfile.TemporaryDirectory()
        self.addCleanup(self.builtin.cleanup)
        self.addCleanup(self.user.cleanup)
        real = themes.theme_dirs
        themes.theme_dirs = lambda: (self.builtin.name, self.user.name)
        self.addCleanup(setattr, themes, "theme_dirs", real)
        self.addCleanup(themes.load, True)
        self.addCleanup(theme.apply, "default")

    def write(self, where, name, data):
        with open(os.path.join(where, name), "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def test_a_user_theme_of_the_same_name_shadows_the_builtin(self):
        self.write(self.builtin.name, "moody.json",
                   {"name": "Moody", "palette": {"ACCENT": "111111"}})
        self.write(self.user.name, "moody.json",
                   {"name": "Moody (mine)", "palette": {"ACCENT": "222222"}})
        loaded = themes.load(force=True)
        self.assertEqual(loaded["moody"]["name"], "Moody (mine)")
        self.assertEqual(loaded["moody"]["palette"]["ACCENT"], "222222")

    def test_shadowing_replaces_rather_than_layers(self):
        """The user's file is merged over the DEFAULT, not over the built-in
        it displaces — otherwise you could never turn one of its options back
        off, only pile more on top."""
        self.write(self.builtin.name, "moody.json",
                   {"name": "Moody", "glow": True, "poster_scale": 1.4})
        self.write(self.user.name, "moody.json", {"name": "Moody"})
        loaded = themes.load(force=True)
        self.assertFalse(loaded["moody"]["glow"])
        self.assertEqual(loaded["moody"]["poster_scale"], 1.0)

    def test_a_user_theme_may_shadow_the_default(self):
        self.write(self.user.name, "default.json",
                   {"name": "Default", "palette": {"ACCENT": "abcdef"}})
        self.assertEqual(themes.load(force=True)["default"]["palette"]["ACCENT"],
                         "abcdef")

    def test_the_builtin_default_survives_a_broken_default_file(self):
        """``default`` is the fallback, so it is the one id that has to
        resolve no matter what is on disk."""
        with open(os.path.join(self.user.name, "default.json"), "w") as fh:
            fh.write("{ this is not json")
        with self.assertLogs("mpvtk_browser.themes", "WARNING"):
            loaded = themes.load(force=True)
        self.assertEqual(loaded["default"]["palette"]["ACCENT"],
                         themes.DEFAULT["palette"]["ACCENT"])

    def test_one_broken_theme_does_not_take_the_others_with_it(self):
        """You edit a theme, save it mid-thought, and the app must not lose
        the theme you are actually using."""
        self.write(self.user.name, "good.json", {"name": "Good"})
        with open(os.path.join(self.user.name, "broken.json"), "w") as fh:
            fh.write("{,,,")
        with self.assertLogs("mpvtk_browser.themes", "WARNING"):
            loaded = themes.load(force=True)
        self.assertIn("good", loaded)
        self.assertNotIn("broken", loaded)

    def test_a_nameless_theme_is_labelled_with_its_filename(self):
        self.write(self.user.name, "midnight.json",
                   {"palette": {"ACCENT": "000044"}})
        self.assertEqual(themes.load(force=True)["midnight"]["name"],
                         "midnight")

    def test_non_json_files_are_ignored(self):
        with open(os.path.join(self.user.name, "notes.txt"), "w") as fh:
            fh.write("hello")
        self.assertNotIn("notes", themes.load(force=True))

    def test_a_missing_theme_directory_is_not_an_error(self):
        themes.theme_dirs = lambda: (None, "/nonexistent/themes")
        self.assertIn("default", themes.load(force=True))


class PaletteIsNotGlobalsTest(unittest.TestCase):
    """``theme.X`` is served by a module ``__getattr__`` over a dict, not by
    names written into ``globals()``. With themes coming from user-editable
    JSON, the difference is the difference between a rejected key and an
    arbitrary write into this module's namespace."""

    def tearDown(self):
        theme.apply("default")

    def test_an_unknown_colour_raises_instead_of_going_stale(self):
        with self.assertRaises(AttributeError):
            theme.ACCNET
        self.assertFalse(hasattr(theme, "NOT_A_COLOUR"))

    def test_the_error_says_what_the_palette_actually_has(self):
        with self.assertRaises(AttributeError) as caught:
            theme.ACCNET
        self.assertIn("ACCENT", str(caught.exception))

    def test_a_theme_cannot_replace_this_module_s_functions(self):
        """A palette key can never win against a real attribute, because
        __getattr__ only runs when normal lookup has already failed."""
        theme.apply("nebula")
        for name in ("rgb", "apply", "active", "apply_to_toolkit"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(theme, name)))

    def test_switching_themes_leaves_nothing_behind(self):
        """The failure a globals loop invites: a theme that does not mention a
        colour inherits the PREVIOUS theme's value instead of the default's."""
        theme.apply("nebula")
        theme.apply("default")
        for key, value in themes.DEFAULT["palette"].items():
            with self.subTest(colour=key):
                self.assertEqual(getattr(theme, key), value)

    def test_the_palette_is_listed_for_introspection(self):
        self.assertIn("ACCENT", dir(theme))
        self.assertIn("rgb", dir(theme))


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
