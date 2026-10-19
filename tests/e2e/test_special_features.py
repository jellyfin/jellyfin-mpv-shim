"""Special Features and trailers against a real server (#759).

The unit suite pins what the browser does with the server's answers; this
pins the answers. Every server fact `docs/jellyfin-api-notes.md` §15 relies
on was read in the server source, and these are the measurements:

* the fixture film's counts, and that the list holds what the counts say,
  in SortName order, each carrying `ExtraType`;
* `GET /Items/{extraId}` carries `ExtraType` too -- the premise of the
  download door (`extras.is_extra`, docs/offline-sync.md §6);
* extras never surface in a library query;
* a season owns the extras in its own folder, on both versions;
* the trailers route answers both trailers, and an extra plays and reports;
* the row and the Trailer button, rendered by a real browser on real DTOs,
  on the film's page and on Season 1's (`_InTheBrowser`);
* the auto-download lookahead's own query holds no extras.

The fixture is stdjflib's `Movie With Extras (2020)`: one extra of every
listed kind, by folder and by suffix, plus `extras/` (Unknown) and two
trailers; and `Season 01/featurettes/` in The Standard Show.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from jellyfin_mpv_shim.extras import is_extra  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import components  # noqa: E402

from test_route_walk import (  # noqa: E402
    ROUTE_ERROR_KEY, _LogTrap, _SyncPool)

FILM = "Movie With Extras"
SHOW = "The Standard Show"
#: Tall enough that no row is virtualized away, so every tile has a handler.
TALL = (1280, 3000)
#: Per kind, from the fixture's folders and suffixes.
KINDS = {"Unknown": 1, "BehindTheScenes": 1, "DeletedScene": 2,
         "Interview": 2, "Featurette": 2, "Short": 1, "Clip": 1, "Scene": 1}


class _Contract:
    """Mixed into one class per server, so the output names the version."""

    SERVER_ENV = "JMS_E2E_SERVER"

    @classmethod
    def setUpClass(cls):
        cls.address = (os.environ.get(cls.SERVER_ENV) or "").rstrip("/")
        if not cls.address:
            raise unittest.SkipTest("%s is not set" % cls.SERVER_ENV)
        cls.session = _e2e.Session(address=cls.address)
        cls.source = cls.session.library_source()
        found = cls.session.find(FILM, library="Movies", item_type="Movie")
        cls.film = cls.source.get_item(_e2e.SOURCE_UUID, found["Id"])
        cls.extras = cls.source.get_special_features(_e2e.SOURCE_UUID,
                                                     found["Id"])

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def test_the_counts_come_back_through_detail_fields(self):
        """Misspelt, a `Fields` entry is dropped without a word
        (api-notes §1) and the gate never opens."""
        self.assertEqual(self.film.get("SpecialFeatureCount"),
                         sum(KINDS.values()))
        self.assertEqual(self.film.get("LocalTrailerCount"), 2)

    def test_the_list_is_what_the_count_says(self):
        self.assertEqual(len(self.extras), self.film["SpecialFeatureCount"])
        got = {}
        for e in self.extras:
            got[e.get("ExtraType")] = got.get(e.get("ExtraType"), 0) + 1
        self.assertEqual(got, KINDS)

    def test_it_is_in_sortname_order(self):
        names = [(e.get("SortName") or e.get("Name") or "").casefold()
                 for e in self.extras]
        self.assertEqual(names, sorted(names))

    def test_every_one_is_an_extra_with_the_fields_its_tile_draws(self):
        for e in self.extras:
            self.assertTrue(is_extra(e), e.get("Name"))
            self.assertIn("UserData", e, e.get("Name"))
            self.assertTrue(e.get("RunTimeTicks"), e.get("Name"))
            caption = components.tile_lines(e)[1]
            if e["ExtraType"] == "Unknown":
                self.assertEqual(caption, "", e.get("Name"))
            else:
                self.assertTrue(caption, e.get("Name"))

    def test_an_extra_fetched_by_id_still_says_it_is_one(self):
        """What the sync door reads: `_expand` fetches a single item by
        id, so a DTO without `ExtraType` there would walk through it."""
        one = self.source.get_item(_e2e.SOURCE_UUID, self.extras[0]["Id"])
        self.assertIsNotNone(one.get("ExtraType"))

    def test_no_extra_surfaces_in_a_library_query(self):
        ids = {e["Id"] for e in self.extras}
        listed = {i["Id"] for i in self.session.find_all(
            library="Movies", item_type="Movie,Video")}
        self.assertFalse(ids & listed)

    def test_the_trailers_route_answers_both(self):
        trailers = self.source.get_trailers(_e2e.SOURCE_UUID, self.film["Id"])
        self.assertEqual(len(trailers), 2)
        self.assertTrue(all(t.get("ExtraType") == "Trailer" for t in trailers))

    def test_a_season_owns_the_extras_in_its_folder(self):
        season = next(
            s for s in self.source.get_seasons(
                _e2e.SOURCE_UUID,
                self.session.find("The Standard Show", library="Shows",
                                  item_type="Series")["Id"])
            if s.get("IndexNumber") == 1)
        item = self.source.get_item(_e2e.SOURCE_UUID, season["Id"])
        self.assertEqual(item.get("SpecialFeatureCount"), 1)
        names = [e.get("Name") for e in self.source.get_special_features(
            _e2e.SOURCE_UUID, season["Id"])]
        self.assertEqual(len(names), 1, names)

    def test_the_sibling_list_carries_the_seasons_counts(self):
        """What the season page gates on: `get_seasons` names the fields,
        and a list route omits them otherwise (api-notes §15)."""
        season = self._season_one()[1]
        self.assertEqual(season.get("SpecialFeatureCount"), 1)

    def test_the_lookahead_query_holds_no_extras(self):
        """The auto-download lookahead's own request (sync/auto.py, the
        `api.get_episodes(..., start_item_id=anchor)` call), cross-season.
        Not `get_episodes` (the SeasonId listing) and not
        `get_series_queue`, which adds filters and fields to the same
        route: the guard is on the request the sync loop makes."""
        from jellyfin_mpv_shim.sync.auto import _FIELDS

        series, season = self._season_one()
        first = self.source.get_episodes(
            _e2e.SOURCE_UUID, series["Id"], season["Id"])[0]
        api = self.source._conn(_e2e.SOURCE_UUID).api
        items = (api.get_episodes(series["Id"], start_item_id=first["Id"],
                                  limit=100, fields=_FIELDS) or {}
                 ).get("Items", [])
        self.assertTrue(items)
        for i in items:
            self.assertEqual(i.get("Type"), "Episode", i.get("Name"))
            self.assertFalse(is_extra(i), i.get("Name"))

    def _season_one(self):
        series = self.session.find(SHOW, library="Shows", item_type="Series")
        season = next(
            s for s in self.source.get_seasons(_e2e.SOURCE_UUID, series["Id"])
            if s.get("IndexNumber") == 1)
        return series, season

    # -- in the browser ---------------------------------------------------
    #
    # Shaped like test_route_walk.py: a real browser over this class's
    # source -- not a new Session, whose login would revoke this one's --
    # loaded inline, built and laid out for real, and failed on a route
    # error or a logged failure as well as on a raise.

    def _browser(self):
        import logging

        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        trap = _LogTrap()
        root = logging.getLogger()
        root.addHandler(trap)
        self.addCleanup(root.removeHandler, trap)
        self.session.register_source_login(self)
        b = MpvtkBrowser(app=None, source=self.source,
                         server_uuid=_e2e.SOURCE_UUID)
        # The home load is already on the real pool; drain it before going
        # inline, or it lands after the class has logged out.
        b._async._pool.shutdown(wait=True, cancel_futures=True)
        b._pool = _SyncPool()
        self.addCleanup(self._shutdown, b)
        b._load_route(b.route)
        trap.drain()
        return b, trap

    @staticmethod
    def _shutdown(b):
        try:
            b.shutdown()
        except Exception:
            pass

    def _render(self, b, trap, label):
        from jellyfin_mpv_shim.mpvtk.layout import layout

        self.assertFalse(b.route.get("_loading"), label)
        nodes, handlers = layout(b.build(TALL), *TALL)
        self.assertIsNone(b.route.get(ROUTE_ERROR_KEY), label)
        self.assertEqual(trap.describe(), [], label)
        trap.drain()
        return nodes, handlers

    def _go(self, b, trap, route):
        b.navigate(dict(route, server=_e2e.SOURCE_UUID))
        self.assertEqual(b.route.get("kind"), route["kind"])
        return self._render(b, trap, "%(kind)s %(item_id)s" % route)

    def test_the_films_page_draws_the_row_and_plays_its_trailers(self):
        b, trap = self._browser()
        _nodes, handlers = self._go(b, trap, {
            "kind": "detail", "item_id": self.film["Id"], "title": FILM})
        tiles = [n.get("id") for n in _nodes
                 if (n.get("id") or "").startswith("detail-extras-")]
        self.assertEqual(tiles, ["detail-extras-" + e["Id"]
                                 for e in self.extras])

        played = []
        b._actions.play_list = lambda ids, srv, i, **kw: played.append(
            list(ids))
        handlers["btn-trailer"]["click"]()
        self.assertEqual(played, [[t["Id"] for t in self.source.get_trailers(
            _e2e.SOURCE_UUID, self.film["Id"])]])

        second = self.extras[1]["Id"]
        handlers["detail-extras-" + second]["click"]()
        self.assertEqual((b.route["kind"], b.route["item_id"]),
                         ("detail", second))
        nodes, _h = self._render(b, trap, "the extra's own page")
        self.assertNotIn("act-download", [n.get("id") for n in nodes])

    def test_season_ones_page_draws_its_row(self):
        b, trap = self._browser()
        series, season = self._season_one()
        nodes, _h = self._go(b, trap, {
            "kind": "season", "item_id": season["Id"],
            "series_id": series["Id"], "title": season.get("Name", "")})
        tiles = [n.get("id") for n in nodes
                 if (n.get("id") or "").startswith("season-extras-")]
        self.assertEqual(len(tiles), 1, tiles)


@_e2e.require_server
class SpecialFeaturesContractTest(_Contract, unittest.TestCase):
    pass


class SpecialFeaturesContractAltTest(_Contract, unittest.TestCase):
    """The same against JMS_E2E_SERVER_ALT (10.11); skipped when unset."""

    SERVER_ENV = "JMS_E2E_SERVER_ALT"


@_e2e.require_server_and_mpv
class PlayingAnExtraTest(_e2e.E2ETestCase):
    def _film_extras(self):
        film = self.session.find(FILM, library="Movies", item_type="Movie")
        source = self.session.library_source()
        self.addCleanup(source.stop)
        return film, source

    def test_an_extra_plays_and_the_server_hears_about_it(self):
        film, source = self._film_extras()
        extra = next(e for e in source.get_special_features(
            _e2e.SOURCE_UUID, film["Id"]) if e["ExtraType"] == "Featurette")
        self.session.reset_played(extra["Id"])
        self.addCleanup(self.session.reset_played, extra["Id"])
        before = self.session.user_data(extra["Id"]).get("LastPlayedDate")
        self.pm.play(_e2e.build_media(self.session, [extra["Id"]]).video,
                     is_initial_play=True)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the extra")
        self.pump_until(lambda: (self.pm._player.playback_time or 0) > 1)
        self.pm.stop()
        self.assertTrue(
            _e2e.wait_for(lambda: self.session.user_data(extra["Id"]).get(
                "LastPlayedDate") not in (None, before), timeout=20),
            "the server recorded no playback of the extra")

    def test_the_local_trailers_play_as_one_queue(self):
        film, source = self._film_extras()
        ids = [t["Id"] for t in source.get_trailers(_e2e.SOURCE_UUID,
                                                    film["Id"])]
        self.assertEqual(len(ids), 2)
        self.addCleanup(self.session.reset_played, *ids)
        media = _e2e.build_media(self.session, ids)
        self.pm.play(media.video, is_initial_play=True)
        self.assertTrue(_e2e.wait_for(lambda: self.pm._player.duration),
                        "mpv never opened the first trailer")
        self.assertEqual(len(media.queue), 2)


if __name__ == "__main__":
    unittest.main()
