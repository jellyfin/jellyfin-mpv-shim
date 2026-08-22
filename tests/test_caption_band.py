"""The caption band is sized for the lines a row can actually fill.

A tile is artwork plus a fixed band under it, and the band is a promise
`_paint_caption` keeps rather than a clip -- so its height is the row's
pitch, its hover ring and its scroll geometry all at once. Sizing it for two
lines where the items have one leaves a strip of nothing under every caption
with the ring drawn around it, which is what the home screen's library row
looked like.

The three-line half of the same decision (a Live TV listing at poster width)
is tested in test_live_tv.py, where the items that need it live.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource                      # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser     # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.components import labels    # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.strips import (             # noqa: E402
    LANDSCAPE_GEOM, POSTER_GEOM)


LIBRARY = {"Id": "lib1", "Type": "UserView", "Name": "TV Shows"}
MOVIE = {"Id": "m1", "Type": "Movie", "Name": "Casablanca",
         "ProductionYear": 1942}


class HasNoSubtitlesTest(unittest.TestCase):
    def test_a_row_of_libraries_has_none(self):
        self.assertTrue(labels.has_no_subtitles([LIBRARY, dict(LIBRARY,
                                                               Id="lib2")]))

    def test_a_row_of_movies_does(self):
        self.assertFalse(labels.has_no_subtitles([MOVIE]))

    def test_one_item_of_another_type_keeps_the_line_for_the_row(self):
        # A strip is composited at ONE caption height, so this cannot be a
        # per-tile answer -- the mixed row has to keep the taller band.
        self.assertFalse(labels.has_no_subtitles([LIBRARY, MOVIE]))

    def test_an_empty_row_does_not_shrink(self):
        # Nothing to shrink for, and a row still loading must not be pitched
        # for one line and then filled with items that need two.
        self.assertFalse(labels.has_no_subtitles([]))
        self.assertFalse(labels.has_no_subtitles(None))

    def test_a_pseudo_item_carrying_its_own_subtitle_keeps_the_line(self):
        # Chapters set _subtitle directly, bypassing episode_subtitle.
        self.assertFalse(labels.has_no_subtitles(
            [dict(LIBRARY, _subtitle="0:00")]))

    def test_a_movie_with_no_year_still_keeps_the_line(self):
        """The predicate is on the TYPE, not on this batch's values.

        A grid recomputes its geometry from the items loaded so far, and
        that fixes the row pitch and the virtualization window. If one
        yearless page could shrink the band, page two re-pitches every row
        under the reader's scroll position.
        """
        self.assertFalse(labels.has_no_subtitles([{"Id": "m2",
                                                   "Type": "Movie",
                                                   "Name": "Untitled"}]))


class CaptionGeomTest(unittest.TestCase):
    def setUp(self):
        self.tiles = MpvtkBrowser(app=None, source=FakeSource()).tiles

    def test_a_library_row_loses_the_second_line(self):
        geom = self.tiles.caption_geom([LIBRARY], LANDSCAPE_GEOM)
        self.assertEqual(geom.caption_lines, 1)
        self.assertLess(geom.caption_h, LANDSCAPE_GEOM.caption_h)

    def test_and_it_reclaims_exactly_the_line_and_its_gap(self):
        geom = self.tiles.caption_geom([LIBRARY], LANDSCAPE_GEOM)
        self.assertEqual(
            LANDSCAPE_GEOM.caption_h - geom.caption_h,
            LANDSCAPE_GEOM.sub_size + LANDSCAPE_GEOM.TITLE_GAP)

    def test_a_movie_row_keeps_both_lines(self):
        geom = self.tiles.caption_geom([MOVIE], POSTER_GEOM)
        self.assertEqual(geom.caption_lines, 2)
        self.assertEqual(geom.caption_h, POSTER_GEOM.caption_h)

    def test_the_band_still_holds_the_title_and_its_slack(self):
        """Reclaiming the line must not crop the caption to its own text.

        `_paint_caption` starts at tile_h + 6 and draws the title; the band
        has to hold that plus the slack the two-line band carried under its
        last line, or a one-line caption sits tighter than a two-line one.
        """
        two, one = LANDSCAPE_GEOM, self.tiles.caption_geom([LIBRARY],
                                                           LANDSCAPE_GEOM)
        two_slack = two.caption_h - (two.title_size + two.TITLE_GAP
                                     + two.sub_size)
        self.assertEqual(one.caption_h - one.title_size, two_slack)

    def test_shrinking_is_idempotent(self):
        once = self.tiles.caption_geom([LIBRARY], LANDSCAPE_GEOM)
        twice = self.tiles.caption_geom([LIBRARY], once)
        self.assertEqual(once, twice)

    def test_a_three_line_band_is_never_shrunk(self):
        """It belongs to a Live TV listing, which has lines to fill it.

        Guarded in single_line() rather than left to the caller: the two
        decisions meet in caption_geom, and a Program is not a
        NO_SUBTITLE_TYPE, so nothing else stops them composing wrongly.
        """
        wide = POSTER_GEOM.with_caption_lines(3)
        self.assertEqual(wide.single_line(), wide)


class HomeLibraryRowTest(unittest.TestCase):
    """The row that had the bug, through the page that draws it.

    The unit above passes whether or not home ASKS for it -- the library row
    took art.geom_wide bare, which is how it kept the empty line while
    caption_geom was right there.
    """

    def test_the_library_strip_is_a_one_line_strip(self):
        from jellyfin_mpv_shim.mpvtk.layout import layout
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import RING_PAD

        b = MpvtkBrowser(app=None, source=FakeSource())
        b.navigate({"kind": "home", "server": "s1"})
        b._pool.shutdown(wait=True)
        nodes, _h = layout(b.build((1280, 720)), 1280, 720)
        strip = next(n for n in nodes if n.get("id") == "row-libs")

        # Against the SAME geometry in both spellings. Comparing the library
        # strip to a poster row instead proves nothing: a landscape tile is
        # shorter than a poster whatever its caption does, so that assertion
        # held with the fix reverted.
        wide = b.geom_wide
        two_line = wide.strip_h + 2 * RING_PAD
        one_line = b.tiles.caption_geom([LIBRARY], wide).strip_h + 2 * RING_PAD
        self.assertLess(one_line, two_line, "caption_geom stopped shrinking")
        self.assertEqual(
            strip["h"], one_line,
            "the library row is %spx tall; a one-line landscape strip is %s "
            "and a two-line one is %s, so home is not asking caption_geom"
            % (strip["h"], one_line, two_line))


if __name__ == "__main__":
    unittest.main()
