"""One base size, and every text size derived from it.

Surveyed rather than invented: every explicit `size=` in the browser was
counted (237 call sites) and grouped by what the text actually is. The app
turned out to be two populations that never met — content authored at
14–18, and **194 call sites rendering at 20 because nobody passed a size**.
Only 15 of 237 deliberate choices picked 20; the six buttons whose author
did pick one picked 15.

So the tiers here are named for the job, and the base is 17: the survey's
best fit (236 of 237 authored sizes land within a pixel) and the size the
app's own buttons use when somebody chose. **[iw]**: "I'd probably default
to the size we use for buttons as Normal."

`HEADING` landing on 24 — what `heading_size` has always been — is the
check that the ratios are real and not fitted to the data.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk import theme as tk                  # noqa: E402
from jellyfin_mpv_shim.mpvtk import widgets                      # noqa: E402


class ScaleTest(unittest.TestCase):
    def tearDown(self):
        tk.set_type_scale(None)

    def test_the_stock_scale_is_the_sizes_the_app_already_used(self):
        tk.set_type_scale(None)
        self.assertEqual(
            {t: tk.size(t) for t in tk.TYPE_SCALE},
            {"MICRO": 12, "CAPTION": 14, "SMALL": 15, "NORMAL": 17,
             "LARGE": 19, "TITLE": 22, "HEADING": 24, "PAGE": 26,
             "HERO": 29})

    def test_the_heading_tier_is_the_old_heading_size(self):
        # Independent check on the ratios: 24 was chosen years before this
        # scale existed, so the scale reproducing it is evidence rather
        # than coincidence.
        tk.set_type_scale(None)
        self.assertEqual(tk.size("HEADING"), 24)

    def test_every_tier_moves_with_the_base(self):
        tk.set_type_scale(34)      # exactly double
        for tier, ratio in tk.TYPE_SCALE.items():
            with self.subTest(tier):
                self.assertEqual(tk.size(tier), round(34 * ratio))

    def test_the_order_never_inverts(self):
        # A scale whose tiers cross over at some base is not a scale. Ints
        # are rounded, so this is not free.
        for base in range(8, 61):
            tk.set_type_scale(base)
            got = [tk.size(t) for t in
                   ("MICRO", "CAPTION", "SMALL", "NORMAL", "LARGE",
                    "TITLE", "HEADING", "PAGE", "HERO")]
            with self.subTest(base=base):
                self.assertEqual(got, sorted(got), "tiers crossed at %d" % base)

    def test_an_unknown_tier_raises(self):
        # Never a silent default: a typo'd tier rendering as body text
        # looks almost right, which is the worst outcome.
        with self.assertRaises(KeyError):
            tk.size("subtitle")

    def test_a_nonsense_base_falls_back(self):
        for bad in (0, -5, None, "big", float("nan")):
            with self.subTest(bad=bad):
                tk.set_type_scale(bad)
                self.assertGreater(tk.size("NORMAL"), 0)

    def test_setting_it_is_wholesale(self):
        # Like set_tokens: a theme that says nothing must get the default
        # back, not the previous theme's value.
        tk.set_type_scale(30)
        tk.set_type_scale(None)
        self.assertEqual(tk.size("NORMAL"), tk.DEFAULT_BASE_SIZE)


class WidgetDefaultsTest(unittest.TestCase):
    """The widgets take their size from the scale, at construction.

    Not in the signature: a default argument is evaluated once, at import,
    so `size=theme.size("NORMAL")` would freeze whatever the scale was
    before the app ever set it.
    """

    def tearDown(self):
        tk.set_type_scale(None)

    CONTROLS = ("Button", "TextBox", "Checkbox", "Dropdown", "Menu")

    @staticmethod
    def _text_size(widget):
        """The size the widget actually renders its label at.

        `Button` and `Checkbox` keep no `size` of their own -- they hand it
        to a child `Text` -- so asking the widget would raise, and asking
        only the widgets that do store one would quietly skip the two the
        report was about.
        """
        if getattr(widget, "size", None) is not None:
            return widget.size
        seen = list(getattr(widget, "children", []) or [])
        while seen:
            child = seen.pop(0)
            if type(child).__name__ == "Text" and getattr(child, "text", ""):
                return child.size
            seen.extend(getattr(child, "children", []) or [])
        raise AssertionError("no text found in %s" % type(widget).__name__)

    def _build(self, name):
        cls = getattr(widgets, name)
        if name == "Checkbox":
            return cls("x", False, on_toggle=lambda: None)
        if name == "Dropdown":
            return cls("d", ["a"], selected=0)
        if name == "Menu":
            return cls("m", ["a"], 0, 0)
        if name == "TextBox":
            return cls("t", "")
        return cls("x")

    def test_controls_are_normal(self):
        tk.set_type_scale(None)
        for name in self.CONTROLS:
            with self.subTest(name):
                self.assertEqual(self._text_size(self._build(name)),
                                 tk.size("NORMAL"))

    def test_they_follow_a_theme_swap(self):
        # The bug this shape prevents: sizes frozen at import would ignore
        # every theme after the first.
        tk.set_type_scale(40)
        for name in self.CONTROLS:
            with self.subTest(name):
                self.assertEqual(self._text_size(self._build(name)),
                                 tk.size("NORMAL"))

    def test_an_explicit_size_still_wins(self):
        tk.set_type_scale(None)
        self.assertEqual(self._text_size(widgets.Button("x", size=11)), 11)

    def test_a_control_is_no_longer_larger_than_body_text(self):
        """The reported symptom.

        Controls used to default to 20 while body text was authored at
        14-18, so a dropdown read a whole tier larger than the label
        beside it. 194 call sites arrived at 20 by not passing a size.
        """
        tk.set_type_scale(None)
        self.assertLessEqual(
            self._text_size(widgets.Dropdown("d", ["a"], selected=0)),
            tk.size("LARGE"))


class UserOverrideTest(unittest.TestCase):
    """`ui_text_scale` multiplies the base, so tiers keep their ratios."""

    def tearDown(self):
        tk.set_type_scale(None)

    def _apply(self, factor):
        from jellyfin_mpv_shim.mpvtk_browser import theme

        s = mock.Mock()
        s.ui_text_scale = factor
        with mock.patch("jellyfin_mpv_shim.conf.settings", s):
            theme.apply_to_toolkit()
        return {t: tk.size(t) for t in tk.TYPE_SCALE}

    def test_it_scales_every_tier_together(self):
        base = self._apply(1.0)
        big = self._apply(1.5)
        for tier in tk.TYPE_SCALE:
            with self.subTest(tier):
                self.assertAlmostEqual(big[tier] / base[tier], 1.5, delta=0.08)

    def test_headings_are_not_left_behind(self):
        """`heading_size` is its own theme key, so it had to be taught to
        follow the scale -- otherwise the one control a user reaches for to
        make text bigger would leave every section heading where it was."""
        from jellyfin_mpv_shim.mpvtk_browser import theme

        self._apply(1.0)
        small = theme.heading_size()
        self._apply(1.5)
        self.assertGreater(theme.heading_size(), small)

    def test_a_pinned_heading_size_still_wins(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme

        with mock.patch.object(theme, "_active", {"heading_size": 40}):
            self.assertEqual(theme.heading_size(), 40)

    def test_a_broken_value_does_not_break_the_ui(self):
        for bad in (0, -1, None, "large"):
            with self.subTest(bad=bad):
                got = self._apply(bad)
                self.assertEqual(got["NORMAL"], tk.DEFAULT_BASE_SIZE)


class ReadabilityFloorTest(unittest.TestCase):
    """`ui_text_min` raises the smallest text, which a multiplier cannot.

    A scale moves everything, so the hardest thing on screen to read stays
    the hardest thing on screen to read -- a guide badge at 0.70x base is
    still the smallest text there is. A floor compresses the bottom of the
    scale instead, which is what low vision actually needs.
    """

    def tearDown(self):
        tk.set_type_scale(None)

    def test_it_raises_the_small_tiers_and_leaves_the_rest(self):
        tk.set_type_scale(17, minimum=15)
        self.assertEqual(tk.size("MICRO"), 15)
        self.assertEqual(tk.size("CAPTION"), 15)
        self.assertEqual(tk.size("NORMAL"), 17)
        self.assertEqual(tk.size("HERO"), 29)

    def test_it_reaches_an_explicit_size_too(self):
        # The point of flooring where widgets RESOLVE rather than where
        # tiers are computed: several hundred call sites still pass a
        # literal, and 11px is exactly what a floor is for.
        tk.set_type_scale(17, minimum=16)
        self.assertEqual(widgets.Text("x", size=11).size, 16)

    def test_it_raises_the_widgets_that_take_a_default(self):
        """The bug this test exists for.

        The floor was first capped at the BASE, so with a base of 17 a
        floor of 18 came out as 17 and the setting appeared to do nothing
        to buttons or body text -- the two things it is most for. [iw]:
        "Minimum text size seems to not be affecting buttons among other
        things."
        """
        tk.set_type_scale(17, minimum=20)
        self.assertEqual(tk.min_size(), 20)
        for name in ("Button", "Checkbox", "Dropdown"):
            with self.subTest(name):
                w = (widgets.Button("x") if name == "Button" else
                     widgets.Checkbox("x", False) if name == "Checkbox" else
                     widgets.Dropdown("d", ["a"], selected=0))
                got = getattr(w, "size", None)
                if got is None:
                    got = next(c.size for c in w.children
                               if type(c).__name__ == "Text" and c.text)
                self.assertEqual(got, 20)

    def test_a_floor_past_the_top_tier_is_capped_there(self):
        # A hand-edited absurdity is clamped rather than honoured, but the
        # cap is the LARGEST tier -- capping lower is what broke it.
        tk.set_type_scale(17, minimum=9999)
        self.assertLessEqual(tk.size("MICRO"), tk.size("HERO"))
        self.assertGreater(tk.size("MICRO"), 17)

    def test_the_floor_is_measured_against_scaled_text(self):
        # With a 2x multiplier nothing is below 34 anyway, so an 18px floor
        # must not drag anything DOWN or clamp the multiplier.
        tk.set_type_scale(17, minimum=18, factor=2.0)
        self.assertEqual(tk.size("NORMAL"), 34)


class EverySizeScalesTest(unittest.TestCase):
    """The multiplier reaches literals, not just tiers.

    Folding it into the base was the first attempt and it was wrong: the
    tiers scaled and the several hundred call sites still passing a
    literal did not, so turning text up enlarged the controls and left the
    body text behind -- the very mismatch the scale was built to remove.
    """

    def tearDown(self):
        tk.set_type_scale(None)

    def test_a_literal_size_follows_the_multiplier(self):
        tk.set_type_scale(17, factor=2.0)
        self.assertEqual(widgets.Text("x", size=13).size, 26)

    def test_a_tier_scales_exactly_once(self):
        # Both the tier and the literal path multiply; a tier must not go
        # through it twice.
        tk.set_type_scale(17, factor=2.0)
        self.assertEqual(tk.size("NORMAL"), 34)

    def test_the_ratios_survive(self):
        tk.set_type_scale(None)
        stock = {t: tk.size(t) for t in tk.TYPE_SCALE}
        tk.set_type_scale(17, factor=1.5)
        for tier, was in stock.items():
            with self.subTest(tier):
                self.assertAlmostEqual(tk.size(tier) / was, 1.5, delta=0.09)


class TileCaptionTest(unittest.TestCase):
    """Tile captions are geometry, not tiers, and were left behind.

    They are painted into the strip bitmap by Pillow and already scale
    with Cover Size, so they never went through `theme.size()` -- which is
    why turning text up moved everything except the tiles [iw].
    """

    def _geom(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        return POSTER_GEOM

    def test_the_caption_band_grows_with_the_text(self):
        """`caption_h` is a fixed band and `_paint_caption` lays out title,
        a 7px gap, then subtitle inside it. Scaling the type without the
        band clips the subtitle away entirely."""
        g = self._geom()
        for factor in (1.25, 1.5, 2.0, 3.0):
            with self.subTest(factor):
                s = g.with_text_scale(factor)
                self.assertLessEqual(s.title_size + 7 + s.sub_size,
                                     s.caption_h)

    def test_it_keeps_the_slack_the_stock_band_has(self):
        g = self._geom()
        slack = g.caption_h - (g.title_size + g.sub_size)
        s = g.with_text_scale(2.0)
        self.assertGreaterEqual(
            s.caption_h - (s.title_size + s.sub_size), slack)

    def test_the_floor_reaches_captions(self):
        s = self._geom().with_text_scale(1.0, minimum=18)
        self.assertGreaterEqual(s.title_size, 18)
        self.assertGreaterEqual(s.sub_size, 18)

    def test_a_scale_of_one_changes_nothing(self):
        g = self._geom()
        s = g.with_text_scale(1.0)
        self.assertEqual((s.title_size, s.sub_size, s.caption_h),
                         (g.title_size, g.sub_size, g.caption_h))


class ControlGeometryTest(unittest.TestCase):
    """A type scale must not resize the controls.

    `size` on a Dropdown means two unrelated things: the type size of the
    popup's rows, and -- for an icon-only trigger -- the dimensions of the
    button. Only the first belongs on a type scale. Tying the second to it
    shrank the playback HUD's Chapters, Subtitles, Audio and Video-quality
    buttons from 38px to 32px when the control default moved 20 -> 17,
    while every other HUD button stayed put, because those are `Icon`s
    sized on their own. [iw]: "Some of the HUD buttons got smaller but most
    didn't."
    """

    def tearDown(self):
        tk.set_type_scale(None)

    def _trigger(self):
        return widgets.Dropdown("d", ["a"], selected=0,
                                trigger_icon="bookmark")

    def test_the_trigger_box_does_not_follow_the_type_scale(self):
        tk.set_type_scale(None)
        was = (self._trigger().w, self._trigger().h)
        for base in (12, 17, 24, 40):
            with self.subTest(base=base):
                tk.set_type_scale(base)
                d = self._trigger()
                self.assertEqual((d.w, d.h), was)

    def test_nor_the_users_text_multiplier(self):
        # `ui_scale` resizes the interface; `ui_text_scale` resizes the
        # words. A control that followed both would be a partial
        # duplicate of the first. [iw]: "that would basically just be the
        # dpi setting which we already have."
        tk.set_type_scale(None)
        was = self._trigger().w
        for factor in (1.5, 2.0):
            with self.subTest(factor=factor):
                tk.set_type_scale(17, factor=factor)
                self.assertEqual(self._trigger().w, was)

    def test_the_popup_type_still_does(self):
        # The half that SHOULD move: the rows in the open list are text.
        tk.set_type_scale(None)
        small = self._trigger().size
        tk.set_type_scale(34)
        self.assertGreater(self._trigger().size, small)

    def test_an_explicit_size_still_wins(self):
        d = widgets.Dropdown("d", ["a"], selected=0,
                             trigger_icon="bookmark", w=50, h=50)
        self.assertEqual((d.w, d.h), (50, 50))


if __name__ == "__main__":
    unittest.main()
