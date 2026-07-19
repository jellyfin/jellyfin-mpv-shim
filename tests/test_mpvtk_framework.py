"""Unit tests for the framework features added to close the MIGRATION.md
"Framework deficits" list: Stack (+occlude markers), Text wrap,
Table, hold-repeat flags, click modifier dispatch, and the synchronous
scroll-offset query. Renderer-side behavior (overlay slot ordering,
repeat timers, modifier capture) is covered by the mpvtk selftest
(``python3 -m jellyfin_mpv_shim.mpvtk --selftest`` under xvfb).
"""

import json
import unittest

from tests.integration._harness import FakeMPV

from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
from jellyfin_mpv_shim.mpvtk.layout import layout, wrap_text, LINE_H
from jellyfin_mpv_shim.mpvtk.widgets import (
    Box,
    Button,
    Column,
    Image,
    ImageMap,
    Stack,
    Table,
    Text,
    VScroll,
)


def by_id(nodes, id):
    return next(n for n in nodes if n["id"] == id)


class TestTextWrap(unittest.TestCase):
    def test_wrap_text_breaks_on_words(self):
        # heuristic metrics: aaa at size 10 is ~5.4px/char
        lines = wrap_text("aaa bbb ccc ddd", 10, False, 40)
        self.assertGreater(len(lines), 1)
        self.assertEqual(" ".join(lines), "aaa bbb ccc ddd")

    def test_wrap_preserves_blank_lines(self):
        self.assertEqual(
            wrap_text("a\n\nb", 10, False, 400), ["a", "", "b"]
        )

    def test_wrap_hard_breaks_long_words(self):
        lines = wrap_text("a" * 40, 10, False, 60)
        self.assertGreater(len(lines), 2)
        self.assertEqual("".join(lines), "a" * 40)

    def test_layout_emits_one_node_per_line(self):
        tree = Column(
            [Text("aaa bbb ccc ddd eee fff", id="t", size=10,
                  wrap=True, w=60)],
            w=400, h=300,
        )
        nodes, _ = layout(tree, 400, 300)
        lines = [n for n in nodes if n["t"] == "text"]
        self.assertGreater(len(lines), 1)
        self.assertEqual(lines[0]["id"], "t")
        self.assertEqual(lines[1]["id"], "t.l1")
        # stacked one line apart at the same x
        self.assertEqual(lines[0]["x"], lines[1]["x"])
        self.assertEqual(
            round(lines[1]["y"] - lines[0]["y"], 1), 10 * LINE_H
        )

    def test_max_lines_truncates_with_ellipsis(self):
        tree = Column(
            [Text("aaa bbb ccc ddd eee fff ggg", id="t", size=10,
                  wrap=True, max_lines=2, w=60)],
            w=400, h=300,
        )
        nodes, _ = layout(tree, 400, 300)
        lines = [n for n in nodes if n["t"] == "text"]
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1]["text"].endswith("…"), lines[-1])

    def test_column_reserves_wrapped_height(self):
        # the wrapped Text's assigned height pushes the next sibling down
        tree = Column(
            [
                Text("aaa bbb ccc ddd eee fff", id="t", size=10,
                     wrap=True, w=60),
                Box(id="after", h=10, bg="111111"),
            ],
            w=400, h=300,
        )
        nodes, _ = layout(tree, 400, 300)
        lines = [n for n in nodes if n["t"] == "text"]
        after = by_id(nodes, "after")
        self.assertEqual(
            round(after["y"], 1),
            round(len(lines) * 10 * LINE_H, 1),
        )

    def test_unwrapped_text_unchanged(self):
        nodes, _ = layout(
            Column([Text("hello", id="t", size=10)], w=400, h=300),
            400, 300,
        )
        self.assertEqual(len([n for n in nodes if n["t"] == "text"]), 1)


class TestStack(unittest.TestCase):
    def _nodes(self, stack):
        return layout(Column([stack], w=400, h=300), 400, 300)[0]

    def test_fill_and_anchors(self):
        nodes = self._nodes(
            Stack(
                [
                    Box(id="base", bg="111111"),  # anchor None -> fill
                    Box(id="ne", w=20, h=10, bg="222222", anchor="ne"),
                    Box(id="c", w=20, h=10, bg="222222", anchor="c"),
                    Box(id="se", w=20, h=10, bg="222222", anchor="se",
                        dx=-5, dy=-5),
                ],
                w=200, h=100,
            )
        )
        base = by_id(nodes, "base")
        self.assertEqual((base["x"], base["y"], base["w"], base["h"]),
                         (0, 0, 200, 100))
        ne = by_id(nodes, "ne")
        self.assertEqual((ne["x"], ne["y"]), (180, 0))
        c = by_id(nodes, "c")
        self.assertEqual((c["x"], c["y"]), (90, 45))
        se = by_id(nodes, "se")
        self.assertEqual((se["x"], se["y"]), (175, 85))

    def test_paint_order_is_child_order(self):
        nodes = self._nodes(
            Stack(
                [
                    Image("/a.bgra", 100, 50, id="under"),
                    Image("/b.bgra", 20, 20, id="over", anchor="nw"),
                ],
                w=100, h=50,
            )
        )
        ids = [n["id"] for n in nodes if n["t"] == "img"]
        self.assertEqual(ids, ["under", "over"])

    def test_occlude_marker_precedes_child(self):
        nodes = self._nodes(
            Stack(
                [
                    Image("/a.bgra", 100, 50, id="strip"),
                    Box(id="chip", w=30, h=10, bg="ffcc66",
                        anchor="sw", occlude=True),
                ],
                w=100, h=50,
            )
        )
        kinds = [n["t"] for n in nodes]
        self.assertIn("occ", kinds)
        occ = next(n for n in nodes if n["t"] == "occ")
        chip = by_id(nodes, "chip")
        # marker carries the child's rect and comes after the image but
        # before the child (renderer subtracts it from earlier images)
        self.assertEqual((occ["x"], occ["y"], occ["w"], occ["h"]),
                         (chip["x"], chip["y"], chip["w"], chip["h"]))
        idx = {n["id"]: i for i, n in enumerate(nodes)}
        self.assertLess(idx["strip"], idx[occ["id"]])
        self.assertLess(idx[occ["id"]], idx["chip"])

    def test_inherits_scroll_container(self):
        tree = Column(
            [
                VScroll(
                    Stack(
                        [
                            Image("/a.bgra", 100, 50, id="strip"),
                            Box(id="chip", w=10, h=10, bg="111111",
                                anchor="ne", occlude=True),
                        ],
                        w=100, h=50,
                    ),
                    id="page", h=200,
                )
            ],
            w=400, h=300,
        )
        nodes, _ = layout(tree, 400, 300)
        occ = next(n for n in nodes if n["t"] == "occ")
        self.assertEqual(occ["sc"], "page")
        self.assertEqual(by_id(nodes, "chip")["sc"], "page")

    def test_measures_to_largest_child(self):
        nodes, _ = layout(
            Column(
                [
                    Stack([Box(id="a", w=120, h=40, bg="111111")]),
                    Box(id="after", h=10, bg="222222"),
                ],
                w=400, h=300,
            ),
            400, 300,
        )
        self.assertEqual(by_id(nodes, "after")["y"], 40)


class TestTable(unittest.TestCase):
    def _table(self, **kw):
        return Table(
            columns=[
                {"label": "#", "w": 40},
                {"label": "Title", "flex": 1},
                {"label": "Year", "w": 60, "align": "right"},
            ],
            rows=[
                {"id": "r0", "cells": ["1", "Alpha", "2001"],
                 "on_click": lambda: None},
                {"id": "r1", "cells": ["2", "Beta", "2002"],
                 "selected": True, "on_click": lambda: None},
            ],
            w=400,
            **kw,
        )

    def test_header_and_cells_share_geometry(self):
        nodes, _ = layout(
            Column([self._table()], w=400, h=300), 400, 300
        )
        texts = [n for n in nodes if n["t"] == "text"]
        year_hdr = next(n for n in texts if n["text"] == "Year")
        year_cell = next(n for n in texts if n["text"] == "2001")
        self.assertEqual(year_hdr["x"], year_cell["x"])
        self.assertEqual(year_hdr["w"], year_cell["w"])
        num_hdr = next(n for n in texts if n["text"] == "#")
        num_cell = next(n for n in texts if n["text"] == "2")
        self.assertEqual(num_hdr["x"], num_cell["x"])

    def test_selection_and_click_wiring(self):
        nodes, handlers = layout(
            Column([self._table()], w=400, h=300), 400, 300
        )
        r1 = by_id(nodes, "r1")
        self.assertIn("fill", r1)  # selected row gets a background
        self.assertTrue(r1.get("click"))
        self.assertIn("click", handlers["r0"])

    def test_right_align_passes_through(self):
        nodes, _ = layout(
            Column([self._table()], w=400, h=300), 400, 300
        )
        year_cell = next(
            n for n in nodes if n["t"] == "text" and n["text"] == "2001"
        )
        self.assertEqual(year_cell["align"], "right")


class TestRepeat(unittest.TestCase):
    def test_button_repeat_flag(self):
        nodes, _ = layout(
            Column(
                [Button("Up", id="b", repeat=True,
                        on_click=lambda: None)],
                w=200, h=100,
            ),
            200, 100,
        )
        self.assertTrue(by_id(nodes, "b").get("rpt"))

    def test_plain_button_has_no_flag(self):
        nodes, _ = layout(
            Column(
                [Button("Up", id="b", on_click=lambda: None)],
                w=200, h=100,
            ),
            200, 100,
        )
        self.assertNotIn("rpt", by_id(nodes, "b"))

    def test_imagemap_region_repeat(self):
        im = ImageMap(
            "/a.bgra", 100, 50,
            regions=[
                {"id": "reg", "x": 0, "y": 0, "w": 50, "h": 50,
                 "on_click": lambda: None, "repeat": True}
            ],
        )
        nodes, _ = layout(Column([im], w=200, h=100), 200, 100)
        self.assertTrue(by_id(nodes, "reg").get("rpt"))


class TestClickMods(unittest.TestCase):
    def test_wants_mods_rules(self):
        wants = MpvtkApp._wants_mods
        self.assertFalse(wants(lambda: None))
        self.assertTrue(wants(lambda mods: None))
        # captured loop variable via default arg: bare call
        self.assertFalse(wants(lambda i=3: None))
        self.assertTrue(wants(lambda m, i=3: None))
        self.assertTrue(wants(lambda *a: None))

        class C:
            def bare(self):
                pass

            def with_mods(self, mods):
                pass

        self.assertFalse(wants(C().bare))
        self.assertTrue(wants(C().with_mods))

    def _app_with_handler(self, fn):
        app = MpvtkApp.attach(FakeMPV(), ext=False)
        app.size = (400, 300)
        app._build = lambda size: Column(
            [Box(w=50, h=50, bg="222222", id="btn", on_click=fn)],
            w=size[0], h=size[1],
        )
        app._render()
        return app

    def test_click_dispatch_passes_mods(self):
        got = []
        app = self._app_with_handler(lambda m: got.append(m))
        app._dispatch({"t": "click", "id": "btn", "shift": True})
        self.assertEqual(got, [{"shift": True, "ctrl": False}])

    def test_click_dispatch_bare_handler(self):
        got = []
        app = self._app_with_handler(lambda: got.append("x"))
        app._dispatch({"t": "click", "id": "btn", "shift": True,
                       "ctrl": True})
        self.assertEqual(got, ["x"])


class TestScrollOffsets(unittest.TestCase):
    def test_reads_property_mirror(self):
        app = MpvtkApp.attach(FakeMPV(), ext=False)
        app.backend.get_property = (
            lambda name: {"page": 320.0}
            if name == "user-data/mpvtk/scroll" else None
        )
        self.assertEqual(app.scroll_offsets(), {"page": 320.0})

    def test_missing_property_is_empty(self):
        # FakeMPV has no _get_property; the backend swallows it
        app = MpvtkApp.attach(FakeMPV(), ext=False)
        self.assertEqual(app.scroll_offsets(), {})


if __name__ == "__main__":
    unittest.main()
