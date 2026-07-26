"""UI scale factor: the logical/physical boundary.

The drift test below is the important one. scaling.scale_scene converts a
scene with an explicit table of pixel keys rather than a recursive walk
over anything numeric -- because a slider's min/max/value and an image's
iw/ih are numbers that must NOT be touched. That table is only correct as
long as nobody adds a new pixel-valued node field without updating it, and
the failure mode is invisible at 1x, which is where everything is
developed. So: lay the same tree out at 1x and 2x and require every
numeric field to either scale or be explicitly known not to.
"""

import unittest

from jellyfin_mpv_shim.mpvtk import scaling
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk.widgets import (
    Box, Button, Checkbox, Column, Dropdown, Gradient, Icon, Image, ImageMap,
    Menu, Progress, Row, Scroll, Slider, Text, TextBox,
)


# Numeric node fields that are deliberately NOT pixel geometry.
NOT_PIXELS = {
    "iw", "ih",          # physical bitmap dims -- the boundary itself
    "min", "max", "value",  # slider domain
    "marks", "ranges",   # slider domain, normalized
    "a", "a1", "a2",     # alphas
    "v",                 # content version
    "sel",               # dropdown index
}


def _tree():
    """A tree touching every widget that emits numeric geometry.

    Images are covered separately: a producer legitimately rasterizes a
    BIGGER bitmap at 2x, so iw/ih differ between the two scenes for a
    reason that has nothing to do with scale_scene.
    """
    return Column([
        Text("hello", size=22),
        Text("wrapped text here", size=15, wrap=True, w=120),
        Box([Text("in a box", size=18)], w=200, h=60, radius=8,
            border="ff0000", border_w=3, pad=10),
        Row([
            Button("ok", size=20, radius=6),
            Checkbox("check", checked=True),
        ], gap=12),
        Slider("sl", min=0, max=100, value=42, w=300, h=20,
               marks=[0.25, 0.5], ranges=[[0.1, 0.4]]),
        Progress(frac=0.5, w=240, h=8),
        Gradient(top=0, bottom=200, w=100, h=40),
        Dropdown("dd", ["a", "b"], selected=1, size=18, w=160),
        # An icon-trigger dropdown is what emits `pw` (the popup is sized
        # to its items rather than to the trigger).
        Dropdown("dd2", ["alpha", "beta"], selected=0, size=18,
                 trigger_icon="menu"),
        TextBox("tb", text="typed", size=17, w=220, h=34),
        Icon("play", size=24),
        # A Menu is what emits `rh`, its row height. That field used to
        # ship as "ih" and silently inherited the img exclusion, so the
        # tree must keep exercising it.
        Menu("menu", ["Play", "Mark Watched", "Delete"], x=10, y=20,
             size=17),
        # A Scroll is what emits cw/ch.
        Scroll(Column([Text("row %d" % i, size=16) for i in range(20)],
                      gap=6),
               "y", id="scr", w=300, h=120, scrollbar=True),
    ], gap=14, pad=16)


def _scene(scale, w=800, h=600):
    scaling.set_scale(scale)
    try:
        nodes, _handlers = layout(_tree(), w, h)
        scaling.scale_scene(nodes)
        return {n["id"]: n for n in nodes}
    finally:
        scaling.set_scale(1.0)


class ScaleSceneDriftTest(unittest.TestCase):
    """Every numeric node field must scale, or be known not to."""

    def test_no_pixel_field_escapes_the_conversion(self):
        one = _scene(1.0)
        two = _scene(2.0)
        self.assertEqual(set(one), set(two), "same tree -> same node ids")

        unscaled = []
        for nid, n1 in one.items():
            n2 = two[nid]
            # Union, not n1's keys: a field emitted only at 2x would
            # otherwise never be looked at.
            for key in set(n1) | set(n2):
                v1, v2 = n1.get(key), n2.get(key)
                if key in NOT_PIXELS:
                    continue
                if not isinstance(v1, (int, float)) or isinstance(v1, bool):
                    continue
                if not isinstance(v2, (int, float)) or isinstance(v2, bool):
                    continue
                if v1 == 0:
                    continue  # 0 scales to 0; carries no information
                # Two independent signals. The magnitude check allows a
                # unit of double-rounding slack, but that slack SWALLOWS
                # small values -- an unscaled field whose 1x value is 1
                # lands within it (|1 - 2| = 1), and border_w defaults to
                # exactly 1. So also assert the field moved at all: for a
                # pixel value at 2x, staying put is the signature of not
                # being scaled.
                if v2 == v1:
                    unscaled.append((nid, key, v1, v2, "did not move"))
                elif abs(v2 - v1 * 2) > 1.51:
                    unscaled.append((nid, key, v1, v2, "not ~2x"))

        self.assertFalse(
            unscaled,
            "these numeric node fields did not scale from 1x to 2x. If a "
            "field is genuinely not pixel geometry, add it to NOT_PIXELS; "
            "otherwise add it to scaling._PX_KEYS: %r" % (unscaled,),
        )

    def test_domain_values_are_left_alone(self):
        two = _scene(2.0)
        slider = next(n for n in two.values() if n["t"] == "slider")
        self.assertEqual((slider["min"], slider["max"], slider["value"]),
                         (0, 100, 42))
        self.assertEqual(slider["marks"], [0.25, 0.5])

    def test_scale_1_is_a_no_op(self):
        """The 1x path must stay byte-identical -- it is what the rest of
        the suite exercises."""
        scaling.set_scale(1.0)
        nodes, _ = layout(_tree(), 800, 600)
        before = [dict(n) for n in nodes]
        scaling.scale_scene(nodes)
        self.assertEqual(before, nodes)

    def test_shared_hover_dicts_are_not_mutated_in_place(self):
        """hover dicts are often shared module constants; scaling one in
        place would compound on every frame."""
        shared = {"bc": "ff0000", "bw": 3}
        tree = Column([Box([], w=50, h=50, hover=shared, id="b1"),
                       Box([], w=50, h=50, hover=shared, id="b2")])
        scaling.set_scale(2.0)
        try:
            nodes, _ = layout(tree, 400, 400)
            scaling.scale_scene(nodes)
            scaling.scale_scene(nodes)   # a second frame over the same dict
            self.assertEqual(shared["bw"], 3, "the shared dict was mutated")
            for n in nodes:
                if n.get("hover"):
                    self.assertEqual(n["hover"]["bw"], 6)
        finally:
            scaling.set_scale(1.0)


class RasterBoundaryTest(unittest.TestCase):
    """Image widgets enforce that producers rasterized at the UI scale."""

    def tearDown(self):
        scaling.set_scale(1.0)

    def test_a_producer_that_forgot_to_scale_is_rejected(self):
        scaling.set_scale(2.0)
        with self.assertRaises(ValueError) as cm:
            Image("poster.bgra", 150, 225, w=150, h=225)   # 1x bitmap
        self.assertIn("did not rasterize", str(cm.exception))

    def test_a_correctly_rasterized_producer_is_accepted(self):
        scaling.set_scale(2.0)
        iw, ih = scaling.raster(150, 225)
        self.assertEqual((iw, ih), (300, 450))
        img = Image("poster.bgra", iw, ih, w=150, h=225)
        self.assertEqual((img.lw, img.lh), (150, 225))
        self.assertEqual((img.iw, img.ih), (300, 450))

    def test_imagemap_is_checked_too(self):
        scaling.set_scale(1.5)
        with self.assertRaises(ValueError):
            ImageMap("strip.bgra", 478, 271, w=478, h=271)

    def test_an_inherently_physical_image_derives_its_logical_box(self):
        """Trickplay frames are sized by the video, not by a logical box:
        no w/h declared, so the footprint is derived instead of checked."""
        scaling.set_scale(2.0)
        img = Image("frame.bgra", 320, 180)
        self.assertEqual((img.lw, img.lh), (160.0, 90.0))

    def test_layout_clamps_against_the_logical_footprint(self):
        """iw/ih are physical; clamping a logical w against them would mix
        spaces and shrink the image at scales below 1."""
        scaling.set_scale(2.0)
        iw, ih = scaling.raster(100, 100)
        nodes, _ = layout(Column([Image("x.bgra", iw, ih, w=100, h=100)]),
                          800, 600)
        scaling.scale_scene(nodes)
        img = next(n for n in nodes if n["t"] == "img")
        self.assertEqual((img["w"], img["h"]), (200, 200))
        self.assertEqual((img["iw"], img["ih"]), (200, 200))


class StripStoreScaleTest(unittest.TestCase):
    """End to end: the real compositor must satisfy the Image boundary."""

    def tearDown(self):
        scaling.set_scale(1.0)

    def _store(self):
        import tempfile
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore
        return StripStore(cache_dir=tempfile.mkdtemp(prefix="scaletest-"))

    def test_a_strip_bitmap_matches_its_logical_footprint(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM, Tile

        for s in (1.0, 1.5, 2.0):
            scaling.set_scale(s)
            store = self._store()
            e = store.strip([Tile(key="a", title="A"), Tile(key="b")],
                            POSTER_GEOM)
            self.assertEqual(
                (e["iw"], e["ih"]), scaling.raster(e["lw"], e["lh"]),
                "at %gx the bitmap and its logical box disagree, so the "
                "ImageMap below would raise" % s)
            # the construction the view actually performs
            ImageMap(e["src"], e["iw"], e["ih"], regions=e["regions"],
                     v=e["v"], w=e["lw"], h=e["lh"])

    def test_regions_stay_logical(self):
        """Regions are consumed by layout, which runs in logical space."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM, Tile

        scaling.set_scale(1.0)
        one = self._store().strip([Tile(key="a"), Tile(key="b")], POSTER_GEOM)
        scaling.set_scale(2.0)
        two = self._store().strip([Tile(key="a"), Tile(key="b")], POSTER_GEOM)
        self.assertEqual([r["x"] for r in one["regions"]],
                         [r["x"] for r in two["regions"]])
        self.assertEqual(two["iw"], 2 * one["iw"])


class _StubBackend:
    def __init__(self, hidpi=None):
        self._hidpi = hidpi

    def get_property(self, name):
        if name == "display-hidpi-scale" and self._hidpi is not None:
            return self._hidpi
        raise RuntimeError("property unavailable")

    def command(self, *a):
        pass


class _StubApp:
    """_resolve_scale only reaches self.backend, so this is enough."""

    def __init__(self, hidpi=None):
        self.backend = _StubBackend(hidpi)


class ResolveScaleTest(unittest.TestCase):
    """settings.ui_scale wins; null follows the display."""

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        self.settings = settings
        self._saved = getattr(settings, "ui_scale", None)

    def tearDown(self):
        self.settings.ui_scale = self._saved
        scaling.set_scale(1.0)

    def _resolve(self, hidpi=None):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        MpvtkApp._resolve_scale(_StubApp(hidpi))
        return scaling.scale()

    def test_an_explicit_setting_wins_over_the_display(self):
        self.settings.ui_scale = 1.5
        self.assertEqual(self._resolve(hidpi=2.0), 1.5)

    def test_null_follows_the_display(self):
        self.settings.ui_scale = None
        self.assertEqual(self._resolve(hidpi=2.0), 2.0)

    def test_null_on_a_backend_without_the_property_is_1(self):
        """Not every VO exposes display-hidpi-scale; 1.0 beats guessing."""
        self.settings.ui_scale = None
        self.assertEqual(self._resolve(hidpi=None), 1.0)


class BoundaryConversionTest(unittest.TestCase):
    """Physical values arriving from the renderer become logical here.

    The scroll offset is the one that actually broke: views window
    virtualized lists by comparing an offset against logical row heights,
    so a physical offset silently selects the wrong rows at any scale != 1
    (and the library scroller renders the wrong slice of the list).
    """

    def tearDown(self):
        scaling.set_scale(1.0)

    def _app(self, scroll=None, nodes=None):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        class _B:
            def get_property(self, name):
                return scroll

        app = MpvtkApp.__new__(MpvtkApp)   # no mpv, no loop
        app.backend = _B()
        app._nodes = nodes or []
        return app

    def test_scroll_offsets_are_logical(self):
        scaling.set_scale(2.0)
        app = self._app(scroll={"library": 400, "queue": 0})
        self.assertEqual(app.scroll_offsets(), {"library": 200.0, "queue": 0.0})

    def test_node_rect_is_logical(self):
        scaling.set_scale(2.0)
        app = self._app(nodes=[{"id": "hud-seek", "x": 100, "y": 40,
                                "w": 600, "h": 20, "t": "rect"}])
        r = app.node_rect("hud-seek")
        self.assertEqual((r["x"], r["y"], r["w"], r["h"]), (50.0, 20.0,
                                                            300.0, 10.0))

    def test_node_rect_does_not_mutate_the_scene(self):
        """The scene has already been pushed physical; handing out a
        converted copy must not corrupt it for the next frame."""
        scaling.set_scale(2.0)
        node = {"id": "n", "x": 100, "y": 40, "w": 600, "h": 20, "t": "rect"}
        app = self._app(nodes=[node])
        app.node_rect("n")
        self.assertEqual(node["x"], 100)

    def test_at_1x_the_scene_node_is_handed_back_as_is(self):
        scaling.set_scale(1.0)
        node = {"id": "n", "x": 100, "y": 40, "w": 600, "h": 20, "t": "rect"}
        app = self._app(nodes=[node])
        self.assertIs(app.node_rect("n"), node)


class ScaleCliTest(unittest.TestCase):
    """--scale overrides ui_scale for one run without persisting it."""

    def _parse(self, argv):
        from jellyfin_mpv_shim.args import _build_parser

        return _build_parser().parse_args(argv)

    def test_scale_parses_as_a_float(self):
        self.assertEqual(self._parse(["--scale", "1.5"]).ui_scale, 1.5)
        self.assertEqual(self._parse(["--scale", "2"]).ui_scale, 2.0)

    def test_absent_leaves_the_config_alone(self):
        """None is what tells main() not to touch settings at all."""
        self.assertIsNone(self._parse([]).ui_scale)

    def test_dest_matches_the_settings_key(self):
        """main() applies overrides as settings.<dest> = args.<dest>, so the
        flag's dest has to be the config key name or the override silently
        writes an attribute nothing reads."""
        from jellyfin_mpv_shim.conf import Settings

        self.assertIn("ui_scale", Settings.__annotations__)

    def test_nonsense_values_degrade_to_1(self):
        """argparse takes any float; set_scale is the guard."""
        for bad in (0.0, -1.0, float("inf")):
            self.assertEqual(scaling.set_scale(bad or 1.0), 1.0, repr(bad))


class ScaleResolutionTest(unittest.TestCase):
    def tearDown(self):
        scaling.set_scale(1.0)

    def test_bad_values_fall_back_to_1(self):
        for bad in (None, 0, -2, "nonsense", float("nan"), float("inf")):
            self.assertEqual(scaling.set_scale(bad), 1.0, repr(bad))

    def test_px_rounds_half_up_and_matches_raster(self):
        scaling.set_scale(1.5)
        self.assertEqual(scaling.px(15), 23)      # 22.5 -> 23
        self.assertEqual(scaling.raster(15, 15), (23, 23))

    def test_logical_size_is_the_inverse_of_the_surface(self):
        scaling.set_scale(2.0)
        self.assertEqual(scaling.logical_size((2560, 1440)), (1280.0, 720.0))


if __name__ == "__main__":
    unittest.main()
