"""A carousel heading lays out the same whether or not it links (#18/[iw]).

The two spellings used to be different widgets -- a bare ``Text`` for a plain
row, a padded ``Box`` for one with a "see all" chevron -- so a linked row's
title sat 6px right and 2px down of a plain one, and the whole row measured
4px taller. The home screen mixes them (Next Up links, Continue Watching does
not), so adjacent titles jogged sideways and the rows below them did not line
up.

Asserted as *equality between the two spellings*, not against the numbers:
the pad is a design value and may move, but the two must move together. The
one absolute claim is the alignment with the artwork -- the title starts
directly above the first tile, and both start on the page margin, which is
what `ROW_LEAD` and the heading's short leading Spacer buy between them.

The bleed case used to be a separate path and asserted only the two
spellings against each other, never against the artwork. It was wrong the
whole time: a bleed row indented its heading by CONTENT_PAD and left the
strip at RING_PAD, so on the home screen every title sat 16px right of the
tiles under it. There is one path now and one claim, applied to it.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource                      # noqa: E402

from jellyfin_mpv_shim.mpvtk.layout import layout                # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components import chrome     # noqa: E402


ITEMS = [{"Id": "i%d" % i, "Name": "Item %d" % i, "Type": "Movie"}
         for i in range(6)]


def _row(prefix, see_all, **kw):
    b = MpvtkBrowser(app=None, source=FakeSource())
    node = b.tiles.tile_row("A Title", ITEMS, prefix, see_all=see_all, **kw)
    nodes, _h = layout(node, 1280, 720)
    title = next(n for n in nodes if n.get("text") == "A Title")
    first = next(n for n in nodes if n.get("id") == "%s-i0" % prefix)
    return title, first


class HeadingAlignmentTest(unittest.TestCase):
    def test_linked_and_plain_titles_sit_in_the_same_place(self):
        plain, _ = _row("row-plain", None)
        linked, _ = _row("row-linked", lambda: None)
        self.assertEqual((plain["x"], plain["y"]), (linked["x"], linked["y"]))

    def test_a_link_does_not_make_the_row_taller(self):
        _, plain_tile = _row("row-plain", None)
        _, linked_tile = _row("row-linked", lambda: None)
        # The strip's y is where the height difference showed up: a taller
        # heading pushes every tile below it down.
        self.assertEqual(plain_tile["y"], linked_tile["y"])

    def test_the_title_starts_above_the_first_tile(self):
        for prefix, see_all in (("row-plain", None),
                                ("row-linked", lambda: None)):
            with self.subTest(prefix):
                title, tile = _row(prefix, see_all)
                self.assertEqual(title["x"], tile["x"])

    def test_both_start_on_the_page_margin(self):
        """Against the number, not just against each other.

        A row draws its own margins now -- it is a child of an unpadded
        column so the strip can reach the window edge -- so nothing else
        would catch the pair drifting off the margin together.
        """
        title, tile = _row("row-margin", None)
        self.assertEqual(title["x"], chrome.CONTENT_PAD)
        self.assertEqual(tile["x"], chrome.CONTENT_PAD)

    def test_only_the_linked_heading_is_clickable(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        plain = b.tiles.tile_row("A Title", ITEMS, "row-plain", see_all=None)
        _nodes, handlers = layout(plain, 1280, 720)
        self.assertNotIn("row-plain-more", handlers)
        linked = b.tiles.tile_row("A Title", ITEMS, "row-linked",
                                  see_all=lambda: None)
        _nodes, handlers = layout(linked, 1280, 720)
        self.assertIn("click", handlers.get("row-linked-more", {}))


if __name__ == "__main__":
    unittest.main()
