"""A season screen has to say which show it is a season of.

Reported: the page said "Season 1" three times -- in the title bar, as the
page heading, and in the season picker beside it -- so the one thing it
never told you was the name of the series. [iw]: "it shows the actual show
name in the titlebar and we can let the subheading be the season number."

The picker keeps saying it, because a picker showing its own selection is
not a repetition, it is the control working.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource, build_scene                # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser            # noqa: E402


def _browser(route):
    b = MpvtkBrowser(app=None, source=FakeSource())
    b.nav_stack = [dict(route)]
    b._load_route(b.route)
    b._pool.shutdown(wait=True)
    return b


SEASON = {"kind": "season", "item_id": "se1", "series_id": "sh1",
          "server": "s1", "title": "Season 1", "bar_title": "A Show"}


class SeasonHeadingTest(unittest.TestCase):
    def test_the_bar_says_the_show_and_the_page_says_the_season(self):
        b = _browser(SEASON)
        nodes, _h = build_scene(b, (1280, 720))
        texts = [n.get("text") for n in nodes if n.get("t") == "text"]
        self.assertIn("A Show", texts,
                      "the title bar does not name the series")
        self.assertIn("Season 1", texts,
                      "the page heading no longer names the season")

    @staticmethod
    def _shown(nodes):
        """Every place the scene puts a season name in front of the user.

        Text nodes **and** the dropdown's own selection, which is the third
        of the three the report counted and the one a text-only scan cannot
        see: a Dropdown ships `items` plus a `sel` index and the renderer
        draws the label itself, so scanning `t == "text"` finds two and
        calls the bug half-fixed.
        """
        out = []
        for n in nodes:
            if n.get("t") == "text" and n.get("text"):
                out.append(n["text"])
            elif n.get("t") == "dropdown":
                items = n.get("items") or []
                sel = n.get("sel")
                if isinstance(sel, int) and 0 <= sel < len(items):
                    out.append(items[sel])
        return out

    def test_the_season_name_is_not_shown_three_times(self):
        b = _browser(SEASON)
        nodes, _h = build_scene(b, (1280, 720))
        shown = self._shown(nodes)
        # Two: the heading, and the picker showing its selection. A picker
        # displaying what is selected is the control working, not a
        # repetition. Asserted as a bound so an unrelated label does not
        # fail this, but a third *heading* does.
        self.assertLessEqual(
            shown.count("Season 1"), 2,
            "the season name is shown %d times: %r"
            % (shown.count("Season 1"), shown))

    def test_each_of_the_three_surfaces_says_the_right_thing(self):
        """Named individually, because the count above is a bound.

        The three the report counted are the chrome bar, the page heading
        and the picker. They are told apart by `sc`: the bar is chrome and
        sits outside the page's scroll container, the other two are inside
        it. A count alone would be satisfied by the bar going blank.
        """
        b = _browser(SEASON)
        nodes, _h = build_scene(b, (1280, 720))
        bar = [n["text"] for n in nodes
               if n.get("t") == "text" and n.get("text") and not n.get("sc")]
        page = [n["text"] for n in nodes
                if n.get("t") == "text" and n.get("sc") == "season"]
        picker = next(n for n in nodes if n.get("id") == "season-switch")

        self.assertIn("A Show", bar, "the bar does not name the series")
        self.assertNotIn("Season 1", bar,
                         "the bar is still repeating the season")
        self.assertIn("Season 1", page,
                      "the page heading no longer names the season")
        self.assertEqual(picker["items"][picker["sel"]], "Season 1",
                         "the picker is not showing the season it is on")

    def test_a_route_with_no_show_name_still_titles_the_bar(self):
        # bar_title is absent for a season reached without a SeriesName.
        # Falling back to the season name is what the bar did before, and
        # is much better than an empty bar.
        route = dict(SEASON)
        del route["bar_title"]
        b = _browser(route)
        nodes, _h = build_scene(b, (1280, 720))
        texts = [n.get("text") for n in nodes if n.get("t") == "text"]
        self.assertIn("Season 1", texts)

    def test_switching_season_keeps_the_show_in_the_bar(self):
        """The picker rebuilds the route, so it is a second place the bar
        title has to be carried -- and the one that regresses silently,
        since the bar is correct until you use the control."""
        b = _browser(SEASON)
        page = b._page_for(b.route)
        page._switch_season({"Id": "se2", "Name": "Season 2"})
        self.assertEqual(b.route.get("bar_title"), "A Show")
        self.assertEqual(b.route.get("title"), "Season 2")

    def test_a_seasons_own_series_name_wins_when_it_has_one(self):
        b = _browser(SEASON)
        page = b._page_for(b.route)
        page._switch_season({"Id": "se2", "Name": "Season 2",
                             "SeriesName": "Another Show"})
        self.assertEqual(b.route.get("bar_title"), "Another Show")


class OpeningASeasonTest(unittest.TestCase):
    """The only production path that sets `bar_title`.

    It was untested until the harness's Season DTOs grew `SeriesName`:
    every stand-in omitted it, so `_open_item` had nothing to read and the
    field the feature is named about had nowhere to live. A test written
    against those fakes would have passed against code that never set
    bar_title at all.
    """

    def test_opening_a_season_carries_the_show_name(self):
        b = _browser({"kind": "series", "item_id": "sh1", "server": "s1",
                      "title": "A Show"})
        seasons = b.source.get_seasons("s1", "sh1")
        b._open_item(seasons[0])
        self.assertEqual(b.route.get("kind"), "season")
        self.assertEqual(b.route.get("bar_title"), "A Show")

    def test_a_season_with_no_series_name_leaves_it_unset(self):
        # Rather than an empty string, which would blank the bar instead of
        # falling back to the season name.
        b = _browser({"kind": "series", "item_id": "sh1", "server": "s1",
                      "title": "A Show"})
        b._open_item({"Id": "se9", "Name": "Season 9", "Type": "Season",
                      "SeriesId": "sh1"})
        self.assertIsNone(b.route.get("bar_title"))


class ToSeriesTest(unittest.TestCase):
    def test_it_navigates_with_the_show_name(self):
        b = _browser(SEASON)
        page = b._page_for(b.route)
        page.render((1280, 720))
        b._nav_to_series = None
        from tests._shell_harness import build_scene
        nodes, handlers = build_scene(b, (1280, 720))
        handlers["season-to-series"]["click"]()
        self.assertEqual(b.route.get("kind"), "series")
        self.assertEqual(b.route.get("title"), "A Show",
                         "To Series navigated with an empty title, so the "
                         "series page's bar falls back to Home")

    def test_it_falls_back_to_the_bar_title(self):
        """A season DTO short of SeriesName must not blank the title.

        `bar_title` is the same show name and is already on the route, so
        it is the honest fallback.
        """
        b = _browser(SEASON)
        page = b._page_for(b.route)
        for season in (b.route.get("_data") or {}).get("seasons", []):
            season.pop("SeriesName", None)
        from tests._shell_harness import build_scene
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["season-to-series"]["click"]()
        self.assertEqual(b.route.get("title"), "A Show")


class OfflineSeasonTest(unittest.TestCase):
    def test_a_synthesized_season_carries_the_show_name(self):
        """Offline there is no server Season DTO -- it is built from the
        downloaded episodes, and it has to be told what the live one gets
        for free."""
        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            OfflineLibrarySource)

        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        src._snap = type("S", (), {"items": [
            {"Type": "Episode", "SeriesId": "sh1", "SeasonId": "se1",
             "SeriesName": "A Show", "ParentIndexNumber": 1,
             "SeasonName": "Season 1"},
        ]})()
        src._aggregate_userdata = staticmethod(lambda items: {})
        (season,) = src.get_seasons("s1", "sh1")
        self.assertEqual(season["SeriesName"], "A Show")


if __name__ == "__main__":
    unittest.main()
