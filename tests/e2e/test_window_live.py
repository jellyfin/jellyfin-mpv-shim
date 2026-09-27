"""Slice S6, row 30: the library window changes size under the real app.

test_window_resize.py asks the same questions of a separately spawned mpv
and browser (one of the harness substitutions the e2e audit named, and its
focus case skips when it cannot focus anything). Here: the shipped app,
focus put by keys, the size changed with mpv's `geometry` (xvfb has no
window manager to ask).

- the grid reflows to the new width and its tiles keep their shape;
- the focused tile keeps focus, and is still on screen;
- a window too small to use does not crash the app, and the library works
  again at a usable size.
"""

import os
import sys
import unittest
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _app  # noqa: E402
from test_browse_routes import _BrowseCase  # noqa: E402


def grid_tiles(frame):
    """The library grid's item tiles on screen: grid-<row>-<item id>."""
    return [n for n in frame.get("nodes", [])
            if (n.get("id") or "").startswith("grid-")
            and len(n["id"].rsplit("-", 1)[-1]) == 32
            and n.get("vis") and n.get("w")]


def per_row(frame):
    tiles = grid_tiles(frame)
    return max(Counter(round(n["y"]) for n in tiles).values()) if tiles else 0


class _WindowCase(_BrowseCase):

    def resize(self, w, h):
        self.app.mpv.command("set", "geometry", "%dx%d" % (w, h))
        return self.app.wait_for(lambda f: (f.get("w"), f.get("h")) == (w, h),
                                 timeout=15, what="a %dx%d frame" % (w, h))

    def into_grid(self):
        """The Movies grid, with keyboard focus on one of its tiles."""
        self.open_library(self.library("Movies"))
        self.app.wait_for(lambda f: len(grid_tiles(f)) > 1, timeout=30,
                          what="the grid's tiles")

        def on_a_tile(f):
            return any(n["id"] == f.get("nav") for n in grid_tiles(f))
        return self.app.press_until("DOWN", on_a_tile, what="focus on a tile")


class TheGridReflowsTest(_WindowCase):

    def test_wider_means_more_per_row_and_the_same_shape(self):
        self.resize(1280, 720)
        self.into_grid()
        narrow = self.app.frame()
        wide = self.resize(1900, 720)
        self.app.wait_for(lambda f: per_row(f) > per_row(narrow),
                          timeout=15, what="more tiles a row at 1900 wide "
                          "(%d at 1280)" % per_row(narrow))
        wide = self.app.frame()

        def shape(f):
            t = grid_tiles(f)[0]
            return t["w"] / float(t["h"])
        self.assertAlmostEqual(shape(narrow), shape(wide), delta=0.03,
                               msg="the tiles changed shape across a resize")
        self.assertEqual(0, self.app.quit(timeout=30))


class FocusSurvivesAResizeTest(_WindowCase):

    def test_the_focused_tile_keeps_focus(self):
        self.resize(1280, 720)
        focused = self.into_grid()["nav"]
        self.assertTrue(focused and focused.startswith("grid-"), focused)
        where = set()
        for w, h in ((1500, 940), (900, 600), (1280, 720)):
            f = self.resize(w, h)
            f = self.app.wait_for(lambda f: _app.shown(f, focused),
                                  timeout=15,
                                  what="%s on screen at %dx%d"
                                  % (focused, w, h))
            self.assertEqual(focused, f.get("nav"),
                             "focus moved at %dx%d" % (w, h))
            node = _app.node(f, focused)
            where.add((round(node["x"]), round(node["y"])))
        # Control: the tile really moved, or keeping focus proves nothing.
        self.assertGreater(len(where), 1, "the layout never changed")
        self.assertEqual(0, self.app.quit(timeout=30))


class ATooSmallWindowDoesNotCrashItTest(_WindowCase):

    def test_tiny_then_usable_again(self):
        self.resize(1280, 720)
        self.into_grid()
        self.resize(200, 120)
        self.assertTrue(self.app.alive(), "a tiny window killed the app")
        self.resize(1280, 720)
        f = self.app.wait_for(lambda f: grid_tiles(f), timeout=15,
                              what="the grid back at a usable size")
        was = f.get("nav")
        f = self.app.press_until("RIGHT", lambda f: f.get("nav") != was,
                                 what="focus moving after the tiny window")
        self.assertTrue(f.get("nav"), "the keyboard lands nowhere")
        self.assertEqual(0, self.app.quit(timeout=30))


if __name__ == "__main__":
    unittest.main()
