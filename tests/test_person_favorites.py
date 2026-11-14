"""Favorite people from their credits, and find them again in Favorites."""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import copy
import unittest

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
from tests._shell_harness import (
    FakeController, FakeSource, _DeferredPool, _SyncPool, build_scene,
    ids, menu_pick,
)


PERSON = {"Id": "p1", "Name": "Actor One", "Type": "Person",
          "UserData": {"IsFavorite": True}}


class PersonFavoritesTest(unittest.TestCase):
    def setUp(self):
        self.src = FakeSource()
        self.src.items["p1"] = copy.deepcopy(PERSON)
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "other-login"
        self.b.route["server"] = "srv1"

    def test_cast_menu_reads_the_existing_favorite_before_toggling(self):
        # People embedded in a movie's DTO carry no UserData.
        cast = {"Id": "p1", "Name": "Actor One", "Type": "Person"}
        self.b._open_tile_menu(cast, 10, 10)
        self.assertIsNotNone(self.b._menu)
        entries = self.b._tile_menu_entries(cast)
        self.assertEqual([e[2] for e in entries], ["favorite"])
        self.assertEqual(entries[0][0], "Remove from favorites")
        menu_pick(self.b, "favorite")
        self.assertIn(("set_favorite", ("srv1", "p1", False)),
                      self.ctl.transport)

    def test_a_late_person_fetch_cannot_replace_another_menu(self):
        self.b._pool = _DeferredPool()
        self.b._open_tile_menu(dict(PERSON), 10, 10)
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 20, 20)
        self.b._pool.drain()
        self.assertEqual(self.b._menu["item"]["Id"], "m1")

    def test_dismissing_a_pending_menu_keeps_it_closed(self):
        self.b._pool = _DeferredPool()
        self.b._open_tile_menu(dict(PERSON), 10, 10)
        self.b._close_menu()
        self.b._pool.drain()
        self.assertIsNone(self.b._menu)

    def test_failed_person_fetch_does_not_offer_a_guessed_favorite(self):
        def fail(*_a, **_kw):
            raise OSError("offline")

        self.src.get_item = fail
        self.b._open_tile_menu(dict(PERSON), 10, 10)
        self.assertIsNone(self.b._menu)
        self.assertNotIn("set_favorite", [c[0] for c in self.ctl.transport])

    def test_person_page_can_unfavorite_and_favorite_without_a_redraw(self):
        self.b._open_item(dict(PERSON))
        _nodes, handlers = build_scene(self.b)
        click = handlers["person-favorite"]["click"]
        invalidations = []
        self.b.invalidate = lambda: invalidations.append(True)
        for expected in (False, True, False):
            click()
            self.assertEqual(self.ctl.transport[-1],
                             ("set_favorite", ("srv1", "p1", expected)))
        self.assertTrue(invalidations, "favorite changed without a repaint")

    def test_a_refused_favorite_restores_the_person_page(self):
        self.ctl.set_favorite = lambda *_a: False
        self.b._open_item(dict(PERSON))
        _nodes, handlers = build_scene(self.b)
        handlers["person-favorite"]["click"]()
        self.assertTrue(self.b.route["_person"]["UserData"]["IsFavorite"])

    def test_people_row_opens_a_paged_portrait_list(self):
        self.src.favorite_rows = [{"key": "people", "title": "People",
                                   "types": "Person", "items": [dict(PERSON)]}]
        self.b.navigate({"kind": "favorites", "server": "srv1"})
        nodes, handlers = build_scene(self.b)
        self.assertIn("fav-people-p1", ids(nodes))
        handlers["fav-people-more"]["click"]()
        self.assertEqual(self.b.route["list"],
                         {"type": "items", "include_item_types": "Person",
                          "is_favorite": True, "shape": "poster"})
        self.assertNotIn("list-sort", ids(build_scene(self.b)[0]))


class _PeopleApi:
    """10.11's /Persons ignores StartIndex and counts AFTER Limit."""

    def __init__(self):
        self.calls = []

    def items(self, **_kw):
        return {"Items": [], "TotalRecordCount": 0}

    def _get(self, handler, params):
        self.calls.append((handler, dict(params)))
        people = [dict(PERSON, Id="p%d" % i,
                       UserData={"IsFavorite": i % 2 == 0})
                  for i in range(122)]
        if params.get("IsFavorite"):
            people = [p for p in people if p["UserData"]["IsFavorite"]]
        if params.get("Limit"):
            people = people[:params["Limit"]]
        return {"Items": people, "TotalRecordCount": len(people)}


class PeopleFavoriteQueryTest(unittest.TestCase):
    def setUp(self):
        self.api = _PeopleApi()
        self.src = LibrarySource.__new__(LibrarySource)
        self.src._conns = {"srv": type("C", (), {"api": self.api})()}

    def test_favorites_query_the_people_endpoint_with_user_data(self):
        rows = self.src.get_favorite_sections("srv")
        people = next(row for row in rows if row["key"] == "people")
        self.assertEqual(len(people["items"]), 24)
        self.assertTrue(all(p["UserData"]["IsFavorite"]
                            for p in people["items"]))
        handler, params = self.api.calls[0]
        self.assertEqual(handler, "Persons")
        self.assertEqual(params["UserId"], "{UserId}")
        self.assertTrue(params["IsFavorite"])
        self.assertNotIn("Limit", params)
        self.assertNotIn("StartIndex", params)

    def test_drilldown_can_reach_every_favorite_beyond_the_first_row(self):
        spec = {"type": "items", "include_item_types": "Person",
                "is_favorite": True}
        found = []
        for start in (0, 24, 48):
            items, total = self.src.get_list("srv", spec,
                                              start_index=start, limit=24)
            found.extend(p["Id"] for p in items)
            self.assertEqual(total, 61)
        self.assertEqual(found, ["p%d" % i for i in range(0, 122, 2)])


if __name__ == "__main__":
    unittest.main()
