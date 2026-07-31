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

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    GRID_FIELDS, LIBRARY_ITEM_TYPES, LibrarySource)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


class FakeApi:
    def __init__(self):
        self.calls = []
        self.filter_calls = []

    def get_user_items(self, **kw):
        self.calls.append(kw)
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
                self.assertEqual(call["include_item_types"], itype)
                self.assertIs(call["recursive"], True)

    def test_a_folder_is_listed_exactly_as_it_stands(self):
        """A folder inside a library carries no collection type, and
        flattening one would destroy the only structure a Home Videos
        library has. Neither key is sent at all."""
        call = self._call(None)
        self.assertNotIn("include_item_types", call)
        self.assertNotIn("recursive", call)

    def test_an_unknown_collection_type_is_a_folder(self):
        call = self._call("somethingnew")
        self.assertNotIn("recursive", call)

    def test_the_grid_does_not_ask_for_overviews(self):
        """A tile draws a name, a year and a runtime -- none of them fields.
        Overview was a third of the response body for a hundred items."""
        self.assertNotIn("Overview", GRID_FIELDS)
        self.assertIn("PrimaryImageAspectRatio", self._call("movies")["fields"])

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


if __name__ == "__main__":
    unittest.main()
