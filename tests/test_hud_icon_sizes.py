"""Every icon control on the playback HUD is the same size, and white.

Two defects, both **present at the baseline** and confirmed as such by
[iw] -- the type-scale work only made the first visible by moving the
control default from 20 to 17.

**Size.** The transport buttons pass `icon_size=30`; the four track
pickers (Chapters, Subtitles, Audio, Video Quality) are `trigger_icon`
Dropdowns, whose glyph the renderer derived from `node.size * 1.2` -- the
*type* size. So they drew at 24 beside their neighbours' 30, and then at
20 once the control default moved. A Dropdown's `size` means two
unrelated things and only one of them is type.

**Colour.** The chromeless trigger drew with `on_surface_muted` (grey),
an app-chrome token, while every other HUD button hardcodes `"eeeeee"`.
Over video the muted one is simply harder to read.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import pathlib
import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk import scaling                      # noqa: E402
from jellyfin_mpv_shim.mpvtk import theme as tk                  # noqa: E402
from jellyfin_mpv_shim.mpvtk import widgets                      # noqa: E402
from jellyfin_mpv_shim.mpvtk.layout import layout                # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import hud                  # noqa: E402


def _glyph(node_id, **kw):
    d = widgets.Dropdown(node_id, ["a", "b"], selected=0,
                         trigger_icon="bookmark", **kw)
    nodes, _h = layout(d, 400, 200)
    return next(n for n in nodes if n.get("t") == "dropdown")


class TriggerGlyphTest(unittest.TestCase):
    def tearDown(self):
        tk.set_type_scale(None)

    def test_an_explicit_icon_size_reaches_the_renderer(self):
        self.assertEqual(_glyph("d", icon_size=30).get("isz"), 30)

    def test_the_glyph_does_not_follow_the_type_scale(self):
        # The regression that made this visible: with the glyph derived
        # from `size`, moving the control default shrank four HUD buttons
        # and nothing else.
        sizes = []
        for base in (12, 17, 24, 40):
            tk.set_type_scale(base)
            sizes.append(_glyph("d", icon_size=30)["isz"])
        self.assertEqual(sizes, [30, 30, 30, 30])

    def test_nor_the_users_text_multiplier(self):
        """An icon is a control, not a line of text.

        Scaling the whole interface is `ui_scale`'s job; a text
        multiplier that also resized icons would be a second, partial
        copy of it. [iw]: "that would basically just be the dpi setting
        which we already have."
        """
        sizes = []
        for factor in (1.0, 1.5, 2.0):
            tk.set_type_scale(17, factor=factor)
            sizes.append(_glyph("d", icon_size=30)["isz"])
        self.assertEqual(sizes, [30, 30, 30])

    def test_it_matches_a_button_icon_at_every_setting(self):
        """The two families on the HUD bar, kept together.

        `Button(icon_size=)` -> `Icon` and `Dropdown(icon_size=)` ->
        trigger glyph must resolve identically, or the transport buttons
        and the track pickers drift apart -- which is the defect this
        file is named after, in its second form.
        """
        for factor in (1.0, 1.5, 2.0):
            with self.subTest(factor=factor):
                tk.set_type_scale(17, factor=factor)
                btn = widgets.Button("", icon="hd", icon_size=30)
                icon = next(c for c in btn.children
                            if type(c).__name__ == "Icon")
                self.assertEqual(_glyph("d", icon_size=30)["isz"], icon.w)

    def test_without_one_it_still_falls_back(self):
        # Unchanged for every other caller: no icon_size means the old
        # behaviour, so this fix is additive.
        tk.set_type_scale(None)
        self.assertIsNone(_glyph("d").get("isz"))


class TriggerGlyphUiScaleTest(unittest.TestCase):
    """The glyph follows `ui_scale`, which is the axis the rest of this
    file does not test -- and #721 is what lived in the gap.

    The three tests above assert `isz` is 30 no matter what the TYPE scale
    or the text multiplier does, which is right and is what this file is
    named after. But they all read `layout()` output *before*
    `scale_scene`, so none of them can see the logical -> physical
    conversion, and `isz` was in none of `scaling.py`'s tables. At
    `ui_scale` 2 the trigger's box went 47 -> 94 and the glyph stayed 30:
    three HUD controls drawing a small icon in a big hit box, on exactly
    the HiDPI displays the setting exists for.
    """

    def tearDown(self):
        tk.set_type_scale(None)
        scaling.set_scale(1.0)

    @staticmethod
    def _physical(scale, icon_size=30):
        """The dropdown trigger and a Button's icon, in PHYSICAL px.

        Laid out inside a Row because a widget laid out alone stretches to
        the viewport, and the trigger's box is the number under test.
        """
        scaling.set_scale(scale)
        try:
            row = widgets.Row([
                widgets.Dropdown("dd", ["a", "b"], trigger_icon="bookmark",
                                 icon_size=icon_size),
                widgets.Button("", icon="hd", icon_size=icon_size, id="btn",
                               on_click=lambda: None),
            ], gap=6, align="center")
            nodes, _h = layout(row, 900, 400)
            scaling.scale_scene(nodes)
            dd = next(n for n in nodes if n.get("t") == "dropdown")
            icon = next(n for n in nodes if n.get("t") == "icon")
            return dd, icon
        finally:
            scaling.set_scale(1.0)

    def test_the_glyph_grows_with_the_interface(self):
        for scale in (1.0, 1.5, 2.0, 3.5):
            with self.subTest(scale=scale):
                dd, _icon = self._physical(scale)
                self.assertAlmostEqual(dd["isz"], 30 * scale, delta=1.0)

    def test_the_glyph_still_fits_the_box_it_sits_in(self):
        """The visible symptom, stated as a ratio rather than a number.

        The renderer centres the glyph in the trigger
        (`renderer.lua`: `ex + (node.w - isz) / 2`), so what the eye reads
        is `isz / w`. That ratio is what went from 0.64 to 0.32.
        """
        base = None
        for scale in (1.0, 1.5, 2.0, 3.5):
            with self.subTest(scale=scale):
                dd, _icon = self._physical(scale)
                ratio = dd["isz"] / dd["w"]
                if base is None:
                    base = ratio
                self.assertAlmostEqual(ratio, base, delta=0.03)

    def test_it_still_matches_a_button_icon_after_conversion(self):
        """The same claim `test_it_matches_a_button_icon_at_every_setting`
        makes about the type scale, one axis over -- and this is the one
        that has to survive rounding, since a Button's icon goes through
        `_PX_KEYS` and must land on the same integer."""
        for scale in (1.0, 1.5, 2.0, 3.5):
            with self.subTest(scale=scale):
                dd, icon = self._physical(scale)
                self.assertEqual(dd["isz"], icon["w"])

    def test_an_odd_size_rounds_the_way_a_box_does(self):
        """`isz` is a BOX, not a font size: 17 at 1.5x is 25.5, and the
        renderer floors whatever it is handed. Rounded (26) it matches the
        Button's icon; left exact (25.5) the renderer floors to 25 and the
        two families are one pixel apart again -- which is the bug this
        file exists for, in miniature. This is why `isz` belongs in
        `_PX_KEYS` and not in `_EXACT_KEYS`.
        """
        dd, icon = self._physical(1.5, icon_size=17)
        self.assertEqual(dd["isz"], icon["w"])
        self.assertEqual(dd["isz"], int(dd["isz"]), "a box is a whole pixel")


class HudUsesOneSizeTest(unittest.TestCase):
    """The HUD hands every icon control the same number."""

    STATE = {
        "has_media": True,
        "audio": [{"id": 1, "label": "A", "selected": True},
                  {"id": 2, "label": "B"}],
        "subtitles": [{"id": 0, "label": "None", "selected": True},
                      {"id": 1, "label": "En"}],
        "quality": {"options": [{"id": "a", "label": "Auto",
                                 "selected": True},
                                {"id": "b", "label": "720"}]},
    }
    TIERS = {"chapters": True, "quality": True, "audio": True,
             "subs": True, "fav": True}

    def _pickers(self, size):
        return [p for p in hud._pickers(
            None, dict(self.STATE), 0,
            [{"time": 0, "title": "One"}, {"time": 90, "title": "Two"}],
            self.TIERS, size) if getattr(p, "trigger_icon", None)]

    def test_every_picker_takes_the_size_it_is_given(self):
        got = self._pickers(30)
        self.assertTrue(got, "no pickers built; the fixture has drifted")
        for p in got:
            with self.subTest(p.id):
                self.assertEqual(p.icon_size, 30)

    def test_it_is_the_same_size_the_buttons_use(self):
        """`HUD_ICON` is the one number, so the two families cannot drift
        apart again -- which is how they were 30 and 24 to begin with."""
        src = pathlib.Path(hud.__file__).read_text()
        self.assertIn("icon_size=HUD_ICON", src,
                      "the transport buttons no longer use HUD_ICON")
        self.assertIn("picker_icon = sz(HUD_ICON)", src,
                      "the pickers no longer use HUD_ICON")


class TriggerColourTest(unittest.TestCase):
    def test_a_chromeless_trigger_is_not_drawn_muted(self):
        """Read from renderer.lua: the colour is chosen there, and there
        is no scene field to assert on."""
        src = (pathlib.Path(hud.__file__).parent.parent
               / "mpvtk" / "renderer.lua").read_text()
        block = src[src.index("chromeless icon trigger"):]
        block = block[:block.index("return")]
        # The draw call, not the whole block: the comment beside it names
        # the token it replaced, so scanning the block matches the prose
        # and passes (or fails) for the wrong reason.
        call = block[block.index("draw_icon_path(ass, node.ticon"):]
        self.assertIn("state.tok.on_surface,", call,
                      "the HUD's track pickers are drawn with a muted "
                      "chrome token over video")
        self.assertNotIn("on_surface_muted", call)


if __name__ == "__main__":
    unittest.main()
