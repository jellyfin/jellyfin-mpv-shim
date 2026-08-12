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

import pathlib
import sys
import unittest

sys.argv = [sys.argv[0]]

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
