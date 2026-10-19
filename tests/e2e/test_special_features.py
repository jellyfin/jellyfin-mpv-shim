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
* the trailers route answers both trailers, and an extra plays and reports.

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

FILM = "Movie With Extras"
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
