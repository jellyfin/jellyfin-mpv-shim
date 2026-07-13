"""Tests for the jellyfin-web parity features (filters, shuffle, favorites
plumbing, person/chapter artwork specs) added to the library browser."""

import unittest
from unittest import mock

from jellyfin_mpv_shim.library_browser.repository import (
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
        self.assertEqual(LibrarySource._filter_params(None), {})
        self.assertEqual(LibrarySource._filter_params(
            {"unplayed": False, "favorite": False, "genre": None,
             "letter": None}), {})

    def test_unplayed_and_favorite(self):
        params = LibrarySource._filter_params(
            {"unplayed": True, "favorite": True})
        self.assertEqual(params["Filters"], "IsUnplayed")
        self.assertEqual(params["IsFavorite"], "true")

    def test_genre(self):
        params = LibrarySource._filter_params({"genre": "Drama"})
        self.assertEqual(params["Genres"], "Drama")

    def test_letter_and_hash(self):
        self.assertEqual(LibrarySource._filter_params({"letter": "M"}),
                         {"NameStartsWith": "M"})
        # '#' = everything sorting before 'A' (numbers, punctuation).
        self.assertEqual(LibrarySource._filter_params({"letter": "#"}),
                         {"NameLessThan": "A"})


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
