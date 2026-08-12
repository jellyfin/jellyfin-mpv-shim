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
                                                    POSTER_GEOM, StripStore)


def _relative_luminance(colour):
    def channel(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = theme.rgb(colour)
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)


def _contrast(a, b):
    """WCAG contrast ratio between two ``"rrggbb"`` colours, 1.0 to 21.0."""
    la, lb = _relative_luminance(a), _relative_luminance(b)
    return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)


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

    def test_every_shipped_theme_parses_without_complaint(self):
        """The loader forgives a bad theme file, which is right for one the
        user wrote and wrong for one we ship: a typo in a shipped theme would
        just quietly render as the default's value for that key. Nothing else
        would notice, so this does."""
        builtin, _user = themes.theme_dirs()
        files = sorted(f for f in os.listdir(builtin) if f.endswith(".json"))
        self.assertTrue(files, "no themes shipped?")
        for filename in files:
            with self.subTest(theme=filename):
                path = os.path.join(builtin, filename)
                with open(path, encoding="utf-8") as fh:
                    data = json.load(fh)
                with self.assertNoLogs("mpvtk_browser.themes", "WARNING"):
                    themes.resolve(data, where=path)

    def test_every_shipped_theme_is_legible_against_its_own_background(self):
        """A theme is a palette of colours that have to work *together*. The
        cheap way to ship an unusable one is a body colour that vanishes into
        the window behind it, so check the pairs that carry the UI."""
        for _label, theme_id in themes.choices(force=True):
            t = themes.get(theme_id)
            p = t["palette"]
            with self.subTest(theme=theme_id):
                # Body text has to be readable on the page and on a card.
                for fg, bg in (("TEXT_FG", "WINDOW_BG"), ("TEXT_FG", "CARD_BG"),
                               ("TEXT_FG", "PANEL_BG"),
                               ("TEXT_FG", "BUTTON_BG")):
                    self.assertGreaterEqual(
                        _contrast(p[fg], p[bg]), 4.5,
                        "%s on %s is unreadable in %s" % (fg, bg, theme_id))
                # A secondary label has to be dimmer than body text without
                # disappearing — it is what carries the tile caption hierarchy.
                self.assertGreaterEqual(_contrast(p["SUBTLE_FG"], p["WINDOW_BG"]),
                                        3.0, "SUBTLE_FG vanishes in " + theme_id)
                self.assertGreaterEqual(_contrast(p["SUBTLE_FG"], p["TEXT_FG"]),
                                        1.5, "SUBTLE_FG reads as body text in "
                                        + theme_id)
                # A button has to be findable, and its hover has to register.
                self.assertGreaterEqual(_contrast(p["BUTTON_BG"], p["WINDOW_BG"]),
                                        1.2, "buttons vanish in " + theme_id)
                self.assertGreaterEqual(
                    _contrast(p["BUTTON_ACTIVE"], p["BUTTON_BG"]), 1.15,
                    "hover is invisible in " + theme_id)
                # ...and the accent has to read as a line ON the page, not
                # only as a fill behind white text. Our single ACCENT does
                # both jobs — hover rings and focus borders as well as button
                # fills — and Jellyfin blue cannot do the first one on a pale
                # background, which is why the translated light themes take
                # jf-web's $primary-dark instead.
                self.assertGreaterEqual(_contrast(p["ACCENT"], p["WINDOW_BG"]),
                                        3.0, "ACCENT vanishes in " + theme_id)

    # NOTE: passing everything above is necessary and NOT sufficient. It
    # checks the palette; it cannot see that most of the browser never asks
    # for it. See test_shell_library's rendered-scene check, which is what
    # actually catches a theme the widgets ignore.


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
        self.assertFalse(d["rounded"])    # square cards (the art still crops)
        self.assertFalse(d["accent_buttons"])   # plain top-bar buttons
        self.assertEqual(d["poster_scale"], 1.0)
        # None, not 24: an unset heading size means "follow the type
        # scale", whose HEADING tier is 24 at the stock base. Pinning the
        # number here would opt every theme's headings out of the user's
        # text-size multiplier.
        self.assertIsNone(d["heading_size"])
        from jellyfin_mpv_shim.mpvtk import theme as tk
        self.assertEqual(tk.size("HEADING"), 24)
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


class GradientAndHudAccentTest(unittest.TestCase):
    """The two keys that reach surfaces a palette alone cannot."""

    def tearDown(self):
        theme.apply("default")

    def test_gradient_stops_are_sorted_and_deduplicated(self):
        """The renderer walks stops in order, and a zero-length segment would
        divide by nothing. A theme file is hand-written, so neither can be
        assumed."""
        t = themes.resolve({"window_gradient": [[1.0, "03215f"],
                                                [0.0, "#0f3562"],
                                                [0.5, "1162a4"]]})
        self.assertEqual(t["window_gradient"],
                         [(0.0, "0f3562"), (0.5, "1162a4"), (1.0, "03215f")])

    def test_a_malformed_gradient_is_dropped_not_half_applied(self):
        for bad in ([[0.0, "zz"]], [[0.0, "000000"]], "nope",
                    [[2.0, "000000"], [0.0, "ffffff"]], [["a", "000000"]]):
            with self.subTest(value=bad):
                with self.assertLogs("mpvtk_browser.themes", "WARNING"):
                    t = themes.resolve({"window_gradient": bad})
                self.assertIsNone(t["window_gradient"])

    def test_no_gradient_means_a_flat_fill(self):
        theme.apply("default")
        self.assertIsNone(theme.window_gradient())
        self.assertIsNone(theme.topbar_gradient())

    def test_the_translated_themes_carry_jf_webs_own_gradients(self):
        self.assertTrue(themes.get("jf-wmc")["window_gradient"])
        self.assertTrue(themes.get("jf-purplehaze")["topbar_gradient"])

    def test_hud_accent_follows_the_palette_unless_pinned(self):
        """One accent everywhere is what makes a theme read as a theme, so
        the over-video accent defaults to it. jellyfin-web goes the other
        way and hardcodes its player slider to Jellyfin blue."""
        from jellyfin_mpv_shim.mpvtk import theme as tk

        cfg = theme.apply("nebula")
        theme.apply_to_toolkit(glow=cfg.get("glow", False))
        self.assertEqual(tk.ACCENT_ON_VIDEO, theme.ACCENT)

    def test_a_theme_can_pin_the_over_video_accent(self):
        """For an accent that cannot hold up against a moving picture."""
        from jellyfin_mpv_shim.mpvtk import theme as tk

        t = themes.resolve({"palette": {"ACCENT": "00729a"},
                            "hud_accent": "00a4dc"})
        self.assertEqual(t["hud_accent"], "00a4dc")
        tk.set_tokens(ACCENT=t["palette"]["ACCENT"],
                      ACCENT_ON_VIDEO=t["hud_accent"])
        self.assertEqual(tk.ACCENT, "00729a")
        self.assertEqual(tk.ACCENT_ON_VIDEO, "00a4dc")
        self.addCleanup(theme.apply_to_toolkit, False)


class RuntimeSwitchTest(unittest.TestCase):
    """Changing theme without a restart.

    Possible only because nothing caches a colour across frames: widget
    defaults resolve per construction, the renderer keeps its tokens in one
    replaceable table, and the strip store keys its baked bitmaps on the
    theme. Each of those was a deliberate choice; this is what they buy.
    """

    def setUp(self):
        import sys
        sys.path.insert(0, ".")
        from tests._shell_harness import FakeSource, _SyncPool
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()
        self.addCleanup(theme.apply, "default")
        self.addCleanup(theme.apply_to_toolkit, False)

    def test_switching_repaints_the_palette_and_the_toolkit(self):
        from jellyfin_mpv_shim.mpvtk import theme as tk

        self.b.set_theme("nebula")
        nebula = themes.get("nebula")["palette"]["ACCENT"]
        self.assertEqual(theme.ACCENT, nebula)
        # ...and the toolkit, which is what the widget defaults read.
        self.assertEqual(tk.ACCENT, nebula)
        self.assertEqual(tk.ON_SURFACE,
                         themes.get("nebula")["palette"]["TEXT_FG"])

    def test_switching_back_restores_every_colour(self):
        self.b.set_theme("nebula")
        self.b.set_theme("default")
        for key, value in themes.DEFAULT["palette"].items():
            with self.subTest(colour=key):
                self.assertEqual(getattr(theme, key), value)

    def test_composited_strips_do_not_survive_the_switch(self):
        """A strip has the theme's colours baked into its bitmap -- the
        resume bar, the watched tick, the accent. Serving a cached one after
        a switch would leave the old theme scattered across the rows."""
        before = self.b.strips.tag
        self.b.set_theme("nebula")
        self.assertNotEqual(self.b.strips.tag, before)

    def test_the_strip_cache_is_retagged_not_cleared(self):
        """clear() frees the backing buffers, and on libmpv those are read by
        ADDRESS by a compositor that is still running -- a segfault, not a
        leak. Retagging lets the old entries age out through the LRU, which
        only ever frees the least-recently-used one."""
        self.b.strips.strip([], geom=self.b.geom)
        n_before = len(self.b.strips._cache)
        self.b.set_theme("nebula")
        self.assertEqual(len(self.b.strips._cache), n_before,
                         "entries were dropped, so their buffers were freed")

    def test_the_browse_background_reaches_the_module_that_reads_it(self):
        """mpv paints the area behind the browser itself, via a module
        constant in player_window. It was being assigned on `player`, which
        only re-exports the mixin -- so the write landed on a name nothing
        reads and every theme's browse colour was silently lost. Nothing on
        screen said so: you got the stock dark grey, which looks like a
        theme that simply did not set one.
        """
        from jellyfin_mpv_shim import player_window

        for theme_id in ("nebula", "jf-wmc", "default"):
            with self.subTest(theme=theme_id):
                self.b.set_theme(theme_id)
                self.assertEqual(player_window.BROWSE_BG_HEX,
                                 themes.get(theme_id)["browse_bg"])

    def test_the_browse_background_is_set_at_startup_too(self):
        """Not only on a live switch: most people never change theme after
        picking one, so the constructor path is the one that matters."""
        from jellyfin_mpv_shim import player_window
        from jellyfin_mpv_shim.conf import settings

        old = getattr(settings, "theme", "default")
        settings.theme = "nebula"
        try:
            import sys
            sys.path.insert(0, ".")
            from tests._shell_harness import FakeSource, _SyncPool
            from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

            b = MpvtkBrowser(app=None, source=FakeSource())
            b._pool = _SyncPool()
            self.assertEqual(player_window.BROWSE_BG_HEX,
                             themes.get("nebula")["browse_bg"])
        finally:
            settings.theme = old

    def test_an_unknown_theme_falls_back_rather_than_raising(self):
        self.b.set_theme("no-such-theme")
        self.assertEqual(theme.ACCENT, themes.DEFAULT["palette"]["ACCENT"])


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

    def test_cover_size_moves_the_artwork_and_nothing_else(self):
        """Cover Size is a control over the ARTWORK. It used to scale the
        caption type and the badge type with it, which made it a second,
        unlabelled text-size setting -- so Extra Compact put captions under
        the floor `ui_text_min` exists to enforce, and Extra Large put a
        24px numeral inside a 22px badge disc (every offset in
        `_paint_decorations` is a fixed logical constant and does not scale).
        """
        for factor in (0.75, 1.4, 1.7):
            with self.subTest(factor=factor):
                big = POSTER_GEOM.scaled(factor)
                self.assertEqual(big.tile_w,
                                 round(POSTER_GEOM.tile_w * factor))
                for field in ("title_size", "sub_size", "badge_size",
                              "caption_h"):
                    self.assertEqual(
                        getattr(big, field), getattr(POSTER_GEOM, field),
                        "Cover Size moved %s, which is type, not artwork"
                        % field)

    def test_the_other_two_scales_still_move_the_type(self):
        """The half a careless fix breaks. Cover Size is not a text control;
        `ui_scale` and `ui_text_scale` are, and they reach a tile caption
        through `physical()` and `with_text_scale()` respectively -- which is
        the whole reason `scaled` does not have to."""
        scaled = POSTER_GEOM.with_text_scale(1.5)
        self.assertGreater(scaled.title_size, POSTER_GEOM.title_size)
        self.assertGreater(scaled.sub_size, POSTER_GEOM.sub_size)
        self.assertGreater(scaled.caption_h, POSTER_GEOM.caption_h,
                           "the band has to grow with the type or the "
                           "subtitle is clipped away")

    def test_cover_size_reaches_the_landscape_tile(self):
        """The shape it did NOT reach, and not a rare one: episodes, home
        videos, Live TV listings and every library whose median artwork is
        landscape are drawn in it. Turning covers up grew the film posters
        and left all of those at stock size.

        Through the BROWSER's own derivation, not through `TileGeom.scaled`.
        The geometry has always been able to scale; the bug was that
        `_derive_cover_size` never asked it to for this one shape, and a test
        that calls `LANDSCAPE_GEOM.scaled(1.4)` itself passes either way.
        """
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.conf import settings
        from tests._shell_harness import FakeSource

        was = settings.poster_scale
        self.addCleanup(setattr, settings, "poster_scale", was)
        shapes = {}
        for factor in (1.0, 1.4):
            settings.poster_scale = factor
            b = MpvtkBrowser(app=None, source=FakeSource())
            shapes[factor] = (b.geom.tile_w, b.geom_wide.tile_w,
                              b.geom_square.tile_w)
        for i, name in enumerate(("poster", "landscape", "square")):
            with self.subTest(shape=name):
                self.assertEqual(
                    shapes[1.4][i], round(shapes[1.0][i] * 1.4),
                    "Cover Size does not reach the %s tile" % name)

    def test_cover_size_still_leaves_the_stock_shape_identical_at_one(self):
        """`scaled` returns `self` at 1.0, and the derivation leans on it:
        `auto_geom` and its tests compare the landscape geometry by IDENTITY
        against the module singleton."""
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.conf import settings
        from tests._shell_harness import FakeSource

        was = settings.poster_scale
        self.addCleanup(setattr, settings, "poster_scale", was)
        settings.poster_scale = None
        b = MpvtkBrowser(app=None, source=FakeSource())
        self.assertIs(b.geom_wide, LANDSCAPE_GEOM)


class BadgeShadowTest(unittest.TestCase):
    """`badge_shadow`: tile badges as a mark plus a drop shadow, no pill."""

    def tearDown(self):
        theme.apply("default")

    def test_the_stock_look_keeps_its_pills(self):
        theme.apply("default")
        self.assertFalse(StripStore.shadowed_badges())

    def test_super_dark_asks_for_shadows(self):
        """The shipped theme the option exists for: near-black surfaces and
        dark buttons, where an accent-filled badge pill reads as a chip stuck
        to the poster rather than as a state.

        Its ACCENT is deliberately NOT its BUTTON_BG, which is what the first
        draft of this theme did. ACCENT does double duty here -- button fill
        AND hover ring, focus border and resume bar -- so a colour dark
        enough to be a button is a hairline nobody can see. Pinned, because
        the two look interchangeable in a palette file.
        """
        cfg = theme.apply("superdark")
        self.assertTrue(StripStore.shadowed_badges())
        self.assertNotEqual(cfg["palette"]["ACCENT"],
                            cfg["palette"]["BUTTON_BG"])

    def _badge(self, theme_id):
        """``(pixels, that theme's own accent)`` for one painted badge.

        The accent is read from the theme being painted, not from DEFAULT.
        Asking whether Jellyfin blue is absent from a theme whose accent is
        grey is a question with the right answer for the wrong reason: it
        holds whether or not the pill was drawn.
        """
        from PIL import Image as PILImage, ImageDraw

        theme.apply(theme_id)
        img = PILImage.new("RGBA", (60, 60), (0, 0, 0, 0))
        StripStore._paint_glyph_badge(img, ImageDraw.Draw(img), 30, 30,
                                      "check", theme.ACCENT)
        return list(img.getdata()), theme.rgb(theme.ACCENT) + (255,)

    def test_a_shadowed_badge_draws_no_pill_but_still_marks_the_corner(self):
        """Both halves. "No pill" alone is satisfied by drawing nothing at
        all, which is the failure this option is one line away from."""
        stock, stock_accent = self._badge("default")
        self.assertIn(stock_accent, stock,
                      "the stock badge lost its accent disc")
        dark, dark_accent = self._badge("superdark")
        self.assertNotIn(dark_accent, dark, "badge_shadow still filled a pill")
        self.assertTrue(any(p[3] > 0 for p in dark),
                        "badge_shadow drew nothing at all")

    def test_the_padding_round_a_mark_covers_its_own_blur(self):
        """The halo is drawn INSIDE the layer, so the room left for it has to
        be derived from the blur rather than fixed. It was a flat 5px while
        sigma ran to 2 and more, and under-padding a Gaussian does not soften
        its edge -- it puts a straight one where the layer stops, which is
        the squared-off corner every numeric badge was carrying."""
        for size in (10, 14, 16, 20, 23, 40, 64):
            with self.subTest(size=size):
                blur = max(1.0, size / StripStore.SHADOW_BLUR)
                drop = max(1, round(size / StripStore.SHADOW_DROP))
                self.assertGreaterEqual(StripStore.shadow_pad(size),
                                        3 * blur + drop)

    def _count_badge_shadow_top(self, count):
        """The topmost row of the card carrying any shadow ink, for a tile
        whose unwatched count is ``count`` — over a white poster, so anything
        that is not 255 is the badge's own halo."""
        import tempfile

        from PIL import Image as PILImage

        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM, Tile

        held = []

        class _Capture(StripStore):
            def _store(self, img):
                held.append(img)
                return "s", img.width, img.height, 1

        theme.apply("superdark")
        art = PILImage.new("RGBA", (300, 450), (255, 255, 255, 255))
        store = _Capture(tempfile.mkdtemp())
        self.addCleanup(store.shutdown)
        store.strip([Tile(key="t1", title="Film", poster=art, poster_tag="p",
                          badge=count)], POSTER_GEOM)
        img = held[0].convert("RGB")
        g = POSTER_GEOM.physical()
        # Inside the card's own 1px outline, right-hand half (where the
        # count sits), top quarter.
        for y in range(1, g.tile_h // 4):
            for x in range(g.tile_w // 2, g.tile_w - 1):
                if sum(img.getpixel((x, y))) / 3 < 250:
                    return y
        return None

    def test_a_counts_shadow_does_not_grow_with_its_digits(self):
        """The bug the tester saw as "cut-off on numeric badges".

        The padding round a mark is derived from its size, and the size was
        being taken as the LONGER side of the text — so "128" got three times
        the blur of "2" and a 36px-tall layer to hold it, which centred 17px
        below the top of the card does not fit and was clipped by the bitmap.

        A shadow is proportional to the WEIGHT of the ink, and every one of
        these is the same type at the same size. Asserted as equality across
        digit counts rather than against a number, because the number is
        whatever the type happens to measure.
        """
        tops = {n: self._count_badge_shadow_top(n) for n in (2, 12, 128)}
        self.assertNotIn(None, tops.values(), "no count badge drew a shadow")
        self.assertEqual(len(set(tops.values())), 1,
                         "the halo grows with the digit count: %r" % (tops,))

    def test_a_counts_shadow_is_not_clipped_by_the_top_of_the_card(self):
        """The consequence, asserted on its own: an unclipped halo has faded
        to nothing before it runs out of card, so the first row carrying its
        ink is strictly below the top edge."""
        for count in (2, 12, 128, 1280):
            with self.subTest(count=count):
                top = self._count_badge_shadow_top(count)
                self.assertIsNotNone(top)
                self.assertGreater(
                    top, 1,
                    "the badge's shadow runs off the top of the card")

    def test_a_badges_shadow_fades_out_before_the_edge_of_the_card(self):
        """The other half, and the one the padding alone does not give: a
        stack inset 17px from the corner puts the OUTERMOST badge's halo over
        the side of the card, where it is clipped by the bitmap instead of
        fading. A pill could sit there because a pill ends at its rim.

        Measured on the artwork's own boundary ring, over a white poster --
        the case with the most to lose, and the one where shadow ink pressed
        against the edge is unmistakable.
        """
        import tempfile

        from PIL import Image as PILImage

        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM, Tile

        held = []

        class _Capture(StripStore):
            def _store(self, img):
                held.append(img)
                return "s", img.width, img.height, 1

        theme.apply("superdark")
        art = PILImage.new("RGBA", (300, 450), (255, 255, 255, 255))
        store = _Capture(tempfile.mkdtemp())
        self.addCleanup(store.shutdown)
        # Every corner-badge kind at once, both stacks, and a three-digit
        # count -- the widest thing that goes up there.
        store.strip([Tile(key="t1", title="Film", poster=art, poster_tag="p",
                          watched=True, sources=2, downloaded=True,
                          kind="videocam", badge=128)], POSTER_GEOM)
        img = held[0].convert("RGB")
        g = POSTER_GEOM.physical()
        # Two pixels in from the card's own 1px outline, which is near-black
        # under every theme and is not what this is measuring.
        i = 2
        ring = ([(x, i) for x in range(i, g.tile_w - i)]
                + [(g.tile_w - 1 - i, y) for y in range(i, g.tile_h - i)]
                + [(i, y) for y in range(i, g.tile_h - i)])
        darkest = min(sum(img.getpixel(p)) / 3 for p in ring)
        self.assertGreater(
            darkest, 235,
            "a badge's shadow reaches the edge of the card, where it is cut "
            "off rather than faded out")

    def test_the_mark_stays_white_so_a_dark_accent_still_reads(self):
        """The reason the mark does not simply adopt the pill's colour: the
        first theme to ask for this has a grey accent on purpose, and an
        accent-coloured check with no pill behind it is a grey check on a
        photograph -- which is what the pill was there to prevent."""
        dark, dark_accent = self._badge("superdark")
        self.assertIn((255, 255, 255, 255), dark)
        self.assertNotIn(dark_accent, dark)


if __name__ == "__main__":
    unittest.main()
