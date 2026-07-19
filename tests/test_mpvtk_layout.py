import unittest

from jellyfin_mpv_shim.mpvtk.layout import ellipsize, layout, text_width
from jellyfin_mpv_shim.mpvtk.widgets import (
    Box,
    Column,
    Dropdown,
    HScroll,
    Image,
    Row,
    Spacer,
    Text,
    TextBox,
    VScroll,
)


def by_id(nodes, id):
    return next(n for n in nodes if n["id"] == id)


class TestLayout(unittest.TestCase):
    def test_row_flex_distribution(self):
        tree = Row(
            [
                Box(w=100, h=20, bg="111111", id="a"),
                Spacer(),
                Box(w=50, h=20, bg="111111", id="b"),
            ],
            pad=10,
            gap=10,
            w=400,
            h=40,
        )
        nodes, _ = layout(tree, 400, 40)
        a = by_id(nodes, "a")
        b = by_id(nodes, "b")
        self.assertEqual(a["x"], 10)
        self.assertEqual(a["w"], 100)
        # spacer absorbs 400 - 20(pad) - 20(gaps) - 150 = 210
        self.assertEqual(b["x"], 10 + 100 + 10 + 210 + 10)
        self.assertEqual(b["x"] + b["w"], 400 - 10)

    def test_column_stretch(self):
        tree = Column(
            [Box(h=30, bg="111111", id="bar")],
            align="stretch",
            w=500,
            h=200,
        )
        nodes, _ = layout(tree, 500, 200)
        bar = by_id(nodes, "bar")
        self.assertEqual(bar["w"], 500)

    def test_scroll_content_and_chaining(self):
        tiles = Row(
            [Image("/x.bgra", 140, 200) for _ in range(10)], gap=10
        )
        page = Column(
            [HScroll(tiles, id="row", h=210)], pad=0
        )
        tree = Column(
            [VScroll(page, id="page", flex=1)],
            w=800,
            h=400,
            align="stretch",
        )
        nodes, _ = layout(tree, 800, 400)
        page_n = by_id(nodes, "page")
        row_n = by_id(nodes, "row")
        self.assertEqual(page_n["axis"], "y")
        self.assertTrue(page_n["bar"])
        # v-scroll reserves scrollbar width for content
        self.assertEqual(page_n["cw"], 790)
        self.assertEqual(row_n["sc"], "page")
        self.assertEqual(row_n["axis"], "x")
        # 10 tiles * 140 + 9 gaps * 10 = 1490
        self.assertEqual(row_n["cw"], 1490)
        # children of the row reference it
        imgs = [n for n in nodes if n["t"] == "img"]
        self.assertEqual(len(imgs), 10)
        self.assertTrue(all(n["sc"] == "row" for n in imgs))
        # laid out in content space (offset 0), so last tile x > viewport
        self.assertEqual(imgs[-1]["x"], 9 * 150)

    def test_handlers_registry(self):
        clicked = []
        tree = Row(
            [
                Box(
                    w=50,
                    h=50,
                    bg="222222",
                    id="btn",
                    on_click=lambda: clicked.append(1),
                ),
                TextBox("search", on_change=lambda v: None),
                Dropdown("sort", ["A", "B"], on_select=lambda i, v: None),
            ],
            w=400,
            h=60,
        )
        nodes, handlers = layout(tree, 400, 60)
        self.assertIn("click", handlers["btn"])
        self.assertIn("change", handlers["search"])
        self.assertIn("select", handlers["sort"])
        self.assertTrue(by_id(nodes, "btn")["click"])
        handlers["btn"]["click"]()
        self.assertEqual(clicked, [1])

    def test_ellipsize(self):
        s = "A fairly long title that will not fit"
        out = ellipsize(s, 20, False, 100)
        self.assertTrue(out.endswith("…"))
        self.assertLess(text_width(out, 20), 105)
        self.assertEqual(ellipsize("Short", 20, False, 500), "Short")

    def test_duplicate_id_warning(self):
        tree = Row(
            [
                Box(w=10, h=10, bg="111111", id="dup"),
                Box(w=10, h=10, bg="111111", id="dup"),
            ],
            w=100,
            h=20,
        )
        with self.assertLogs("mpvtk", level="WARNING") as cm:
            layout(tree, 100, 20)
        self.assertTrue(any("duplicate node id" in m for m in cm.output))

    def test_stable_path_ids(self):
        def build():
            return Column(
                [Row([Text("x"), Text("y")]), Text("z")], w=100, h=100
            )

        nodes_a, _ = layout(build(), 100, 100)
        nodes_b, _ = layout(build(), 100, 100)
        self.assertEqual(
            [n["id"] for n in nodes_a], [n["id"] for n in nodes_b]
        )


if __name__ == "__main__":
    unittest.main()
