"""Every listing page opens the same distance below the top bar.

The horizontal margin has its own test (test_carousel_heading_alignment);
this is the other axis, and it went wrong the same way. The home screen's
column was written ``pad=0`` for a horizontal reason -- a carousel is full
width and carries the page margin inside its own scroll viewport, so padding
the column would inset the strip twice -- and the vertical pad went with it,
silently. The result was a home screen whose first section title touched the
top bar while the library grid one click away sat CONTENT_PAD below it.

Asserted against the constant rather than page-to-page, so a page that opens
flush fails on its own rather than dragging a correct page down with it.

Full-bleed pages are excluded by name, not by tolerance: a banner running to
the window edge is the deliberate opposite of this rule, and a test that
accepted either would assert nothing.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._scene_snapshot import snapshot                       # noqa: E402
from tests._shell_harness import FakeSource                      # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components import chrome    # noqa: E402


SIZE = (1280, 720)

#: Routes whose body is a stack of content starting at the top. Detail,
#: series and season are absent on purpose -- they open with a backdrop that
#: bleeds to all three edges.
ROUTES = {
    "home": {"kind": "home", "server": "s1"},
    "grid": {"kind": "grid", "parent_id": "lib1", "server": "s1",
             "title": "Movies"},
    "search": {"kind": "search", "server": "s1", "term": "a"},
}


def _scene(route):
    return snapshot(MpvtkBrowser(app=None, source=FakeSource()), route, SIZE)


def _top_bar_height(nodes):
    """Measured, not assumed: the bar's height is a font-metric sum, so a
    literal here would turn a type-scale change into a failure of this
    file."""
    full = [n for n in nodes
            if n.get("y") == 0 and round(n.get("w", 0)) == SIZE[0]
            and n.get("h")]
    assert full, "no top bar found"
    return max(n["h"] for n in full)


def _content_top(nodes, bar_h):
    """Y of the first thing the user reads.

    Skips the page background and the scroll viewport itself -- both start
    at the bar and are not content -- by ignoring anything as wide as the
    window. A tile strip is that wide too, but its own artwork is not, and
    the artwork is what "content starts here" means.
    """
    ys = [n["y"] for n in nodes
          if n.get("y") is not None and n["y"] >= bar_h
          and round(n.get("w", 0)) < SIZE[0]]
    assert ys, "page rendered no content"
    return min(ys)


class PageTopMarginTest(unittest.TestCase):
    def test_content_starts_one_margin_below_the_top_bar(self):
        for name, route in ROUTES.items():
            with self.subTest(name):
                nodes = _scene(route)
                bar = _top_bar_height(nodes)
                self.assertAlmostEqual(
                    _content_top(nodes, bar) - bar, chrome.CONTENT_PAD,
                    delta=2.01,      # a heading Box's own 2px ring padding
                    msg="%s opens %.1fpx below the top bar; every listing "
                        "page owes CONTENT_PAD" % (
                            name, _content_top(nodes, bar) - bar))

    def test_the_page_also_ends_on_a_margin(self):
        """The bottom half of the same pad.

        Cheap to lose and invisible until the list is long enough to scroll
        to the end, which no snapshot reaches: the scrollable content height
        is what carries it, so that is what this reads.
        """
        for name, route in ROUTES.items():
            with self.subTest(name):
                nodes = _scene(route)
                scroll = [n for n in nodes if n.get("t") == "scroll"
                          and n.get("axis") != "x"]
                self.assertTrue(scroll, "%s has no vertical scroll" % name)
                body = max(n.get("ch", 0) for n in scroll)
                last = max((n["y"] + n.get("h", 0) for n in nodes
                            if n.get("y") is not None
                            and round(n.get("w", 0)) < SIZE[0]),
                           default=0)
                bar = _top_bar_height(nodes)
                self.assertGreaterEqual(
                    body, last - bar + chrome.CONTENT_PAD - 2.01,
                    "%s runs its last row into the bottom edge" % name)


if __name__ == "__main__":
    unittest.main()


class SectionRhythmTest(unittest.TestCase):
    """A section title belongs to the strip under it, not between two.

    Asserted as a ratio rather than as pixels because both numbers are design
    values that may move; what may not move is which of them is larger. At
    gap=10 they were 17 and 27 -- close enough that the eye grouped each title
    with whichever strip it happened to be nearer, and a page of six rows read
    as twelve unrelated bands. jellyfin-web spends the whole gap between
    sections and none inside one (``.sectionTitleContainer-cards`` is
    ``margin: 0; padding-top: 1.25em``).
    """

    def _rows(self):
        nodes = _scene(ROUTES["home"])
        heads = sorted((n for n in nodes if n.get("t") == "text"
                        and n.get("size", 0) >= 20 and n.get("sc") == "home"),
                       key=lambda n: n["y"])
        strips = sorted((n for n in nodes if n.get("t") == "scroll"
                         and n.get("axis") == "x"), key=lambda n: n["y"])
        pairs = []
        for h in heads:
            below = [s for s in strips if s["y"] >= h["y"]]
            if below:
                pairs.append((h, below[0]))
        self.assertGreaterEqual(len(pairs), 2, "home drew too few carousels")
        return pairs

    def test_a_title_sits_closer_to_its_own_strip(self):
        pairs = self._rows()
        for i in range(len(pairs) - 1):
            head, strip = pairs[i]
            own = strip["y"] - (head["y"] + head["h"])
            nxt = pairs[i + 1][0]["y"] - (strip["y"] + strip["h"])
            with self.subTest(head.get("text")):
                self.assertLess(
                    own * 2, nxt,
                    "%r is %.1fpx above its own strip and %.1fpx below the "
                    "previous one; a title that near-equidistant reads as "
                    "belonging to neither" % (head.get("text"), own, nxt))
