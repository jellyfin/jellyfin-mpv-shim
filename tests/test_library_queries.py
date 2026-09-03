"""What a library grid actually asks the server for.

Two measurements against a real server drove this, both on a library the
grid could not draw in under eight seconds:

* One page of a 1334-film library took 8.0s, and 0.3s once the query named
  the item type. The films were never the cost -- the library's own *folders*
  came back with them, and the server builds a Folder's UserData by walking
  everything underneath it.
* Items/Filters, which fills the genre and year pickers, took 3.7s on a
  950-series library while the tiles sat behind a spinner waiting for it, and
  0.4s scoped to the type the grid lists.

So: name the type, and do not make the first frame wait on the pickers.
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

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    GRID_FIELDS, LIBRARY_ITEM_TYPES, LIST_FIELDS, LibrarySource)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.filter_calls = []

    def items(self, handler="", action="GET", params=None, **_kw):
        # `items`, not `get_user_items`: the shim queries GET /Items now
        # (see jellyfin_mpv_shim/items_api). So what is recorded is the
        # real query dict, in the server's own spelling, rather than the
        # apiclient's keyword names.
        self.calls.append(dict(params or {}))
        return {"Items": [], "TotalRecordCount": 0}

    def get_filters(self, parent_id=None, include_item_types=None):
        self.filter_calls.append((parent_id, include_item_types))
        return {"Genres": ["Action"], "Years": [2020]}


class QueryShapeTest(unittest.TestCase):
    def _source(self, api):
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        return src

    def _call(self, collection_type):
        api = FakeApi()
        self._source(api).get_library_items("srv", "lib",
                                            collection_type=collection_type)
        return api.calls[0]

    def test_a_library_root_names_its_item_type(self):
        for ctype, itype in LIBRARY_ITEM_TYPES.items():
            with self.subTest(ctype):
                call = self._call(ctype)
                self.assertEqual(call["IncludeItemTypes"], itype)
                self.assertIs(call["Recursive"], True)

    def test_a_folder_is_listed_exactly_as_it_stands(self):
        """A folder inside a library carries no collection type, and
        flattening one would destroy the only structure a Home Videos
        library has. Neither key is sent at all."""
        call = self._call(None)
        self.assertNotIn("IncludeItemTypes", call)
        self.assertNotIn("Recursive", call)

    def test_an_unknown_collection_type_is_a_folder(self):
        call = self._call("somethingnew")
        self.assertNotIn("Recursive", call)

    def test_the_grid_does_not_ask_for_overviews(self):
        """A tile draws a name, a year and a runtime -- none of them fields.
        Overview was a third of the response body for a hundred items."""
        self.assertNotIn("Overview", GRID_FIELDS)
        self.assertIn("PrimaryImageAspectRatio", self._call("movies")["Fields"])

    def test_every_browse_query_asks_for_the_version_count(self):
        """MediaSourceCount is not one of the unconditional properties: leave
        it out of Fields and the DTO carries no count at all, so a film the
        library holds twice draws exactly like one it holds once. Both field
        sets, because a film reaches the screen through a row as often as
        through a grid."""
        self.assertIn("MediaSourceCount", self._call("movies")["Fields"])
        self.assertIn("MediaSourceCount", GRID_FIELDS)
        self.assertIn("MediaSourceCount", LIST_FIELDS)

    def test_the_filter_pickers_are_scoped_to_the_type(self):
        api = FakeApi()
        self._source(api).get_filter_values("srv", "lib",
                                            collection_type="tvshows")
        self.assertEqual(api.filter_calls, [("lib", "Series")])

    def test_filter_values_still_work_untyped(self):
        api = FakeApi()
        vals = self._source(api).get_filter_values("srv", "lib")
        self.assertEqual(api.filter_calls, [("lib", None)])
        self.assertEqual(vals["genres"], ["Action"])


class MusicOrderTest(unittest.TestCase):
    """How a music library's tabs are ordered.

    SortName is right for everything in this file except one tab. A track's
    SortName is not its title: the server builds it from the disc and track
    numbers with the title only as a tie-break, which is what makes an
    album's own listing come out in play order. Ask a whole library for it
    and you get every album's track 1, then every album's track 2.
    """

    def _call(self, method, **kw):
        api = FakeApi()
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        getattr(src, method)("srv", "lib", **kw)
        return api.calls[0]

    def test_the_songs_tab_is_ordered_by_title(self):
        self.assertEqual(self._call("get_songs")["SortBy"], "Name")

    def test_the_albums_tab_keeps_sortname(self):
        """An album's SortName IS its name, modulo the leading article --
        this is only a track's problem."""
        self.assertEqual(self._call("get_music_albums")["SortBy"], "SortName")

    def test_an_explicit_sort_still_wins(self):
        self.assertEqual(
            self._call("get_songs", sort_by="DateCreated")["SortBy"],
            "DateCreated")


class FirstPaintTest(unittest.TestCase):
    """The tiles must not wait on the pickers."""

    def _browser(self, ctype="tvshows"):
        src = FakeSource()
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        return b, src

    def test_the_items_are_published_before_the_pickers_are_asked_for(self):
        b, src = self._browser()
        seen = {}
        real = src.get_filter_values

        def spy(*a, **kw):
            seen["items"] = b.route.get("_items")
            return real(*a, **kw)

        src.get_filter_values = spy
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "tvshows", "title": "Shows"})
        self.assertIsNotNone(
            seen.get("items"),
            "the grid was still on a spinner while Items/Filters ran")

    def test_the_pickers_still_land(self):
        b, src = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "tvshows", "title": "Shows"})
        self.assertEqual((b.route.get("_filtervals") or {}).get("genres"),
                         ["Action", "Comedy"])

    def test_the_library_says_what_kind_it_is(self):
        b, src = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "tvshows", "title": "Shows"})
        self.assertEqual([q["collection_type"] for q in src.queries],
                         ["tvshows"])
        self.assertEqual(src.filter_value_calls, [("lib1", "tvshows")])

    def test_a_second_load_does_not_re_ask_for_the_pickers(self):
        """They are a property of the library, not of the sort or the
        filters, and the endpoint is the slow one."""
        b, src = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "tvshows", "title": "Shows"})
        b._page_for(b.route)._set("_sort", 1)
        self.assertEqual(len(src.filter_value_calls), 1)


class TvSortTest(unittest.TestCase):
    """"Date Added" on a TV library is when the series was created, which
    for a show you have followed for years is years ago. The question that
    library is actually asked -- what has new episodes -- had no answer."""

    def _grid(self, ctype):
        src = FakeSource()
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": ctype, "title": "L"})
        return b, src

    def _labels(self, b):
        from jellyfin_mpv_shim.mpvtk_browser.pages.grid import sorts_for
        return [s[0] for s in sorts_for(b.route.get("collection_type"))]

    def test_a_tv_library_offers_it(self):
        b, _src = self._grid("tvshows")
        self.assertIn("Date Episode Added", self._labels(b))

    def test_a_movie_library_does_not(self):
        b, _src = self._grid("movies")
        self.assertNotIn("Date Episode Added", self._labels(b))

    def test_the_base_sorts_keep_their_indices(self):
        """A route stores its sort as an index. Anything inserted rather
        than appended would silently re-point every route carrying one."""
        from jellyfin_mpv_shim.mpvtk_browser.pages.grid import SORTS, sorts_for
        self.assertEqual(sorts_for("tvshows")[:len(SORTS)], SORTS)

    def test_choosing_it_sorts_by_the_newest_episode(self):
        b, src = self._grid("tvshows")
        labels = self._labels(b)
        b._page_for(b.route)._set("_sort", labels.index("Date Episode Added"))
        self.assertEqual((src.queries[-1]["sort_by"],
                          src.queries[-1]["sort_order"]),
                         ("DateLastContentAdded", "Descending"))

    def test_the_name_is_the_servers(self):
        """DateLastMediaAdded is not in the server's sort enum, and an
        unknown sort is ignored rather than refused -- the grid would come
        back in name order with nothing to say it had."""
        from jellyfin_mpv_shim.mpvtk_browser.pages.grid import EXTRA_SORTS
        self.assertEqual([s[1] for s in EXTRA_SORTS["tvshows"]],
                         ["DateLastContentAdded"])


if __name__ == "__main__":
    unittest.main()


class SearchAsksForFieldsTest(unittest.TestCase):
    """Search asked for **no fields at all** (#14).

    Every other list names what it wants; this one named nothing, and the
    cost was three things at once rather than one. Measured against a real
    800-item search: +47 KB on a 1.28 MB response, and not slower.

    Found while adding `CanDelete` for Delete from Disk — the entry could
    not appear on a search result the way it does everywhere else, and
    pulling that thread showed the other two.
    """

    def _search_call(self):
        api = FakeApi()
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        src.search("srv", "the")
        return api.calls[0]

    def test_it_asks_for_the_aspect_ratio(self):
        """Absent on *every* result before this, so search tiles have never
        been shaped by their own artwork — auto_geom fell back for all of
        them, which is why a row of films and a row of episodes looked the
        same shape."""
        self.assertIn("PrimaryImageAspectRatio", self._search_call()["Fields"])

    def test_it_asks_for_the_version_count(self):
        # The one someone actually reported: a multi-version item drew no
        # version chip in search results.
        self.assertIn("MediaSourceCount", self._search_call()["Fields"])

    def test_it_asks_whether_the_item_can_be_deleted(self):
        self.assertIn("CanDelete", self._search_call()["Fields"])

    def test_it_does_not_ask_for_overviews(self):
        """Search asks for 800 items — five times a grid page — so the one
        field the grid drops for being a third of the body is the one this
        must not add back."""
        self.assertNotIn("Overview", self._search_call()["Fields"])
