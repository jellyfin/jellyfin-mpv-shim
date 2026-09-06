""""Go to Series" on a tile (#719).

jellyfin-web's Next Up / Continue Watching card has three hit regions: the
play chip plays, the image opens the item, and the series title opens the
show. Ours had the first two, so correcting a Next Up entry that sits on
the wrong episode meant opening the episode, finding the button on its
detail page, and coming back.

Our captions are baked into the strip bitmap -- `TileRenderer` hands the
compositor `Tile(title=…, subtitle=…)` and the whole row becomes one image
-- so the series name an episode tile already draws is not a node anything
can click. The context menu is the reachable version of web's third
region, and it is the better half of the two: it works on every episode and
season tile anywhere (search results, a genre grid, favourites), and it is
the only one a keyboard or remote user can reach at all.
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

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


EPISODE = {"Id": "e1", "Name": "Ep 1", "Type": "Episode",
           "SeriesId": "sh1", "SeriesName": "A Show",
           "ParentIndexNumber": 1, "IndexNumber": 1}
SEASON = {"Id": "se1", "Name": "Season 1", "Type": "Season",
          "SeriesId": "sh1", "SeriesName": "A Show"}
MOVIE = {"Id": "m1", "Name": "A Film", "Type": "Movie"}


def _browser():
    b = MpvtkBrowser(app=None, source=FakeSource(),
                     controller=FakeController())
    b._pool = _SyncPool()
    b.server = "srv1"
    return b


def _labels(b, item):
    return [e[0] for e in b._tile_menu_entries(item)]


def _action_for(b, item, label):
    for e in b._tile_menu_entries(item):
        if e[0] == label:
            return e[2]
    return None


class EntryPresenceTest(unittest.TestCase):
    def test_an_episode_tile_offers_it(self):
        b = _browser()
        self.assertIn("Go to Series", _labels(b, dict(EPISODE)))

    def test_a_season_tile_offers_it_too(self):
        """Gated on SeriesId, not on Type == "Episode". A season carries
        one, and "go up to the show" is as wanted from a season tile --
        the detail page gates on Episode only because that is the single
        type it renders."""
        b = _browser()
        self.assertIn("Go to Series", _labels(b, dict(SEASON)))

    def test_a_movie_does_not(self):
        b = _browser()
        self.assertNotIn("Go to Series", _labels(b, dict(MOVIE)))

    def test_an_episode_with_no_series_id_does_not(self):
        """A dead entry reads as a broken client, and the handler would
        KeyError on SeriesId."""
        b = _browser()
        orphan = {k: v for k, v in EPISODE.items() if k != "SeriesId"}
        self.assertNotIn("Go to Series", _labels(b, orphan))

    def test_it_is_suppressed_on_that_series_own_page(self):
        """Where it would navigate to the page you are looking at."""
        b = _browser()
        b.route.update({"kind": "series", "item_id": "sh1"})
        self.assertNotIn("Go to Series", _labels(b, dict(SEASON)))

    def test_but_not_on_a_DIFFERENT_series_page(self):
        """A series page can show episodes of another show -- a guest
        appearance in a Special, a mixed search result reached from here.
        The suppression is about identity, not about the route kind."""
        b = _browser()
        b.route.update({"kind": "series", "item_id": "OTHER"})
        self.assertIn("Go to Series", _labels(b, dict(EPISODE)))

    def test_it_survives_on_a_season_page(self):
        """The place the reporter actually wants it: you are inside a
        season and need the show to pick a different one."""
        b = _browser()
        b.route.update({"kind": "season", "item_id": "se1",
                        "series_id": "sh1"})
        self.assertIn("Go to Series", _labels(b, dict(EPISODE)))


class NavigationTest(unittest.TestCase):
    def _fire(self, b, item):
        entries = b._tile_menu_entries(item)
        idx = next(i for i, e in enumerate(entries) if e[2] == "goseries")
        b._menu = {"item": item, "server": "srv1", "x": 0, "y": 0}
        b._menu_action(idx, entries[idx][0])

    def test_it_opens_the_series_route(self):
        b = _browser()
        self._fire(b, dict(EPISODE))
        self.assertEqual(b.route.get("kind"), "series")
        self.assertEqual(b.route.get("item_id"), "sh1")

    def test_it_carries_the_series_name_as_the_title(self):
        """The route title is what the top bar shows before the series DTO
        arrives; without it the bar is blank on the way in."""
        b = _browser()
        self._fire(b, dict(EPISODE))
        self.assertEqual(b.route.get("title"), "A Show")

    def test_it_closes_the_menu(self):
        b = _browser()
        self._fire(b, dict(EPISODE))
        self.assertIsNone(b._menu)

    def test_it_goes_to_the_series_and_not_the_episode(self):
        """The failure mode if the handler reached for `Id`: it would open
        the episode's own detail page, which is where you already were."""
        b = _browser()
        self._fire(b, dict(EPISODE))
        self.assertNotEqual(b.route.get("item_id"), "e1")

    def test_the_route_matches_the_detail_pages_own_button(self):
        """Two doors, one page. `pages/detail.py` builds
        {kind, server, item_id, title} and this must not drift from it --
        a route missing a key lands on a screen that loads differently."""
        b = _browser()
        self._fire(b, dict(EPISODE))
        for key, want in (("kind", "series"), ("server", "srv1"),
                          ("item_id", "sh1"), ("title", "A Show")):
            with self.subTest(key=key):
                self.assertEqual(b.route.get(key), want)


class VerbTest(unittest.TestCase):
    def test_it_reuses_the_detail_pages_string_and_icon(self):
        """gettext keys on the English, so a second spelling would be a
        second entry for all 86 locales to translate -- and the icon is
        what makes the two doors recognisably the same door."""
        import pathlib

        from jellyfin_mpv_shim.mpvtk_browser.pages import detail

        src = pathlib.Path(detail.__file__).read_text(encoding="utf-8")
        self.assertIn('_("Go to Series")', src)
        self.assertIn('"movie"', src)

        b = _browser()
        entry = next(e for e in b._tile_menu_entries(dict(EPISODE))
                     if e[2] == "goseries")
        self.assertEqual(entry[0], "Go to Series")
        self.assertEqual(entry[1], "movie")


if __name__ == "__main__":
    unittest.main()
