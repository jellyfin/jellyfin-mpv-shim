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
directly above the first tile, which is what `RING_PAD` buys.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource                      # noqa: E402

from jellyfin_mpv_shim.mpvtk.layout import layout                # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402


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

    def test_a_bleed_row_keeps_both_spellings_aligned(self):
        # `bleed` inserts a leading Spacer instead of relying on container
        # padding, which is a second path to the same alignment.
        plain, plain_tile = _row("row-bp", None, bleed=True)
        linked, linked_tile = _row("row-bl", lambda: None, bleed=True)
        self.assertEqual((plain["x"], plain["y"]), (linked["x"], linked["y"]))
        self.assertEqual(plain_tile["y"], linked_tile["y"])

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
