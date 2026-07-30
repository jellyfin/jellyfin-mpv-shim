"""The generic list route -- jellyfin-web's ``#/list?type=…``.

Every row on the home and Live TV screens is a top-N of something with no
way to see the rest. This is the rest, and the chevron on the heading is
the only route to it.
"""

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.pages import PAGES  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.pages.grid import ListPage  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    LibrarySource, OfflineLibrarySource)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene, ids)


class _Api:
    def __init__(self):
        self.calls = []

    def _record(self, name, kwargs):
        self.calls.append((name, kwargs))
        return {"Items": [{"Id": "x", "Name": "X", "Type": "Movie"}],
                "TotalRecordCount": 1}

    def get_next(self, **kw):
        return self._record("get_next", kw)

    def get_user_items(self, **kw):
        return self._record("get_user_items", kw)


def _source(api):
    src = LibrarySource.__new__(LibrarySource)
    src._conns = {"srv": type("C", (), {"api": api})()}
    return src


class SpecDispatchTest(unittest.TestCase):
    """``spec["type"]`` selects the query; the rest of the dict is its
    arguments. A plain dict so it can live in a route and survive
    back-navigation."""

    def test_nextup_goes_to_the_next_up_endpoint(self):
        api = _Api()
        items, total = _source(api).get_list("srv", {"type": "nextup"})
        self.assertEqual(api.calls[0][0], "get_next")
        self.assertEqual((len(items), total), (1, 1))

    def test_nextup_pages_by_start_index(self):
        api = _Api()
        _source(api).get_list("srv", {"type": "nextup"}, start_index=40,
                              limit=20)
        kw = api.calls[0][1]
        self.assertEqual((kw["index"], kw["limit"]), (40, 20))

    def test_a_genre_listing_is_a_recursive_item_query(self):
        """Not a folder's direct children: a genre listing that stopped at
        the top level would be empty on any library with folders in it."""
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "genre_ids": "g1"})
        name, kw = api.calls[0]
        self.assertEqual(name, "get_user_items")
        self.assertEqual(kw["genre_ids"], "g1")
        self.assertTrue(kw["recursive"])

    def test_favorites_are_the_same_query_with_a_predicate(self):
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "is_favorite": True})
        self.assertEqual(api.calls[0][1]["is_favorite"], "true")

    def test_studios_ride_the_params_passthrough(self):
        """get_user_items has no named studio_ids; params is the documented
        way through and merges last."""
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "studio_ids": "s1"})
        self.assertEqual(api.calls[0][1]["params"], {"StudioIds": "s1"})

    def test_the_grids_own_filters_still_apply(self):
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "genre_ids": "g1"},
                              filters={"unplayed": True})
        self.assertEqual(api.calls[0][1]["filters"], "IsUnplayed")

    def test_an_unknown_type_raises_rather_than_looking_empty(self):
        """A typo in a route must not render as an empty library."""
        with self.assertRaises(ValueError):
            _source(_Api()).get_list("srv", {"type": "nonsense"})

    def test_offline_accepts_the_same_call(self):
        """Signature parity is load-bearing: the offline catalog is what a
        failed load falls back TO."""
        import inspect
        live = inspect.signature(LibrarySource.get_list).parameters
        offline = inspect.signature(OfflineLibrarySource.get_list).parameters
        self.assertEqual(set(live), set(offline))

    def test_offline_answers_empty_rather_than_guessing(self):
        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        self.assertEqual(src.get_list("srv", {"type": "nextup"}), ([], 0))


class ListPageTest(unittest.TestCase):
    def _browser(self, spec, title="All"):
        src = FakeSource()
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "list", "server": "srv1", "title": title,
                    "list": spec})
        return b, src

    def test_it_is_registered(self):
        self.assertIs(PAGES["list"], ListPage)

    def test_it_renders_the_items_the_spec_selected(self):
        b, src = self._browser({"type": "items", "genre_ids": "g1"})
        nodes, _h = build_scene(b)
        self.assertTrue(any(str(n.get("id", "")).startswith("grid-")
                            for n in nodes), "no tiles")
        self.assertEqual(src.list_specs[0]["genre_ids"], "g1")

    def test_a_sortable_list_offers_the_sort_control(self):
        b, _src = self._browser({"type": "items", "genre_ids": "g1"})
        nodes, _h = build_scene(b)
        self.assertIn("list-sort", ids(nodes))

    def test_next_up_offers_no_sort(self):
        """It is already in the server's watch order; re-sorting by name
        would be actively worse. Same for guide listings, which are
        chronological -- a programme list out of time order is not a
        listing of anything."""
        b, _src = self._browser({"type": "nextup"})
        nodes, _h = build_scene(b)
        self.assertNotIn("list-sort", ids(nodes))

    def test_the_sort_dropdown_id_is_per_kind(self):
        """It was hard-coded "person-sort" on a method two pages now share.
        The renderer keeps per-node state against the id, so one id across
        two screens is one dropdown across two screens."""
        b, _src = self._browser({"type": "items"})
        nodes, _h = build_scene(b)
        self.assertNotIn("person-sort", ids(nodes))


class SeeAllTest(unittest.TestCase):
    """The chevron. Drawn in every layout: web hides it in its TV layout,
    but that is web having two layouts, not a judgement that a remote
    cannot use one -- and mpvtk makes any clickable node a D-pad target."""

    def _home(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.source.home_rows = [
            {"title": "Next Up", "kind": "nextup", "collection_type": None,
             "slot": 0, "items": [{"Id": "e1", "Name": "Ep",
                                   "Type": "Episode"}]},
            {"title": "Continue Watching", "kind": "resume",
             "collection_type": None, "slot": 1,
             "items": [{"Id": "m1", "Name": "M", "Type": "Movie"}]},
        ]
        b.navigate({"kind": "home", "server": "srv1"})
        return b

    def test_next_up_has_one(self):
        b = self._home()
        nodes, _h = build_scene(b)
        self.assertIn("row-nextup-0-more", ids(nodes))

    def test_continue_watching_does_not(self):
        """Absent on purpose and matching web: a resume row is not the top
        of a longer list, it *is* the list."""
        b = self._home()
        nodes, _h = build_scene(b)
        self.assertNotIn("row-resume-0-more", ids(nodes))

    def test_clicking_it_opens_the_listing(self):
        b = self._home()
        _n, handlers = build_scene(b)
        handlers["row-nextup-0-more"]["click"]()
        self.assertEqual(b.route.get("kind"), "list")
        self.assertEqual(b.route["list"], {"type": "nextup"})
        self.assertEqual(b.route.get("title"), "Next Up")

    def test_the_heading_is_a_d_pad_target(self):
        """mpvtk's nav_candidates collects any node carrying click, so this
        is the whole remote story -- but only if the node really has one."""
        b = self._home()
        nodes, _h = build_scene(b)
        more = [n for n in nodes if n.get("id") == "row-nextup-0-more"]
        self.assertTrue(more and more[0].get("click"))


class LatestAndOnNowSeeAllTest(unittest.TestCase):
    def _home(self, rows):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.source.home_rows = rows
        b.navigate({"kind": "home", "server": "srv1"})
        return b

    LATEST = [{"title": "Latest Movies", "kind": "latestmedia",
               "collection_type": "movies", "slot": 0, "parent_id": "lib9",
               "items": [{"Id": "m1", "Name": "M", "Type": "Movie"}]}]

    def test_a_latest_row_opens_its_library_newest_first(self):
        """Web opens the library on its Latest *tab*; we have no tabs, so
        the equivalent is that library sorted newest-first -- the same
        items in the same order, without the 16-item cap."""
        from jellyfin_mpv_shim.mpvtk_browser.pages.grid import SORTS
        b = self._home(self.LATEST)
        _n, handlers = build_scene(b)
        handlers["row-latestmedia-0-more"]["click"]()
        self.assertEqual(b.route.get("kind"), "grid")
        self.assertEqual(b.route.get("parent_id"), "lib9")
        self.assertEqual(SORTS[b.route["_sort"]][1:], ("DateCreated",
                                                       "Descending"))

    def test_a_latest_row_without_a_library_gets_no_chevron(self):
        """Nothing else in the row identifies which library it came from --
        the title is translated and the collection type is shared."""
        rows = [dict(self.LATEST[0])]
        del rows[0]["parent_id"]
        b = self._home(rows)
        nodes, _h = build_scene(b)
        self.assertNotIn("row-latestmedia-0-more", ids(nodes))

    def test_on_now_opens_the_plain_guide_query(self):
        """Web's chevron is #/list?type=Programs&IsAiring=true -- the guide
        query, not the recommendations endpoint the row itself uses."""
        b = self._home([{"title": "On Now", "kind": "livetv", "slot": 0,
                         "collection_type": "livetv", "parent_id": None,
                         "items": [{"Id": "p1", "Type": "Program"}]}])
        _n, handlers = build_scene(b)
        handlers["row-livetv-0-more"]["click"]()
        self.assertEqual(b.route["list"],
                         {"type": "programs", "filters": {"is_airing": True}})


class ProgramsSeeAllTest(unittest.TestCase):
    """Six rows capped at twelve with nothing behind them -- the sharpest
    missing-destination case in the app."""

    def _programs(self):
        from tests.test_live_tv import browser, open_live_tv
        b = browser()
        b.source.get_program_sections = lambda srv, limit=12: [
            {"key": "movies", "title": "Upcoming Movies",
             "filters": {"has_aired": False, "is_movie": True},
             "items": [{"Id": "a", "Type": "Program"}]},
        ]
        open_live_tv(b, "programs")
        return b

    def test_a_programs_row_links_to_its_own_query(self):
        b = self._programs()
        _n, handlers = build_scene(b)
        self.assertIn("lt-movies-more", handlers)
        handlers["lt-movies-more"]["click"]()
        self.assertEqual(b.route.get("kind"), "list")
        self.assertEqual(b.route["list"],
                         {"type": "programs",
                          "filters": {"has_aired": False, "is_movie": True}})

    def test_a_row_with_no_filters_gets_no_chevron(self):
        """A stand-in in a test, or a future section the source did not
        describe: no chevron rather than a broken one."""
        from tests.test_live_tv import browser, open_live_tv
        b = browser()
        b.source.get_program_sections = lambda srv, limit=12: [
            {"key": "mystery", "title": "Mystery",
             "items": [{"Id": "a", "Type": "Program"}]}]
        open_live_tv(b, "programs")
        nodes, _h = build_scene(b)
        self.assertNotIn("lt-mystery-more", ids(nodes))


if __name__ == "__main__":
    unittest.main()
