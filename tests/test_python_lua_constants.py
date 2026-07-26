"""Constants duplicated across Python and renderer.lua.

Two values are computed on both sides of the mpv boundary and have to agree,
and until now the only thing holding them together was a "keep in sync"
comment:

* the heuristic char-width table — layout.py measures text to decide how
  much room a node needs, renderer.lua measures it again to place the glyphs.
  Drift means Python reserves one width and Lua draws another, which shows up
  as text that wraps a word early or overflows its box.
* SLIDER_PAD — hud.py maps a click position back to a seek time using the
  track inset renderer.lua drew the track with. Drift means the seek lands
  slightly off where you clicked, worst at the ends of the bar.

Both are *fallbacks*: measured font metrics replace them at runtime, so a
mismatch only bites on the path taken before (or without) metrics — which is
exactly the path nobody would notice being wrong.
"""

import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "jellyfin_mpv_shim")
LAYOUT = os.path.join(PKG, "mpvtk", "layout.py")
RENDERER = os.path.join(PKG, "mpvtk", "renderer.lua")
HUD = os.path.join(PKG, "mpvtk_browser", "hud.py")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _one(pattern, text, what):
    found = re.findall(pattern, text, re.M)
    if len(found) != 1:
        raise AssertionError(
            f"expected exactly one {what}, found {len(found)} — the "
            f"cross-check regex needs updating, not deleting")
    return found[0]


class TestCharWidthTable(unittest.TestCase):
    """layout.py's _NARROW/_WIDE/_*_W vs renderer.lua's char_w()."""

    def setUp(self):
        self.py = _read(LAYOUT)
        self.lua = _read(RENDERER)

    def _py_set(self, name):
        return set(ast.literal_eval(
            _one(rf"^{name} = set\((.*)\)$", self.py, name)))

    def _lua_set(self, name):
        # for c in ("iIlj..."):gmatch('.') do NARROW[c] = true end
        raw = _one(rf"for c in \((.*?)\):gmatch\('\.'\) do {name}\[c\]",
                   self.lua, f"lua {name}")
        return set(ast.literal_eval(raw))   # same escapes as Python here

    def _py_w(self, name):
        return float(_one(rf"^{name} = ([0-9.]+)$", self.py, name))

    def _lua_w(self, guard):
        return float(_one(rf"if {guard} then return ([0-9.]+) end",
                          self.lua, f"lua width for {guard}"))

    def test_narrow_characters_match(self):
        self.assertEqual(self._py_set("_NARROW"), self._lua_set("NARROW"))

    def test_wide_characters_match(self):
        self.assertEqual(self._py_set("_WIDE"), self._lua_set("WIDE"))

    def test_the_four_widths_match(self):
        self.assertEqual(self._py_w("_SPACE_W"), self._lua_w("c == ' '"))
        self.assertEqual(self._py_w("_NARROW_W"), self._lua_w(r"NARROW\[c\]"))
        self.assertEqual(self._py_w("_WIDE_W"), self._lua_w(r"WIDE\[c\]"))
        self.assertEqual(
            self._py_w("_DEFAULT_W"),
            float(_one(r"WIDE\[c\] then return [0-9.]+ end\n\s*return "
                       r"([0-9.]+)", self.lua, "lua default width")))

    def test_a_narrow_char_is_not_also_wide(self):
        self.assertEqual(self._py_set("_NARROW") & self._py_set("_WIDE"),
                         set())


class TestSliderPad(unittest.TestCase):
    """hud.py's _SLIDER_PAD vs renderer.lua's SLIDER_PAD."""

    def test_they_match(self):
        py = int(_one(r"^_SLIDER_PAD = (\d+)$", _read(HUD), "_SLIDER_PAD"))
        lua = int(_one(r"^local SLIDER_PAD = (\d+)$", _read(RENDERER),
                       "lua SLIDER_PAD"))
        self.assertEqual(py, lua,
                         "a click maps to a seek time using this inset; "
                         "drift puts the seek off where the user clicked")


class TestSkipButtonGeometry(unittest.TestCase):
    """hud.py's _SKIP_* vs renderer.lua's PHUD_SKIP_*.

    The Skip Intro/Credits button has two implementations — a scene node
    while the HUD is summoned, a renderer-drawn overlay while it is idle
    — and a live segment hands off between them whenever the bar comes
    up or auto-hides. renderer.lua rebuilds the widget's box by hand
    (Python sends node sizes for everything else, but the idle scene is
    empty), so every input to that box has to agree: drift makes the
    button hop or change size mid-segment.
    """

    def _pair(self, py_name, lua_name, why):
        py = int(_one(rf"^{py_name} = (\d+)$", _read(HUD), py_name))
        lua = int(_one(rf"^local {lua_name} = (\d+)$", _read(RENDERER),
                       f"lua {lua_name}"))
        self.assertEqual(py, lua, why)

    def test_bottom_inset_matches(self):
        self._pair("_SKIP_BOTTOM", "PHUD_SKIP_BOTTOM",
                   "the two copies must land in the same place")

    def test_type_size_matches(self):
        self._pair("_SKIP_SIZE", "PHUD_SKIP_FS",
                   "the label must be the same size in both copies")

    def test_padding_matches(self):
        self._pair("_SKIP_PAD", "PHUD_SKIP_PAD",
                   "the box must be the same size in both copies")

    def test_right_inset_matches(self):
        self._pair("_SKIP_RIGHT", "PHUD_SKIP_RIGHT",
                   "the two copies must sit the same distance from the "
                   "right edge -- the renderer-drawn copy carries the hit "
                   "rect, so a mismatch moves the clickable area away from "
                   "the button you can see")

    def _pair_str(self, py_name, lua_name, why):
        """Same as _pair, for the quoted colour constants."""
        py = _one(rf'^{py_name} = "([0-9a-fA-F]{{6}})"$', _read(HUD), py_name)
        lua = _one(rf"^local {lua_name} = '([0-9a-fA-F]{{6}})'$",
                   _read(RENDERER), f"lua {lua_name}")
        self.assertEqual(py.lower(), lua.lower(), why)

    def test_background_colour_matches(self):
        self._pair_str("_SKIP_BG", "PHUD_SKIP_BG",
                       "the handoff would flash a different-coloured box")

    def test_label_colour_matches(self):
        self._pair_str("_SKIP_FG", "PHUD_SKIP_FG",
                       "the handoff would flash a different-coloured label")

    def test_opacity_matches(self):
        self._pair("_SKIP_ALPHA", "PHUD_SKIP_ALPHA",
                   "the handoff would flash a more/less transparent box")

    def test_opacity_is_translucent_but_legible(self):
        """A guard on the value itself, not parity: 255 is opaque (the old
        look) and a very low value stops the label carrying over bright
        frames. Both copies are pinned to each other above, so checking
        one is enough."""
        alpha = int(_one(r"^_SKIP_ALPHA = (\d+)$", _read(HUD), "_SKIP_ALPHA"))
        self.assertLess(alpha, 255, "the button is meant to be translucent")
        self.assertGreater(alpha, 120, "too transparent to read over video")

    def test_the_colours_are_not_scaled(self):
        """_SCALE_BASE members are multiplied by the UI scale. A colour or
        an opacity in there becomes nonsense at any scale but 1."""
        found = re.search(r"local _SCALE_BASE = \{(.*?)\}", _read(RENDERER),
                          re.S)
        self.assertIsNotNone(found, "could not find _SCALE_BASE")
        base = found.group(1)
        for name in ("PHUD_SKIP_BG", "PHUD_SKIP_FG", "PHUD_SKIP_ALPHA"):
            self.assertNotIn(name, base,
                             f"{name} must not be scaled with the geometry")

    def test_line_height_matches_the_layout_engine(self):
        py = float(_one(r"^LINE_H = ([0-9.]+)", _read(LAYOUT), "LINE_H"))
        lua = float(_one(r"^local PHUD_SKIP_LINE_H = ([0-9.]+)$",
                         _read(RENDERER), "lua PHUD_SKIP_LINE_H"))
        self.assertEqual(py, lua,
                         "the overlay derives the label's height the way "
                         "layout.py does; drift changes the box height")


class TestBoldFactor(unittest.TestCase):
    """layout.py BOLD_FACTOR vs renderer.lua's.

    Only the regular face is measured, so bold width is derived from it by
    this factor. It was 1.04 -- measuring DejaVuSans against DejaVuSans-Bold
    gives ~1.12 -- which under-measured a bold heading by ~14px at size 17
    and let the next node overlap it.
    """

    def _values(self):
        py = float(_one(r"^BOLD_FACTOR = ([0-9.]+)$", _read(LAYOUT),
                        "BOLD_FACTOR"))
        lua = float(_one(r"^local BOLD_FACTOR = ([0-9.]+)$", _read(RENDERER),
                         "lua BOLD_FACTOR"))
        return py, lua

    def test_they_match(self):
        py, lua = self._values()
        self.assertEqual(py, lua,
                         "Python sizes the box, Lua draws the glyphs; drift "
                         "makes bold text overflow or overlap its neighbour")

    def test_it_is_not_the_old_underestimate(self):
        py, _lua = self._values()
        self.assertGreater(py, 1.10,
                           "1.04 is the value that caused the overlap")
        self.assertLess(py, 1.20, "implausibly wide for a bold face")


class TestStripCacheHoldsAWholeScene(unittest.TestCase):
    """strips.py's MAX_ENTRIES vs renderer.lua's MAX_OVERLAYS.

    Not a "must be equal" pair like the two above — an inequality, and the
    direction is the whole point. Freeing an evicted buffer is only safe
    because an LRU whose recency tracks the current build never drops
    anything visible: whatever is on screen was just requested. That argument
    collapses if a single scene can reference more bitmaps than the cache
    holds, because then a dense scene evicts entries it is still using — and
    on the libmpv path eviction FREES the buffer mpv reads by address.

    These were 48 and 63, i.e. the wrong way round.
    """

    def test_the_cache_can_hold_every_overlay_a_scene_may_use(self):
        strips = _read(os.path.join(PKG, "mpvtk_browser", "strips.py"))
        entries = int(_one(r"^    MAX_ENTRIES = (\d+)$", strips,
                           "MAX_ENTRIES"))
        overlays = int(_one(r"^local MAX_OVERLAYS = (\d+)$", _read(RENDERER),
                            "lua MAX_OVERLAYS"))
        self.assertGreater(
            entries, overlays,
            "a scene may reference %d bitmaps but the cache holds %d, so "
            "building one evicts buffers it is still displaying"
            % (overlays, entries))


if __name__ == "__main__":
    unittest.main()
