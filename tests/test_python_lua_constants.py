"""Constants duplicated across Python and renderer.lua.

Some values are computed on both sides of the mpv boundary and have to
agree; the only thing that used to hold them together was a "keep in sync"
comment.

The heuristic char-width table is the main one — layout.py measures text to
decide how much room a node needs, renderer.lua measures it again to place
the glyphs. Drift means Python reserves one width and Lua draws another,
which shows up as text that wraps a word early or overflows its box. It is a
*fallback*: measured font metrics replace it at runtime, so a mismatch only
bites on the path taken before (or without) metrics — which is exactly the
path nobody would notice being wrong.

The Skip button's geometry is the other: two implementations of one widget
that hand off to each other mid-segment.

SLIDER_PAD used to be here too — hud.py positioned the scrub-preview bubble
with its own copy of the renderer's track inset. That bubble is drawn in
renderer.lua now, so the inset exists in one place and there is nothing left
to cross-check.
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


class TestGamepadWireContract(unittest.TestCase):
    """The gamepad table crosses the mpv boundary as JSON, and each side
    pins its own end with its own literal.

    `gamepad.py` names the kinds symbolically (`gamepad.SEEK`) and every
    Python test uses those names; `renderer.lua` branches on the bare
    strings. So renaming one of those constants leaves BOTH suites green
    while the renderer's if/elseif chain falls through to its `else` -- and
    the else is `keypress`, so the right stick would start issuing
    `keypress up`, which during playback is mpv's own arrow seek: no
    `use_web_seek`, no SyncPlay awareness, and over a hidden HUD it summons
    the bar instead. Silent, and exactly the class of bug this file is for.

    Same for the event names and the payload keys: Lua sends
    `{t='gpseek', dir=...}` and app.py reads `evt.get("dir")`, with the
    toolkit test feeding a dict of its own.

    Assertions go through `assertTrue(needle in text)` rather than
    `assertIn`: the haystack is a 265 KB source file and assertIn puts the
    whole of it in the failure message.
    """

    renderer = _read(RENDERER)
    app = _read(os.path.join(PKG, "mpvtk", "app.py"))

    def _has(self, needle, where, why):
        self.assertTrue(needle in where, "%s (looked for %r)" % (why, needle))

    def test_the_dispatched_kinds_are_the_strings_the_renderer_matches(self):
        from jellyfin_mpv_shim import gamepad

        # SEEK and NAV have arms of their own. KEY deliberately does NOT --
        # it is the `else`, which is why a *typo* in any kind silently
        # becomes "treat the third field as a key name" rather than an
        # error. That is the reason this test exists at all.
        for const, name in ((gamepad.SEEK, "SEEK"), (gamepad.NAV, "NAV")):
            with self.subTest(kind=name):
                self._has("kind == '%s'" % const, self.renderer,
                          "renderer.lua has no arm for gamepad.%s == %r"
                          % (name, const))

    def test_the_key_kind_is_the_fallthrough_and_nothing_else(self):
        # If someone gives KEY an arm of its own, the else stops being
        # unreachable-by-design and a mistyped kind starts doing nothing
        # instead of something wrong. Either is fine -- but the comment in
        # gamepad.py says which one this is, so pin it.
        from jellyfin_mpv_shim import gamepad

        self.assertFalse("kind == '%s'" % gamepad.KEY in self.renderer,
                         "gamepad.KEY grew an explicit arm in renderer.lua; "
                         "the fall-through reasoning in gamepad.py and in "
                         "test_the_dispatched_kinds... needs revisiting")

    def test_every_kind_in_the_table_is_one_of_the_three(self):
        from jellyfin_mpv_shim import gamepad

        kinds = {kind for _k, kind, _a, _r in gamepad.bindings()}
        self.assertEqual(kinds, {gamepad.KEY, gamepad.SEEK, gamepad.NAV},
                         "a kind was added or dropped without review")

    def test_the_event_names_and_payload_keys_match(self):
        # Lua: send({ t = 'gpseek', dir = arg })  /  { t = 'gpnav', a = arg }
        for t, key in (("gpseek", "dir"), ("gpnav", "a")):
            with self.subTest(event=t):
                self._has("t = '%s', %s = arg" % (t, key), self.renderer,
                          "renderer.lua does not send %r carrying %r"
                          % (t, key))
                self._has('if t == "%s":' % t, self.app,
                          "mpvtk/app.py does not dispatch %r" % t)
                self._has('evt.get("%s")' % key, self.app,
                          "mpvtk/app.py does not read %r" % key)

    def test_the_repeat_interval_is_read_from_the_field_python_writes(self):
        # Python appends the interval as the FOURTH element; Lua reads b[4].
        from jellyfin_mpv_shim import gamepad

        row = gamepad.bindings()[0]
        self.assertEqual(len(row), 4)
        self._has("tonumber(b[4])", self.renderer,
                  "renderer.lua does not read the repeat interval from the "
                  "position gamepad.bindings() writes it to")


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
