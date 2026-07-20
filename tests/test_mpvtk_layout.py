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


class TestWrapMargin(unittest.TestCase):
    """Wrapping is decided from estimated advances but drawn by libass with
    the real font. A line that fits by a fraction of a pixel renders one
    word too long, so the wrap limit keeps a margin."""

    TXT = ("An overview paragraph long enough to wrap several times so that "
           "we can see exactly where each line ends relative to the container "
           "that it is supposed to fit inside of, which is the whole question "
           "here and it needs enough words to sample many widths properly.")

    def test_no_line_lands_on_the_edge(self):
        from jellyfin_mpv_shim.mpvtk.layout import (WRAP_SLOP, text_width,
                                                    wrap_text)

        tightest = min(
            max_w - text_width(line, 18, False)
            for max_w in range(300, 1400)
            for line in wrap_text(self.TXT, 18, False, max_w))
        self.assertGreaterEqual(
            tightest, WRAP_SLOP - 0.01,
            "a wrapped line sits within the slop of the edge")

    def test_wrapping_still_fills_the_line(self):
        """The margin must not cost a whole word — the line count has to
        match what a no-margin wrap would produce, or close to it."""
        from jellyfin_mpv_shim.mpvtk.layout import wrap_text

        for max_w in (400, 700, 1000):
            n = len(wrap_text(self.TXT, 18, False, max_w))
            wide = len(wrap_text(self.TXT, 18, False, max_w + 40))
            self.assertGreaterEqual(n, wide)
            self.assertLessEqual(n - wide, 2, "margin cost too many lines")

    def test_degenerate_widths_terminate(self):
        from jellyfin_mpv_shim.mpvtk.layout import wrap_text

        for w in (0, 0.5, 1, 2):
            self.assertTrue(wrap_text("hello world", 18, False, w))


class TestAssWrapStyle(unittest.TestCase):
    """The layout engine breaks text into lines; libass must not then
    apply its own smart wrapping on top. Two wrappers disagreeing by a
    fraction of a pixel made long text jump to an extra line at random,
    because our break was never authoritative. Every ASS event that
    carries text has to set \\q2 (wrap only on explicit \\N).

    Checked statically: the renderer runs inside mpv, so there is no way
    to assert on the emitted ASS from a unit test — but a new text
    emitter added without \\q2 is exactly the regression to catch.
    """

    def _renderer(self):
        import os

        import jellyfin_mpv_shim

        path = os.path.join(os.path.dirname(jellyfin_mpv_shim.__file__),
                            "mpvtk", "renderer.lua")
        with open(path) as fh:
            return fh.read()

    def test_every_text_event_disables_libass_wrapping(self):
        src = self._renderer()
        # A text-bearing ASS event is one that sets a font size (\fs).
        # Drawings (\p1) and rects carry no text and are exempt.
        missing = []
        for chunk in src.split("ass:append("):
            head = chunk[:400]
            if "\\\\fs%d" not in head:
                continue
            if "\\\\q2" not in head:
                missing.append(head.split("\n")[0].strip()[:70])
        self.assertEqual(missing, [],
                         "ASS text events without \\q2: %r" % (missing,))

    def test_the_tag_is_present_at_all(self):
        """Guards the guard: if the pattern above stops matching, the
        first test would pass vacuously."""
        self.assertGreaterEqual(self._renderer().count("\\\\q2"), 2)
