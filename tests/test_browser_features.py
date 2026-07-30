"""Tests for the jellyfin-web parity features (filters, shuffle, favorites
plumbing, person/chapter artwork specs) added to the library browser."""

import unittest
from unittest import mock

from jellyfin_mpv_shim.mpvtk_browser.repository import (
    LibrarySource, OfflineLibrarySource, _OfflineSnapshot,
)


def _item(name, itype="Movie", played=False, favorite=False, genres=None,
          item_id=None):
    return {"Id": item_id or name, "Name": name, "Type": itype,
            "Genres": genres or [],
            "UserData": {"Played": played, "IsFavorite": favorite}}


def _offline_source(items):
    src = OfflineLibrarySource.__new__(OfflineLibrarySource)
    src.catalog_path = None
    src.root = None
    src._snap = _OfflineSnapshot(items=items)
    return src


class FilterParamsTest(unittest.TestCase):
    def test_empty_filters(self):
        self.assertEqual(LibrarySource._filter_kwargs(None), {})
        self.assertEqual(LibrarySource._filter_kwargs(
            {"unplayed": False, "favorite": False, "genre": None,
             "letter": None}), {})

    def test_unplayed_and_favorite(self):
        kwargs = LibrarySource._filter_kwargs(
            {"unplayed": True, "favorite": True})
        self.assertEqual(kwargs["filters"], "IsUnplayed")
        self.assertEqual(kwargs["is_favorite"], "true")

    def test_genre(self):
        kwargs = LibrarySource._filter_kwargs({"genre": "Drama"})
        self.assertEqual(kwargs["genres"], "Drama")

    def test_letter_and_hash(self):
        self.assertEqual(LibrarySource._filter_kwargs({"letter": "M"}),
                         {"name_starts_with": "M"})
        # '#' = everything sorting before 'A' (numbers, punctuation).
        self.assertEqual(LibrarySource._filter_kwargs({"letter": "#"}),
                         {"name_less_than": "A"})

    def test_year(self):
        self.assertEqual(LibrarySource._filter_kwargs({"year": 1999}),
                         {"years": "1999"})


class OfflineFiltersTest(unittest.TestCase):
    def setUp(self):
        self.src = _offline_source([
            _item("Alpha", played=True, favorite=True, genres=["Drama"]),
            _item("Beta", played=False, genres=["Comedy"]),
            _item("42", played=False),
        ])

    def _names(self, filters):
        items, total = self.src.get_library_items(
            "offline", "offline:movies", filters=filters)
        return [i["Name"] for i in items]

    def test_no_filters(self):
        self.assertEqual(self._names(None), ["42", "Alpha", "Beta"])

    def test_unplayed(self):
        self.assertEqual(self._names({"unplayed": True}), ["42", "Beta"])

    def test_favorite(self):
        self.assertEqual(self._names({"favorite": True}), ["Alpha"])

    def test_genre(self):
        self.assertEqual(self._names({"genre": "Comedy"}), ["Beta"])

    def test_letter(self):
        self.assertEqual(self._names({"letter": "B"}), ["Beta"])
        self.assertEqual(self._names({"letter": "#"}), ["42"])

    def test_total_reflects_filtering(self):
        _items, total = self.src.get_library_items(
            "offline", "offline:movies", filters={"unplayed": True})
        self.assertEqual(total, 2)

    def test_year_filter(self):
        src = _offline_source([
            dict(_item("Old"), ProductionYear=1999),
            dict(_item("New"), ProductionYear=2024),
        ])
        items, _t = src.get_library_items("offline", "offline:movies",
                                          filters={"year": 1999})
        self.assertEqual([i["Name"] for i in items], ["Old"])


class ApiVersionGatingTest(unittest.TestCase):
    """Filter values degrade to a genre list when Items/Filters is
    unavailable — the endpoint postdates the servers we still support."""

    def _src_with_api(self, api):
        src = LibrarySource.__new__(LibrarySource)
        conn = mock.Mock(spec=["api"])
        conn.api = api
        src._conns = {"srv": conn}
        return src

    def test_filter_values_fall_back_to_genres(self):
        old_api = mock.Mock(spec=["get_genres"])
        old_api.get_genres.return_value = {"Items": [{"Name": "Drama"}]}
        src = self._src_with_api(old_api)
        self.assertEqual(src.get_filter_values("srv"),
                         {"genres": ["Drama"], "years": []})

    def test_filter_values_use_items_filters_when_available(self):
        api = mock.Mock(spec=["get_filters"])
        api.get_filters.return_value = {"Genres": ["Drama"],
                                        "Years": [2001, 1999]}
        src = self._src_with_api(api)
        self.assertEqual(src.get_filter_values("srv"),
                         {"genres": ["Drama"], "years": [2001, 1999]})

    def test_offline_filter_values_derive_years(self):
        src = _offline_source([
            dict(_item("A"), ProductionYear=1999, Genres=["Drama"]),
            dict(_item("B"), ProductionYear=2024),
        ])
        values = src.get_filter_values("offline")
        self.assertEqual(values["years"], [2024, 1999])


class OfflineGenresAndShuffleTest(unittest.TestCase):
    def test_genres_are_distinct_sorted(self):
        src = _offline_source([
            _item("A", genres=["Drama", "Comedy"]),
            _item("B", genres=["Drama"]),
        ])
        self.assertEqual(src.get_genres("offline"), ["Comedy", "Drama"])

    def test_shuffle_ids_scoped_to_library(self):
        eps = [dict(_item("E%d" % i, itype="Episode"), SeriesId="S")
               for i in range(5)]
        movies = [_item("M1"), _item("M2")]
        src = _offline_source(eps + movies)
        self.assertEqual(set(src.get_shuffle_ids("offline", "offline:tv")),
                         {e["Id"] for e in eps})
        self.assertEqual(set(src.get_shuffle_ids("offline", "offline:movies")),
                         {"M1", "M2"})
        self.assertEqual(src.get_shuffle_ids("offline", "offline:playlists"),
                         [])


class PersonImageSpecTest(unittest.TestCase):
    def test_person_primary_image_tag(self):
        # People entries carry PrimaryImageTag instead of ImageTags.
        src = LibrarySource.__new__(LibrarySource)
        person = {"Id": "p1", "Name": "Actor", "PrimaryImageTag": "t9"}
        self.assertEqual(src.image_spec(person),
                         ("p1", "Primary", "t9"))

    def test_regular_items_unaffected(self):
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "i1", "ImageTags": {"Primary": "t1"}}
        self.assertEqual(src.image_spec(item), ("i1", "Primary", "t1"))


class ParentPrimaryImageSpecTest(unittest.TestCase):
    """An episode drawn as its show wants the show's *poster*:
    /Items/{ParentBackdropItemId}/Images/Primary."""

    def test_an_episode_takes_its_series_poster(self):
        src = LibrarySource.__new__(LibrarySource)
        ep = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
              "SeriesId": "S1", "SeriesPrimaryImageTag": "t1",
              "ParentBackdropItemId": "S1", "ParentBackdropImageTags": ["b1"]}
        self.assertEqual(src.image_spec(ep, "ParentPrimary"),
                         ("S1", "Primary", "t1"))

    def test_the_backdrop_owner_stands_in_for_a_missing_series_id(self):
        src = LibrarySource.__new__(LibrarySource)
        ep = {"Id": "e1", "Type": "Episode",
              "ParentBackdropItemId": "S1", "ParentBackdropImageTags": ["b1"]}
        self.assertEqual(src.image_spec(ep, "ParentPrimary"),
                         ("S1", "Primary", None))

    def test_no_parent_falls_through_to_the_items_own_art(self):
        # Otherwise a stray item in the row draws a letter glyph.
        src = LibrarySource.__new__(LibrarySource)
        ep = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"}}
        self.assertEqual(src.image_spec(ep, "ParentPrimary"),
                         ("e1", "Primary", "own"))


class ProgramPosterSpecTest(unittest.TestCase):
    def test_a_program_thumb_beats_the_channel_logo(self):
        """Live TV is a poster row now; a program carrying only a Thumb must
        still show it rather than falling through to the channel."""
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "p1", "Type": "Program", "ImageTags": {"Thumb": "own"},
                "ParentThumbItemId": "c1", "ParentThumbImageTag": "pt",
                "ChannelId": "c1", "ChannelPrimaryImageTag": "cp"}
        self.assertEqual(src.image_spec(item, "Primary"),
                         ("p1", "Thumb", "own"))

    def test_a_program_primary_still_wins(self):
        src = LibrarySource.__new__(LibrarySource)
        item = {"Id": "p1", "Type": "Program",
                "ImageTags": {"Primary": "own", "Thumb": "t"}}
        self.assertEqual(src.image_spec(item, "Primary"),
                         ("p1", "Primary", "own"))


class ThumbChainTest(unittest.TestCase):
    """A "Thumb" request means "this is a landscape tile" -- jellyfin-web's
    preferThumb: true. Every landscape-shaped candidate must be tried before
    the item's own poster, or a 2:3 image lands in a 16:9 tile.

    The old chain went own Thumb -> own Primary and stopped, which is web's
    step 6 taken as step 2. Recorded TV showed it worst: an Episode usually
    carries only a Primary while its Series carries Thumb and Backdrop.
    """

    def spec(self, item, **kw):
        return LibrarySource.__new__(LibrarySource).image_spec(
            item, "Thumb", **kw)

    # -- the ladder, in order (url.ts:39-82) ------------------------------

    def test_1_own_thumb_wins(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Thumb": "own"},
                "SeriesId": "S1", "SeriesThumbImageTag": "st"}
        self.assertEqual(self.spec(item), ("e1", "Thumb", "own"))

    def test_2_series_thumb_beats_the_episode_still(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "SeriesId": "S1", "SeriesThumbImageTag": "st"}
        self.assertEqual(self.spec(item), ("S1", "Thumb", "st"))

    def test_3_parent_thumb_beats_the_episode_still(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "ParentThumbItemId": "SE1", "ParentThumbImageTag": "pt"}
        self.assertEqual(self.spec(item), ("SE1", "Thumb", "pt"))

    def test_4_own_backdrop_beats_the_episode_still(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "BackdropImageTags": ["b1", "b2"]}
        self.assertEqual(self.spec(item), ("e1", "Backdrop", "b1"))

    def test_5_parent_backdrop_beats_the_episode_still(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "ParentBackdropItemId": "S1",
                "ParentBackdropImageTags": ["pb"]}
        self.assertEqual(self.spec(item), ("S1", "Backdrop", "pb"))

    def test_6_own_primary_is_the_fallback_not_the_first_answer(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"}}
        self.assertEqual(self.spec(item), ("e1", "Primary", "own"))

    # -- the gates --------------------------------------------------------

    def test_parent_backdrop_is_episodes_only_like_web(self):
        """url.ts:67 gates this one on Type === 'Episode'. A Movie with a
        parent backdrop must not take it."""
        item = {"Id": "m1", "Type": "Movie", "ImageTags": {"Primary": "own"},
                "ParentBackdropItemId": "C1",
                "ParentBackdropImageTags": ["pb"]}
        self.assertEqual(self.spec(item), ("m1", "Primary", "own"))

    def test_a_photos_parent_thumb_is_not_borrowed(self):
        """url.ts:59 -- a photo's parent is its album, whose thumb is some
        other photo entirely."""
        item = {"Id": "ph1", "Type": "Photo", "MediaType": "Photo",
                "ImageTags": {"Primary": "own"},
                "ParentThumbItemId": "A1", "ParentThumbImageTag": "pt"}
        self.assertEqual(self.spec(item), ("ph1", "Primary", "own"))

    # -- inherit=False (the episode-images setting turned ON) -------------

    def test_inherit_off_skips_series_and_season_thumbs(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "SeriesId": "S1", "SeriesThumbImageTag": "st",
                "ParentThumbItemId": "SE1", "ParentThumbImageTag": "pt"}
        self.assertEqual(self.spec(item, inherit=False),
                         ("e1", "Primary", "own"))

    def test_inherit_off_keeps_the_items_own_backdrop(self):
        """Only *inherited* art is disabled -- the episode's own backdrop is
        still a better landscape image than its poster (url.ts:63 is not
        gated on inheritThumb)."""
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "BackdropImageTags": ["b1"]}
        self.assertEqual(self.spec(item, inherit=False),
                         ("e1", "Backdrop", "b1"))

    def test_inherit_off_skips_the_parent_backdrop(self):
        item = {"Id": "e1", "Type": "Episode", "ImageTags": {"Primary": "own"},
                "ParentBackdropItemId": "S1",
                "ParentBackdropImageTags": ["pb"]}
        self.assertEqual(self.spec(item, inherit=False),
                         ("e1", "Primary", "own"))

    # -- what must not have moved ----------------------------------------

    def test_the_channel_logo_is_still_the_last_resort(self):
        """Ours, not web's -- see the Live TV notes. It must stay below every
        real candidate or a programme with artwork shows a logo instead."""
        item = {"Id": "p1", "Type": "Program", "ChannelId": "c1",
                "ChannelPrimaryImageTag": "cp"}
        self.assertEqual(self.spec(item), ("c1", "Primary", "cp"))

    def test_a_backdrop_outranks_the_channel_logo(self):
        item = {"Id": "p1", "Type": "Program", "BackdropImageTags": ["b1"],
                "ChannelId": "c1", "ChannelPrimaryImageTag": "cp"}
        self.assertEqual(self.spec(item), ("p1", "Backdrop", "b1"))

    def test_a_movie_with_only_a_poster_still_gets_its_poster(self):
        item = {"Id": "m1", "Type": "Movie", "ImageTags": {"Primary": "p"}}
        self.assertEqual(self.spec(item), ("m1", "Primary", "p"))

    def test_nothing_at_all_is_still_none(self):
        self.assertIsNone(self.spec({"Id": "x1", "Type": "Episode"}))


class PrimaryChainUnchangedTest(unittest.TestCase):
    """The Primary chain is deliberately NOT web's. Two differences, both
    load-bearing, both easy to "fix" by accident later."""

    def spec(self, item):
        return LibrarySource.__new__(LibrarySource).image_spec(item, "Primary")

    def test_a_poster_request_never_returns_a_backdrop(self):
        """web's Primary tail reaches BackdropImageTags (url.ts:116); ours
        must not. A 16:9 backdrop cropped into a 2:3 tile is the same defect
        the Thumb chain was fixed for, in the other direction."""
        item = {"Id": "m1", "Type": "Movie", "BackdropImageTags": ["b1"]}
        self.assertIsNone(self.spec(item))

    def test_a_poster_request_still_takes_the_items_own_thumb(self):
        item = {"Id": "p1", "Type": "Program", "ImageTags": {"Thumb": "t"}}
        self.assertEqual(self.spec(item), ("p1", "Thumb", "t"))


class BackdropIndexTest(unittest.TestCase):
    def test_a_backdrop_spec_builds_an_indexed_url(self):
        """image_spec's 3-tuple has nowhere to say "index 0", so image_url
        supplies it -- the cached bitmap is keyed on the tag, and only the
        indexed URL is guaranteed to serve the image that tag names."""
        seen = {}

        class FakeApi:
            def image_url(self, item_id, image_type, index=None, tag=None,
                          max_width=None, fill_width=None, fill_height=None,
                          quality=90):
                seen.update(item_id=item_id, image_type=image_type,
                            index=index, tag=tag)
                return "url"

        src = LibrarySource.__new__(LibrarySource)
        src._conns = {"srv": type("C", (), {"api": FakeApi()})()}
        src.image_url("srv", "e1", "Backdrop", "b1", 240, 135, fill=True)
        self.assertEqual(seen["index"], 0)
        seen.clear()
        src.image_url("srv", "e1", "Primary", "p1", 150, 225, fill=True)
        self.assertIsNone(seen["index"])


class ChapterImageUrlTest(unittest.TestCase):
    def test_no_tag_no_url(self):
        src = LibrarySource.__new__(LibrarySource)
        self.assertIsNone(src.chapter_image_url("srv", "i1", 0, {}))

    def test_tagged_chapter_builds_indexed_url(self):
        src = LibrarySource.__new__(LibrarySource)
        conn = mock.Mock()
        conn.api.image_url.return_value = "http://x/chapter"
        src._conns = {"srv": conn}
        url = src.chapter_image_url("srv", "i1", 3, {"ImageTag": "ct"},
                                    width=320)
        self.assertEqual(url, "http://x/chapter")
        _args, kwargs = conn.api.image_url.call_args
        self.assertEqual(kwargs.get("index"), 3)
        self.assertEqual(kwargs.get("tag"), "ct")

    def test_offline_has_no_chapter_art(self):
        src = _offline_source([])
        self.assertIsNone(src.chapter_image_url("offline", "i1", 0,
                                                {"ImageTag": "ct"}))


class HomeRowsLibrariesParamTest(unittest.TestCase):
    def test_offline_accepts_libraries_kwarg(self):
        src = _offline_source([_item("M1")])
        rows = src.get_home_rows("offline", libraries=[])
        self.assertEqual(rows[0]["title"] is not None, True)
        self.assertEqual([i["Name"] for i in rows[0]["items"]], ["M1"])


if __name__ == "__main__":
    unittest.main()
