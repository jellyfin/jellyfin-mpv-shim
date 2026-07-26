"""Offline browsing: seasons resolve, and coming back online recovers.

Two failures that only showed up with the server away:

* a Season opened offline listed no episodes at all ("Nothing here yet."),
  because the offline Season DTO carried no SeriesId — which the route
  builder reads, and get_episodes then filters against;
* reconnecting kept the browser pointed at the "offline" sentinel server,
  so every subsequent call hit KeyError: 'offline' until a restart.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the browser reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import home_sections
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    OfflineLibrarySource,
    _OfflineSnapshot,
)

SERIES_ID = "series-1"

EPISODES = [
    {"Id": "e1", "Type": "Episode", "Name": "One", "SeriesId": SERIES_ID,
     "SeriesName": "A Show", "SeasonId": "season-1", "ParentIndexNumber": 1,
     "IndexNumber": 1, "UserData": {}},
    {"Id": "e2", "Type": "Episode", "Name": "Two", "SeriesId": SERIES_ID,
     "SeriesName": "A Show", "SeasonId": "season-1", "ParentIndexNumber": 1,
     "IndexNumber": 2, "UserData": {}},
    {"Id": "e3", "Type": "Episode", "Name": "Special", "SeriesId": SERIES_ID,
     "SeriesName": "A Show", "SeasonId": "season-0", "ParentIndexNumber": 0,
     "IndexNumber": 1, "UserData": {}},
]


def offline_source(items=EPISODES):
    src = OfflineLibrarySource.__new__(OfflineLibrarySource)
    src._snap = _OfflineSnapshot(items=list(items))
    return src


class OfflineSeasonTest(unittest.TestCase):
    def test_a_season_knows_its_series(self):
        """app.py builds the season route from item["SeriesId"]. Without it
        the route carries series_id=None and get_episodes discards every
        episode, which is what showed as an empty season."""
        seasons = offline_source().get_seasons("offline", SERIES_ID)
        self.assertTrue(seasons)
        for season in seasons:
            self.assertEqual(season.get("SeriesId"), SERIES_ID,
                             "the season tile cannot route to its episodes")

    def test_opening_a_season_lists_its_episodes(self):
        src = offline_source()
        seasons = src.get_seasons("offline", SERIES_ID)
        season = next(s for s in seasons if s["Id"] == "season-1")
        # Exactly what _load_season does with the route app.py built.
        episodes = src.get_episodes("offline", season["SeriesId"],
                                    season["Id"])
        self.assertEqual([e["Id"] for e in episodes], ["e1", "e2"])

    def test_the_specials_season_resolves_too(self):
        src = offline_source()
        seasons = src.get_seasons("offline", SERIES_ID)
        specials = next(s for s in seasons if s["Id"] == "season-0")
        episodes = src.get_episodes("offline", specials["SeriesId"],
                                    specials["Id"])
        self.assertEqual([e["Id"] for e in episodes], ["e3"])

    def test_a_season_without_a_real_season_id_still_resolves(self):
        """Synthetic "p<n>" ids take the other branch of get_episodes."""
        items = [dict(e) for e in EPISODES]
        for e in items:
            e.pop("SeasonId")
        src = offline_source(items)
        seasons = src.get_seasons("offline", SERIES_ID)
        for season in seasons:
            self.assertEqual(season.get("SeriesId"), SERIES_ID)
            episodes = src.get_episodes("offline", season["SeriesId"],
                                        season["Id"])
            self.assertTrue(episodes,
                            "a synthetic season id resolved to nothing")


class FakeSource:
    def __init__(self, servers):
        self._servers = servers

    def servers(self):
        return list(self._servers)

    def get_libraries(self, server_uuid):
        return []

    def get_home_prefs(self, server_uuid, refresh=False):
        return list(home_sections.DEFAULT_LAYOUT), frozenset()

    def get_home_rows(self, server_uuid, libraries=None, sections=None,
                      layout=None, latest_excludes=None):
        return []


class FakeController:
    def __init__(self, last_server=None):
        self.last_server = last_server

    def get_last_server(self):
        return self.last_server

    def set_last_server(self, uuid):
        self.last_server = uuid


LIVE = [{"uuid": "srv1", "name": "Home"}, {"uuid": "srv2", "name": "Other"}]


class ReconnectAfterOfflineTest(unittest.TestCase):
    """The reconnect path passes the CURRENT selection back in, and offline
    that selection is OfflineLibrarySource's "offline" sentinel. Handing it
    to a live source made every call raise KeyError: 'offline'."""

    def _browser(self, controller=None):
        return MpvtkBrowser(None, FakeSource(LIVE),
                            controller=controller or FakeController())

    def test_the_offline_sentinel_is_not_carried_into_a_live_source(self):
        b = self._browser()
        picked = b._pick_server(LIVE, server_uuid="offline")
        self.assertIn(picked, {"srv1", "srv2"})
        self.assertNotEqual(picked, "offline")

    def test_the_remembered_server_wins_when_the_request_is_stale(self):
        b = self._browser(FakeController(last_server="srv2"))
        self.assertEqual(b._pick_server(LIVE, server_uuid="offline"), "srv2")

    def test_a_server_this_source_has_is_still_honoured(self):
        b = self._browser(FakeController(last_server="srv1"))
        self.assertEqual(b._pick_server(LIVE, server_uuid="srv2"), "srv2",
                         "an explicit, valid request was overridden")

    def test_a_removed_server_falls_back_rather_than_sticking(self):
        b = self._browser()
        self.assertIn(b._pick_server(LIVE, server_uuid="deleted"),
                      {"srv1", "srv2"})

    def test_going_offline_still_selects_the_offline_server(self):
        """The sentinel is legitimate when the offline source IS the source."""
        b = self._browser()
        offline_servers = [{"uuid": "offline", "name": "Downloaded"}]
        self.assertEqual(
            b._pick_server(offline_servers, server_uuid="offline"), "offline")


if __name__ == "__main__":
    unittest.main()
