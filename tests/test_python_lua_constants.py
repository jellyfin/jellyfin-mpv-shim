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

    def test_line_height_matches_the_layout_engine(self):
        py = float(_one(r"^LINE_H = ([0-9.]+)", _read(LAYOUT), "LINE_H"))
        lua = float(_one(r"^local PHUD_SKIP_LINE_H = ([0-9.]+)$",
                         _read(RENDERER), "lua PHUD_SKIP_LINE_H"))
        self.assertEqual(py, lua,
                         "the overlay derives the label's height the way "
                         "layout.py does; drift changes the box height")


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
