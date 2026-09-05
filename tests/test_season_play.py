"""The play chip on a SEASON tile plays the season (#720).

It used to open it. `_resolve_play_ids` sent the season's id to
`get_series_queue`, which asks `Shows/{id}/Episodes` -- and a season id
there is not a series, so the server answered with nothing, and
`_menu_play`'s "resolved to nothing" branch fell through to `_open_item`.
The chip navigated, which is the one thing a Play button must not do.

The behaviour it should have is jellyfin-web's, taken from
`playbackmanager.js`'s `getSeriesOrSeasonPlaybackPromise` rather than from
memory: a season queues **whole**, in order, and starts at the **first
unplayed** episode. Web deliberately skips its Next Up lookup when a season
is named (`!seasonId` guards it), because "play this season" and "carry on
with this show" are different questions and only the second may leave the
season.

Two things here are about the *shape* of the fix rather than the behaviour,
and both are regressions waiting to happen:

* `_resolve_play_ids` is shared with `_menu_queue`, which passes its result
  straight to `_queue_items`. Widening its return type to carry the start
  index would hand a tuple to the queue path;
* the season resolver must ask a season-scoped query. Reaching for the
  series-wide one is the original bug, and it would look correct on a
  one-season show.
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

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.tiles import TilesMixin  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


SEASON = {"Id": "se1", "Name": "Season 1", "Type": "Season",
          "SeriesId": "sh1", "SeriesName": "A Show"}


class _RecordingSource(FakeSource):
    """Records which queue query was asked, and with what."""

    def __init__(self):
        super().__init__()
        self.asked = []

    def get_season_queue(self, server_uuid, series_id, season_id):
        self.asked.append(("season", series_id, season_id))
        return super().get_season_queue(server_uuid, series_id, season_id)

    #: The one id this stand-in will answer a SERIES query for.
    SERIES_ID = "sh1"

    def get_series_queue(self, server_uuid, series_id, start_item_id=None,
                         limit=100):
        """**Empty for anything that is not a series id**, which is the
        whole point of this stand-in.

        `Shows/{id}/Episodes` resolves `{id}` as a series; hand it a SEASON
        id and the server answers with nothing. The shared `FakeSource`
        returns three episodes for any id at all, so the original bug --
        Play on a season falling through to navigation because the query
        came back empty -- is invisible against it, and a test written on
        that fixture passes with the defect in place. Verified: reverting
        the fix leaves those tests green until this override exists.
        """
        self.asked.append(("series", series_id, start_item_id))
        if series_id != self.SERIES_ID:
            return []
        return super().get_series_queue(server_uuid, series_id,
                                        start_item_id, limit)


def _browser(source=None):
    b = MpvtkBrowser(app=None, source=source or _RecordingSource(),
                     controller=FakeController())
    b._pool = _SyncPool()
    b.server = "srv1"
    return b


class SeasonPlayTest(unittest.TestCase):
    def test_the_chip_plays_instead_of_navigating(self):
        """The bug, stated as the user saw it."""
        b = _browser()
        before = list(b.route.items())
        b._play_tile(dict(SEASON))
        self.assertTrue(b.controller.played,
                        "the season resolved to nothing and nothing played")
        self.assertEqual(list(b.route.items()), before,
                         "Play navigated -- that is the defect, not the fix")

    def test_it_queues_the_whole_season_in_order(self):
        b = _browser()
        b._play_tile(dict(SEASON))
        ids, server, _start = b.controller.played[-1]
        self.assertEqual(ids, ["e0", "e1", "e2", "e3", "e4"])
        self.assertEqual(server, "srv1")

    def test_it_starts_at_the_first_unplayed_episode(self):
        """Web's rule. The fixture marks e0 and e1 watched."""
        b = _browser()
        b._play_tile(dict(SEASON))
        _ids, _server, start = b.controller.played[-1]
        self.assertEqual(start, 2)

    def test_a_fully_watched_season_starts_at_the_top(self):
        """`|| 0` in web's own code, but reached honestly: with nothing
        unplayed there is no better answer than the beginning."""
        src = _RecordingSource()
        src.season_watched = 5           # every episode played
        b = _browser(src)
        b._play_tile(dict(SEASON))
        _ids, _server, start = b.controller.played[-1]
        self.assertEqual(start, 0)

    def test_an_untouched_season_starts_at_the_top(self):
        src = _RecordingSource()
        src.season_watched = 0
        b = _browser(src)
        b._play_tile(dict(SEASON))
        _ids, _server, start = b.controller.played[-1]
        self.assertEqual(start, 0)

    def test_it_asks_a_season_scoped_query(self):
        """The original bug in its pure form: the series-wide query, given a
        season id. It would still look right on a one-season show, so this
        asserts on the CALL rather than on the result."""
        src = _RecordingSource()
        b = _browser(src)
        b._play_tile(dict(SEASON))
        self.assertEqual(src.asked, [("season", "sh1", "se1")])

    def test_a_season_with_no_series_id_plays_nothing_rather_than_the_show(
            self):
        """There is nothing to scope by, and falling back to the series
        would put a whole show behind one season's button."""
        src = _RecordingSource()
        b = _browser(src)
        b._play_tile({"Id": "se9", "Name": "Season 9", "Type": "Season"})
        self.assertEqual([a for a in src.asked if a[0] == "series"], [],
                         "fell back to the series-wide query")
        self.assertEqual(b.controller.played, [])


class SeasonQueueTest(unittest.TestCase):
    """"Add to play queue" shares `_resolve_play_ids` with Play."""

    def test_queueing_a_season_gets_ids_and_not_a_tuple(self):
        """The shape regression the fix had to avoid: `_resolve_play_ids`
        has two callers, and `_menu_queue` hands its result straight to
        `_queue_items`. A `(ids, start, items)` tuple would arrive there as
        though it were the id list."""
        src = _RecordingSource()
        b = _browser(src)
        got = b._resolve_play_ids(dict(SEASON), "srv1")
        self.assertEqual(got, ["e0", "e1", "e2", "e3", "e4"])
        for entry in got:
            self.assertIsInstance(entry, str)

    def test_it_is_season_scoped_there_too(self):
        src = _RecordingSource()
        b = _browser(src)
        b._resolve_play_ids(dict(SEASON), "srv1")
        self.assertEqual(src.asked, [("season", "sh1", "se1")])

    def test_a_series_still_uses_the_series_wide_query(self):
        """The other half of the split -- a Series must keep crossing
        season boundaries, which is what `get_series_queue` is for."""
        src = _RecordingSource()
        b = _browser(src)
        b._resolve_play_ids({"Id": "sh1", "Type": "Series"}, "srv1")
        self.assertEqual(src.asked, [("series", "sh1", None)])


class FirstUnplayedTest(unittest.TestCase):
    """The rule on its own, where the edge cases are cheap to state."""

    def _at(self, *played):
        return TilesMixin._first_unplayed(
            [{"Id": str(i), "UserData": {"Played": p}}
             for i, p in enumerate(played)])

    def test_the_first_gap_wins(self):
        self.assertEqual(self._at(True, True, False, True), 2)

    def test_a_gap_after_an_unplayed_run_does_not(self):
        self.assertEqual(self._at(False, True, False), 0)

    def test_all_played_is_zero(self):
        self.assertEqual(self._at(True, True, True), 0)

    def test_zero_is_a_real_index_not_a_fallback(self):
        """Web spells this `StartIndex || seasonStartIndex || 0`, which
        only works because index 0 is falsy. Ours returns the index."""
        self.assertEqual(self._at(False, False), 0)

    def test_a_missing_userdata_reads_as_unplayed(self):
        """An episode DTO without UserData is not evidence that it was
        watched, and treating it as watched would skip past it."""
        self.assertEqual(TilesMixin._first_unplayed(
            [{"Id": "a", "UserData": {"Played": True}}, {"Id": "b"}]), 1)

    def test_an_empty_queue_does_not_raise(self):
        self.assertEqual(TilesMixin._first_unplayed([]), 0)
        self.assertEqual(TilesMixin._first_unplayed(None), 0)


if __name__ == "__main__":
    unittest.main()
