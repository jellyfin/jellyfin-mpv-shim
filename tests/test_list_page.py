"""The generic list route -- jellyfin-web's ``#/list?type=…``.

Every row on the home and Live TV screens is a top-N of something with no
way to see the rest. This is the rest, and the chevron on the heading is
the only route to it.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import re
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

    def items(self, handler="", action="GET", params=None, **_kw):
        # GET /Items -- see jellyfin_mpv_shim/items_api. Recorded under
        # the endpoint it actually hits, and with the query the server
        # actually receives.
        return self._record("items", dict(params or {}))


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
        self.assertEqual(name, "items")
        self.assertEqual(kw["GenreIds"], "g1")
        self.assertTrue(kw["Recursive"])

    def test_favorites_are_the_same_query_with_a_predicate(self):
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "is_favorite": True})
        self.assertEqual(api.calls[0][1]["IsFavorite"], "true")

    def test_studios_ride_the_params_passthrough(self):
        """`get_items` has no named studio_ids; params is the documented
        way through and merges last.

        Asserted on the built query rather than on a `params` key, because
        that is where it ends up -- `build_query` merges params in last, so
        a passthrough that stopped being merged would still look right in a
        test that only checked what was handed over.
        """
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "studio_ids": "s1"})
        self.assertEqual(api.calls[0][1]["StudioIds"], "s1")

    def test_the_grids_own_filters_still_apply(self):
        api = _Api()
        _source(api).get_list("srv", {"type": "items", "genre_ids": "g1"},
                              filters={"unplayed": True})
        self.assertEqual(api.calls[0][1]["Filters"], "IsUnplayed")

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
        # Keyed by the library, not by an ordinal: a Latest row's position
        # among its siblings shifts when one of them empties (see
        # HomePage.MULTI_ROW_KEYS), and these ids key the scroll containers.
        handlers["row-latestmedia-lib9-more"]["click"]()
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
        # With no library to key on, the id falls back to the ordinal.
        self.assertIn("row-latestmedia-0", ids(nodes), "test premise")
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


class FavoritesTest(unittest.TestCase):
    """One row per item type, each a top-N with a chevron into the full
    listing. The shapes are jellyfin-web's favoriteitems.js table -- the
    clearest statement of type-to-shape intent in that codebase, and the
    only place it is written down.
    """

    ROWS = [
        {"key": "movies", "title": "Movies", "types": "Movie",
         "items": [{"Id": "m1", "Name": "M", "Type": "Movie"}]},
        {"key": "episodes", "title": "Episodes", "types": "Episode",
         "items": [{"Id": "e1", "Name": "E", "Type": "Episode",
                    "SeriesId": "S1"}]},
        {"key": "albums", "title": "Albums", "types": "MusicAlbum",
         "items": [{"Id": "a1", "Name": "A", "Type": "MusicAlbum"}]},
    ]

    def _browser(self, rows=None):
        src = FakeSource()
        src.favorite_rows = self.ROWS if rows is None else rows
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "favorites", "server": "srv1",
                    "title": "Favorites"})
        return b, src

    def test_it_draws_a_row_per_type(self):
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        got = ids(nodes)
        for key in ("movies", "episodes", "albums"):
            self.assertTrue(any(str(i).startswith("fav-" + key)
                                for i in got), key)

    def test_the_shapes_are_webs_table(self):
        """Movies portrait, episodes backdrop, albums square -- three
        different tile widths in one scene."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            LANDSCAPE_GEOM, POSTER_GEOM, SQUARE_GEOM)
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        # These rows are carousels, so a tile is fav-<key>-<itemid> -- no
        # row index. Two other nodes share the prefix and must not be
        # measured: the chevron (fav-<key>-more, a clickable rect whose
        # width is the heading's) and the scroll container.
        widths = {}
        for n in nodes:
            nid = str(n.get("id", ""))
            m = re.match(r"^fav-([a-z]+)-(?!more$)", nid)
            if m and n.get("t") == "rect":
                widths.setdefault(m.group(1), n["w"])
        self.assertEqual(widths.get("movies"), POSTER_GEOM.tile_w)
        self.assertEqual(widths.get("episodes"), LANDSCAPE_GEOM.tile_w)
        self.assertEqual(widths.get("albums"), SQUARE_GEOM.tile_w)

    def test_favourite_episodes_show_their_own_artwork(self):
        """preferThumb: false in web. A favourites list is a list of the
        things you marked, so a row of one series' episodes all wearing the
        series thumb would defeat the screen."""
        seen = {}
        src = FakeSource()
        src.favorite_rows = self.ROWS
        real = src.image_spec

        def spy(item, image_type="Primary", width=280, inherit=True):
            seen[str(item.get("Id"))] = inherit
            return real(item, image_type, width, inherit=inherit)

        src.image_spec = spy
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "favorites", "server": "srv1", "title": "F"})
        build_scene(b)
        self.assertIs(seen.get("e1"), False)
        self.assertIs(seen.get("m1"), True)

    def test_a_row_links_to_its_own_unbounded_query(self):
        b, _src = self._browser()
        _n, handlers = build_scene(b)
        handlers["fav-movies-more"]["click"]()
        self.assertEqual(b.route["list"],
                         {"type": "items", "is_favorite": True,
                          "include_item_types": "Movie"})

    def test_nothing_favourited_says_so(self):
        b, _src = self._browser(rows=[])
        nodes, _h = build_scene(b)
        self.assertTrue(any("favourite" in str(n.get("text", "")).lower()
                            for n in nodes))

    def test_the_query_is_recursive_and_filtered(self):
        api = _Api()
        rows = _source(api).get_favorite_sections("srv")
        self.assertTrue(rows)
        kw = api.calls[0][1]
        self.assertEqual(kw["IsFavorite"], "true")
        self.assertTrue(kw["Recursive"])
        self.assertIn("IncludeItemTypes", kw)

    def test_offline_has_none(self):
        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        self.assertEqual(src.get_favorite_sections("srv"), [])


class GenresTest(unittest.TestCase):
    """A heading per genre over a random sample, each linking to the full
    listing. Video genres only -- music genres are tiles in the music
    library's own tab and open a page of albums, which is also the split
    jellyfin-web makes."""

    def _browser(self, rows=None, collection_type="movies"):
        src = FakeSource()
        if rows is not None:
            src.genre_rows = rows
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "genres", "server": "srv1", "parent_id": "lib1",
                    "collection_type": collection_type, "title": "Genres"})
        return b, src

    def test_it_draws_a_row_per_genre(self):
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        self.assertTrue(any(str(n.get("id", "")).startswith("genre-g1")
                            for n in nodes))

    def test_a_heading_links_to_the_full_listing(self):
        """The row is a random sample of ten, so this is the only way to
        see the eleventh -- and the only reason random is tolerable."""
        b, _src = self._browser()
        _n, handlers = build_scene(b)
        handlers["genre-g1-more"]["click"]()
        self.assertEqual(b.route.get("kind"), "list")
        self.assertEqual(b.route["list"]["genre_ids"], "g1")
        self.assertEqual(b.route["list"]["parent_id"], "lib1")

    def test_an_empty_library_says_so(self):
        b, _src = self._browser(rows=[])
        nodes, _h = build_scene(b)
        self.assertTrue(any("genres" in str(n.get("text", "")).lower()
                            for n in nodes))

    def test_the_button_is_offered_on_video_libraries(self):
        for ctype in ("movies", "tvshows"):
            with self.subTest(ctype):
                b = MpvtkBrowser(app=None, source=FakeSource(),
                                 controller=FakeController())
                b._pool = _SyncPool()
                b.server = "srv1"
                b.navigate({"kind": "grid", "server": "srv1",
                            "parent_id": "lib1", "collection_type": ctype,
                            "title": "Lib"})
                nodes, _h = build_scene(b)
                self.assertIn("grid-genres", ids(nodes))

    def test_and_not_on_a_music_library(self):
        """Music genres are a tab in the music library, not this screen."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "music", "title": "Lib"})
        nodes, _h = build_scene(b)
        self.assertNotIn("grid-genres", ids(nodes))

    def test_the_button_opens_the_screen(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Lib"})
        _n, handlers = build_scene(b)
        handlers["grid-genres"]["click"]()
        self.assertEqual(b.route.get("kind"), "genres")
        self.assertEqual(b.route.get("parent_id"), "lib1")

    def test_a_genre_tile_opens_a_row_per_kind(self):
        """It used to fall through to a status line. A genre spans films,
        shows and albums, so it opens as ItemsByName rather than one grid
        of everything sorted by name -- the Genres *screen* links straight
        to a single-type listing instead, because there the type is known."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "home", "server": "srv1"})
        b._open_item({"Id": "g7", "Name": "Comedy", "Type": "Genre"})
        self.assertEqual(b.route.get("kind"), "byname")
        self.assertEqual(b.route["list"]["genre_ids"], "g7")

    def test_a_tv_library_lists_shows_not_episodes(self):
        api = _Api()

        class _G(_Api):
            def get_genres(self, parent_id=None, include_item_types=None):
                self.calls.append(("get_genres",
                                   {"include_item_types": include_item_types}))
                return {"Items": [{"Id": "g1", "Name": "Drama"}]}

        g = _G()
        _source(g).get_genre_sections("srv", "lib1", "tvshows")
        self.assertEqual(g.calls[0][1]["include_item_types"], "Series")
        self.assertEqual(g.calls[1][1]["IncludeItemTypes"], "Series")
        del api

    def test_an_unknown_collection_type_has_no_genres_screen(self):
        self.assertEqual(
            _source(_Api()).get_genre_sections("srv", "lib1", "homevideos"),
            [])

    def test_offline_has_none(self):
        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        self.assertEqual(src.get_genre_sections("srv", "l", "movies"), [])


class StudiosTest(unittest.TestCase):
    """Networks: a flat grid of Studio tiles, then that studio's catalogue.

    Web offers it on TV libraries only, which is where studio metadata
    actually lives, and forces backdrop + preferThumb for the tiles."""

    def test_the_spec_queries_the_studios_endpoint(self):
        class _S(_Api):
            def get_studios(self, **kw):
                return self._record("get_studios", kw)

        api = _S()
        items, _total = _source(api).get_list(
            "srv", {"type": "studios", "parent_id": "lib1",
                    "include_item_types": "Series"})
        name, kw = api.calls[0]
        self.assertEqual(name, "get_studios")
        self.assertEqual(kw["include_item_types"], "Series")
        self.assertEqual(kw["parent_id"], "lib1")
        self.assertTrue(items)

    def test_a_named_shape_overrides_the_median(self):
        """Most studios carry no PrimaryImageAspectRatio, so the median
        would fall back to square and draw every network logo in a box."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        src = FakeSource()
        src.list_items = [{"Id": "s1", "Name": "HBO", "Type": "Studio"}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "list", "server": "srv1", "title": "Networks",
                    "list": {"type": "studios", "shape": "landscape"}})
        nodes, _h = build_scene(b)
        tiles = [n for n in nodes
                 if re.match(r"^grid-\d+-", str(n.get("id", "")))
                 and n.get("t") == "rect"]
        self.assertTrue(tiles)
        self.assertEqual(tiles[0]["w"], LANDSCAPE_GEOM.tile_w)

    def test_without_a_named_shape_the_median_still_decides(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        src = FakeSource()
        src.list_items = [{"Id": "m%d" % i, "Name": "M", "Type": "Movie",
                           "PrimaryImageAspectRatio": 2 / 3}
                          for i in range(4)]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "list", "server": "srv1", "title": "L",
                    "list": {"type": "items"}})
        nodes, _h = build_scene(b)
        tiles = [n for n in nodes
                 if re.match(r"^grid-\d+-", str(n.get("id", "")))
                 and n.get("t") == "rect"]
        self.assertEqual(tiles[0]["w"], POSTER_GEOM.tile_w)

    def test_the_button_is_tv_only(self):
        for ctype, want in (("tvshows", True), ("movies", False),
                            ("music", False)):
            with self.subTest(ctype):
                b = MpvtkBrowser(app=None, source=FakeSource(),
                                 controller=FakeController())
                b._pool = _SyncPool()
                b.server = "srv1"
                b.navigate({"kind": "grid", "server": "srv1",
                            "parent_id": "lib1", "collection_type": ctype,
                            "title": "Lib"})
                nodes, _h = build_scene(b)
                self.assertEqual("grid-studios" in ids(nodes), want)

    def test_the_button_opens_the_screen(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "tvshows", "title": "Lib"})
        _n, handlers = build_scene(b)
        handlers["grid-studios"]["click"]()
        self.assertEqual(b.route.get("kind"), "list")
        self.assertEqual(b.route["list"]["type"], "studios")
        self.assertEqual(b.route["list"]["include_item_types"], "Series")

    def test_a_studio_tile_opens_a_row_per_kind(self):
        """It used to fall through to a status line."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "home", "server": "srv1"})
        b._open_item({"Id": "st1", "Name": "HBO", "Type": "Studio"})
        self.assertEqual(b.route.get("kind"), "byname")
        self.assertEqual(b.route["list"]["studio_ids"], "st1")


class ByNameTest(unittest.TestCase):
    """A genre, studio or person spans several kinds of thing, so it opens
    as a row per kind rather than one grid sorted by name."""

    ROWS = [
        {"key": "movies", "title": "Movies", "types": "Movie", "total": 40,
         "items": [{"Id": "m1", "Name": "M", "Type": "Movie"}]},
        {"key": "episodes", "title": "Episodes", "types": "Episode",
         "total": 2,
         "items": [{"Id": "e1", "Name": "E", "Type": "Episode",
                    "SeriesId": "S1"},
                   {"Id": "e2", "Name": "E2", "Type": "Episode",
                    "SeriesId": "S1"}]},
    ]

    def _browser(self, rows=None, spec=None):
        src = FakeSource()
        src.byname_rows = self.ROWS if rows is None else rows
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "byname", "server": "srv1", "title": "Comedy",
                    "list": spec or {"type": "items", "genre_ids": "g1"}})
        return b, src

    def test_it_draws_a_row_per_kind(self):
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        got = ids(nodes)
        self.assertTrue(any(str(i).startswith("byname-movies") for i in got))
        self.assertTrue(any(str(i).startswith("byname-episodes") for i in got))

    def test_the_shapes_are_webs_table(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            LANDSCAPE_GEOM, POSTER_GEOM)
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        widths = {}
        for n in nodes:
            m = re.match(r"^byname-([a-z]+)-(?!more$)", str(n.get("id", "")))
            if m and n.get("t") == "rect":
                widths.setdefault(m.group(1), n["w"])
        self.assertEqual(widths.get("movies"), POSTER_GEOM.tile_w)
        self.assertEqual(widths.get("episodes"), LANDSCAPE_GEOM.tile_w)

    def test_the_predicate_reaches_the_source(self):
        b, src = self._browser(spec={"type": "items", "person_ids": "p9"})
        build_scene(b)
        self.assertEqual(src.byname_specs[0]["person_ids"], "p9")

    def test_a_truncated_row_offers_the_full_listing(self):
        b, _src = self._browser()
        _n, handlers = build_scene(b)
        handlers["byname-movies-more"]["click"]()
        self.assertEqual(b.route.get("kind"), "list")
        self.assertEqual(b.route["list"],
                         {"type": "items", "genre_ids": "g1",
                          "include_item_types": "Movie"})

    def test_a_complete_row_offers_nothing(self):
        """web's rule for its own More button (itemsByName.js:96): a chevron
        onto a page holding the same tiles is a promise the destination
        cannot keep."""
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        self.assertNotIn("byname-episodes-more", ids(nodes))

    def test_episode_rows_do_not_inherit_series_artwork(self):
        """This is a list of the episodes the name is attached to; one
        series' thumb across all of them says nothing about any of them."""
        seen = {}
        src = FakeSource()
        src.byname_rows = self.ROWS
        real = src.image_spec

        def spy(item, image_type="Primary", width=280, inherit=True):
            seen[str(item.get("Id"))] = inherit
            return real(item, image_type, width, inherit=inherit)

        src.image_spec = spy
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "byname", "server": "srv1", "title": "C",
                    "list": {"type": "items", "genre_ids": "g1"}})
        build_scene(b)
        self.assertIs(seen.get("e1"), False)
        self.assertIs(seen.get("m1"), True)

    def test_nothing_attached_says_so(self):
        b, _src = self._browser(rows=[])
        nodes, _h = build_scene(b)
        self.assertTrue(any(n.get("text") for n in nodes))

    def test_the_query_carries_the_row_type_and_the_predicate(self):
        api = _Api()
        rows = _source(api).get_by_name_sections(
            "srv", {"type": "items", "person_ids": "p1"})
        self.assertTrue(rows)
        kw = api.calls[0][1]
        self.assertEqual(kw["PersonIds"], "p1")
        self.assertIn("IncludeItemTypes", kw)
        self.assertTrue(kw["Recursive"])

    def test_offline_has_none(self):
        src = OfflineLibrarySource.__new__(OfflineLibrarySource)
        self.assertEqual(src.get_by_name_sections("srv", {}), [])


class FilterBarOverflowTest(unittest.TestCase):
    """The view controls ride the filter row, and drop to a second row only
    when they genuinely do not fit.

    Measured rather than switched on a width constant, for the same reason
    the top bar is: what fits depends on how many controls this library has
    -- a movies library carries a Collections toggle and a Genres button a
    music one does not -- and the dropdowns are sized to their contents.
    """

    def _bar(self, width, ctype="movies"):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": ctype, "title": "Lib"})
        nodes, _h = build_scene(b, size=(width, 720))
        return {n["id"]: n for n in nodes if n.get("id")}

    def test_the_buttons_are_there_at_every_width(self):
        for w in (1920, 1280, 900, 760):
            with self.subTest(w=w):
                self.assertIn("grid-genres", self._bar(w))

    def test_they_share_the_filter_row_when_there_is_space(self):
        found = self._bar(1920)
        self.assertLess(
            abs(found["grid-genres"]["y"] - found["grid-sort"]["y"]), 20,
            "the view controls took their own row on a wide window")

    def test_they_lead_the_row_rather_than_sitting_among_the_filters(self):
        """A filter changes what this grid shows; these navigate somewhere
        else entirely. Mixing them made Collections (a filter) and Genres
        (a door) look like the same kind of control."""
        found = self._bar(1920)
        self.assertLess(found["grid-genres"]["x"], found["grid-sort"]["x"])

    def test_and_move_ABOVE_it_when_there_is_not(self):
        """Above, not below: a row of doors under the filters reads as more
        filtering.

        The width is *searched for* rather than pinned. This test pinned
        760, and the filter panel -- which took five controls off the bar
        -- moved the split to somewhere below 760 without breaking the
        behaviour at all: the assertion failed while the feature worked.
        A pinned width can only ever go stale in that direction, silently
        if the number is generous.
        """
        def splits(w):
            f = self._bar(w)
            return f["grid-genres"]["y"] < f["grid-sort"]["y"] - 20

        split = next((w for w in range(1920, 400, -40) if splits(w)), None)
        self.assertIsNotNone(
            split, "the doors never moved above the filter row at any width")
        self.assertLess(split, 1280,
                        "the bar split on a window it comfortably fits in")

    def test_a_library_with_no_view_controls_keeps_one_row(self):
        """Nothing to fit, so nothing to split -- and no empty second row."""
        found = self._bar(760, ctype="music")
        self.assertNotIn("grid-genres", found)
        self.assertNotIn("grid-studios", found)


class FavoritesInTheTopBarTest(unittest.TestCase):
    """Home only.

    On a library "favorites" already means something else and better -- the
    Favorites *filter* on that library's own filter row. A top-bar button
    that jumps to a global screen instead is the same word doing two jobs
    one bar apart, and dropping it gives the title the space back.
    """

    def _has_it(self, route):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate(route)
        nodes, _h = build_scene(b)
        return "nav-favorites" in ids(nodes)

    def test_it_is_there_on_home(self):
        self.assertTrue(self._has_it({"kind": "home", "server": "srv1"}))

    def test_and_not_on_a_library(self):
        self.assertFalse(self._has_it(
            {"kind": "grid", "server": "srv1", "parent_id": "lib1",
             "title": "Movies"}))

    def test_the_library_still_has_its_own_favorites_filter(self):
        """Which is the point -- the function is not lost, it is the one
        that was already better.

        It sits in the filter panel now rather than on the bar, which
        does not change the argument: it is still this library's own
        favorites, one click away, next to the rest of the filtering.
        """
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        _nodes, handlers = build_scene(b)
        handlers["grid-filter"]["click"]()
        nodes, _h = build_scene(b)
        self.assertIn("flt-favorite", ids(nodes))


class FilterOrderTest(unittest.TestCase):
    """The order of the movies filter row, which is a design decision and
    therefore the kind of thing that drifts."""

    def _row(self, want):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        b.route["_collection_capable"] = True
        nodes, _h = build_scene(b, size=(1920, 720))
        return [i for _x, i in sorted(
            (n.get("x", 0), n["id"]) for n in nodes
            if n.get("id") in want)]

    def test_collections_sits_with_the_doors_not_the_filters(self):
        """It was argued as a filter -- it swaps the grid for this
        library's collections -- and that reading did not survive.

        **[iw]**: "we should honestly treat collections as a door". A
        filter narrows the set on screen; Collections replaces it with a
        different kind of thing entirely, which is what every other door
        on this row does. So it leads the row with Genres rather than
        trailing the controls that filter.
        """
        want = ("grid-genres", "grid-collections", "grid-sort",
                "grid-filter", "grid-playall", "grid-shuffle")
        self.assertEqual(self._row(want), list(want))

    def test_every_control_on_the_row_shares_one_height(self):
        """One row of buttons, one baseline.

        The rule is action_btn's own docstring -- "every button in an
        action row must come from here" -- and the bar broke it: Play All
        and Shuffle were plain Buttons, which resolve their label from the
        type scale and came out 1.2px taller than the Filter button.

        It was invisible until the panel moved them next to it. Across the
        bar from each other there was nothing to be uneven WITH, which is
        why a height assertion is worth more here than an eyeball: the
        symptom appears when the layout changes, not when the bug is made.
        """
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        b.route["_collection_capable"] = True
        nodes, _h = build_scene(b, size=(1920, 720))
        found = {n["id"]: n for n in nodes if n.get("id")}
        want = ("grid-genres", "grid-collections", "grid-filter",
                "grid-playall", "grid-shuffle")
        heights = {i: found[i]["h"] for i in want if i in found}
        self.assertEqual(len(heights), len(want), heights)
        self.assertEqual(len(set(heights.values())), 1, heights)
        # ...and on one baseline, or equal heights still stagger.
        self.assertEqual(len({found[i]["y"] for i in want}), 1,
                         {i: found[i]["y"] for i in want})

    def test_collections_looks_different_when_it_is_on(self):
        """It is the one toggle left on this row, and it had no visible
        active state at all on the stock theme.

        It was styled with ``theme.chrome_button_style()``, which returns
        an EMPTY dict for any theme that did not ask for accented chrome --
        so "on" and "off" rendered byte-identical and the only clue you
        were in collections view was that the tiles had changed.
        """
        def coll(on):
            b = MpvtkBrowser(app=None, source=FakeSource(),
                             controller=FakeController())
            b._pool = _SyncPool()
            b.server = "srv1"
            b.navigate({"kind": "grid", "server": "srv1",
                        "parent_id": "lib1", "collection_type": "movies",
                        "title": "Movies"})
            b.route["_collection_capable"] = True
            b.route["_collections"] = on
            nodes, _h = build_scene(b, size=(1920, 720))
            return [n for n in nodes if n.get("id") == "grid-collections"][0]

        self.assertNotEqual(coll(True).get("fill"), coll(False).get("fill"),
                            "the collections toggle looks the same on as off")

    def test_play_all_leads_shuffle(self):
        """Paired, and in web's order."""
        want = ("grid-playall", "grid-shuffle")
        self.assertEqual(self._row(want), list(want))

    def test_a_tv_library_has_no_play_all(self):
        """A TV grid is a grid of shows, so "play all" over it means every
        episode of everything in name order. Shuffle stays -- a random
        episode is a perfectly good ask."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib2",
                    "collection_type": "tvshows", "title": "Shows"})
        got = ids(build_scene(b, size=(1920, 720))[0])
        self.assertNotIn("grid-playall", got)
        self.assertIn("grid-shuffle", got)

    def test_a_home_videos_library_keeps_it(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib3",
                    "collection_type": "homevideos", "title": "Home Videos"})
        self.assertIn("grid-playall",
                      ids(build_scene(b, size=(1920, 720))[0]))

    def test_paginated_is_not_on_the_filter_row(self):
        """It moved into the View modal: it is not a filter, and the row was
        carrying a sort, three filters and two play buttons."""
        self.assertEqual(self._row(("grid-paginated",)), [])


class ByNameIconsTest(unittest.TestCase):
    def test_the_view_buttons_have_icons_that_exist(self):
        """A name that is not in the rasterized set draws nothing at all --
        the button keeps the space for an icon and shows a gap. Only 64
        icons are generated, so an invented name fails silently."""
        from jellyfin_mpv_shim.ui_icon_paths import ICON_PATHS

        for name in ("label", "apartment", "chevron_right", "favorite"):
            with self.subTest(name):
                self.assertIn(name, ICON_PATHS)
                self.assertTrue(ICON_PATHS[name].strip())


class ViewImageTypeTest(unittest.TestCase):
    """A library remembers which image type to draw its items with, and an
    explicit choice beats the median outright -- which is the whole point:
    the artwork got it wrong for this library."""

    def _grid(self, ratios=(2 / 3,) * 6, **settings):
        src = FakeSource()
        src.view_settings = {k: (v if isinstance(v, tuple) else (v, None))
                             for k, v in settings.items()}
        src.grid_items = [
            {"Id": "g%d" % i, "Name": "I", "Type": "Movie",
             "PrimaryImageAspectRatio": r} for i, r in enumerate(ratios)]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        return b, src

    def _tile_w(self, b):
        nodes, _h = build_scene(b)
        tiles = [n for n in nodes
                 if re.match(r"^grid-\d+-", str(n.get("id", "")))
                 and n.get("t") == "rect"]
        self.assertTrue(tiles, "no tiles")
        return tiles[0]["w"]

    def test_auto_still_follows_the_artwork(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        b, _src = self._grid()
        self.assertEqual(self._tile_w(b), POSTER_GEOM.tile_w)

    def test_thumb_overrides_the_median(self):
        """The Home Videos case: posters everywhere, and the user wants
        landscape anyway."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        b, _src = self._grid(imageType=("thumb", "items-lib1-Movie-imageType"))
        self.assertEqual(self._tile_w(b), LANDSCAPE_GEOM.tile_w)

    def test_banner_gets_a_real_banner(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import BANNER_GEOM
        b, _src = self._grid(imageType="banner")
        self.assertEqual(self._tile_w(b), BANNER_GEOM.tile_w)

    def test_poster_insists_where_auto_would_not(self):
        """A Home Videos library of landscape clips with a few portrait ones
        has a median that says landscape. Auto follows it; Poster does not."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            LANDSCAPE_GEOM, POSTER_GEOM)
        wide = (16 / 9,) * 5 + (2 / 3,)
        auto, _src = self._grid(ratios=wide)
        self.assertEqual(self._tile_w(auto), LANDSCAPE_GEOM.tile_w)
        forced, _src2 = self._grid(ratios=wide, imageType="poster")
        self.assertEqual(self._tile_w(forced), POSTER_GEOM.tile_w)

    def test_the_modal_shows_what_is_stored(self):
        """The controls moved off the filter row into a modal -- four
        settings read once and rarely touched did not earn permanent space
        beside the sort and the filters."""
        b, _src = self._grid(imageType="thumb")
        page = b._page_for(b.route)
        self.assertEqual(page._view("imageType"), "thumb")
        page._open_view_settings()
        nodes, _h = build_scene(b)
        got = ids(nodes)
        for nid in ("vs-imagetype", "vs-showtitle", "vs-showyear",
                    "vs-listview"):
            self.assertIn(nid, got)

    def test_the_buttons_clear_the_note_above_them(self):
        """The dialog's height came from a width-aware measure and its
        contents from an intrinsic one, so every wrapped note under a
        setting was allotted a single line -- and the button row was laid
        out where that short reckoning put it, with Done sitting on top of
        the paragraph it should have followed."""
        b, _src = self._grid()
        b._page_for(b.route)._open_view_settings()
        nodes, _h = build_scene(b)
        notes = [n for n in nodes if n["t"] == "text"
                 and n.get("y", 0) > 0
                 and "one page of tiles" in str(n.get("text", ""))]
        self.assertTrue(notes, "the pagination note is not on screen")
        # The wrapped continuation lines are their own nodes, and it is the
        # LAST of them the button has to clear.
        note = notes[0]
        lines = [n for n in nodes if n["t"] == "text"
                 and str(n.get("id", "")).startswith(str(note.get("id")))]
        bottom = max(n["y"] + n.get("h", 0) for n in lines)
        done = [n for n in nodes if n.get("id") == "vs-done"][0]
        self.assertGreaterEqual(
            done["y"], bottom,
            "Done overlaps the note by %.0fpx" % (bottom - done["y"]))

    def test_the_modal_also_carries_pagination(self):
        """Not a view setting of this library's -- it is the application's,
        and lives in conf.json rather than on the server -- but it is the
        same question asked once and then left alone, and the filter row is
        not where you go looking for it a second time."""
        b, _src = self._grid()
        b._page_for(b.route)._open_view_settings()
        self.assertIn("vs-paginated", ids(build_scene(b)[0]))

    def test_the_logo_legibility_switch_rides_the_logo_view(self):
        """The LIBRARY half of the pair (#637). It is the one that is off by
        default, so it is the one someone with dark logo artwork has to be
        able to find -- and here is where they are looking, in the library
        they just set to Logo. The Live TV half is Settings-only: Live TV has
        no View menu, and its default is the one nobody goes looking for."""
        b, _src = self._grid(imageType="logo")
        b._page_for(b.route)._open_view_settings()
        self.assertIn("vs-logolegible", ids(build_scene(b)[0]))

    def test_and_is_absent_from_every_other_view(self):
        """A library not drawing logos has nothing for it to act on, and a
        control that does nothing where it sits reads as a broken one."""
        for image_type in ("primary", "poster", "thumb", "banner", "disc"):
            with self.subTest(image_type):
                b, _src = self._grid(imageType=image_type)
                b._page_for(b.route)._open_view_settings()
                self.assertNotIn("vs-logolegible", ids(build_scene(b)[0]))

    def test_the_logo_switch_writes_the_setting_and_repaints(self):
        """The library key, not the Live TV one -- writing the wrong half
        from here would change the guide and leave the grid you are looking
        at alone. And the plate is baked into the composited strip, so
        flipping it without retagging the store changes nothing on screen."""
        b, _src = self._grid(imageType="logo")
        saved, retagged = {}, []

        class Cfg:
            def set_setting(self, k, v):
                saved[k] = v
                return True

            @staticmethod
            def label_for(key):
                return key
        b._config = lambda: Cfg()
        b.apply_logo_legibility = lambda: retagged.append(True)
        b._page_for(b.route)._open_view_settings()
        _n, handlers = build_scene(b)
        handlers["vs-logolegible"]["click"]()
        self.assertEqual(saved, {"logo_legibility_library": True})
        self.assertEqual(retagged, [True])

    def test_the_pagination_switch_is_wired_to_the_paginator(self):
        """It reads the live global rather than a copy, and clicking it goes
        through Paginator.toggle -- which also has to reset the page state
        and forget the grid's scroll offset, neither of which a bare
        settings write would do."""
        b, _src = self._grid()
        page = b._page_for(b.route)
        toggled = []
        page._pages.toggle = lambda route, *ids: toggled.append((route, ids))
        page._open_view_settings()
        _n, handlers = build_scene(b)
        handlers["vs-paginated"]["click"]()
        self.assertEqual(toggled, [(b.route, ("grid",))])

    def test_choosing_one_saves_it_to_the_key_it_was_read_from(self):
        """Writing elsewhere would leave the user's web client still
        reading the old value."""
        b, src = self._grid(imageType=("primary", "items-lib1-Movie-imageType"))
        page = b._page_for(b.route)
        page._set_view("imageType", "banner")
        self.assertEqual(
            src.saved_view_settings,
            [("lib1", "imageType", "banner", "items-lib1-Movie-imageType")])

    def test_the_grid_reshapes_before_the_round_trip(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        b, _src = self._grid()
        b._page_for(b.route)._set_view("imageType", "thumb")
        self.assertEqual(self._tile_w(b), LANDSCAPE_GEOM.tile_w)

    def test_a_rejected_save_rolls_the_shape_back(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        b, src = self._grid()
        src.save_view_fails = True
        b._page_for(b.route)._set_view("imageType", "thumb")
        self.assertEqual(self._tile_w(b), POSTER_GEOM.tile_w)

    def test_going_back_to_auto_measures_afresh(self):
        """The parked median was computed for a different shape."""
        b, _src = self._grid()
        page = b._page_for(b.route)
        page._set_view("imageType", "thumb")
        page._set_view("imageType", "primary")
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        self.assertEqual(self._tile_w(b), POSTER_GEOM.tile_w)

    def test_a_source_without_the_method_still_renders(self):
        """The offline catalog has no view settings."""
        src = FakeSource()
        src.get_view_settings = None   # class attribute; shadow it
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "M"})
        nodes, _h = build_scene(b)
        self.assertTrue(nodes)


class ViewLabelsAndListTest(unittest.TestCase):
    """showTitle, showYear and the grid-or-list choice, all per library and
    all shared with jellyfin-web."""

    def _grid(self, **settings):
        src = FakeSource()
        src.view_settings = {k: (v if isinstance(v, tuple) else (v, None))
                             for k, v in settings.items()}
        src.grid_items = [{"Id": "g1", "Name": "Alpha", "Type": "Movie",
                           "ProductionYear": 1999,
                           "RunTimeTicks": 102 * 600000000,
                           "PrimaryImageAspectRatio": 2 / 3}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        return b, src

    def _scene(self, b):
        nodes, handlers = build_scene(b)
        return nodes, handlers, {n["t"] for n in nodes}

    def test_the_defaults_are_titles_years_and_a_grid(self):
        b, _src = self._grid()
        _n, _h, types = self._scene(b)
        self.assertIn("img", types, "the default view is not a grid")

    def test_list_view_draws_a_table_and_no_artwork(self):
        """Which is also what makes it cheap: no strips, no overlays, so a
        library of thousands costs nothing to draw."""
        b, _src = self._grid(viewType="List")
        nodes, _h, types = self._scene(b)
        self.assertNotIn("img", types)
        texts = {n.get("text") for n in nodes}
        self.assertTrue({"Name", "Year", "Length"} <= texts)

    def test_the_list_view_web_writes_is_the_one_we_read(self):
        """web keeps the list view on the ARTWORK key -- its picker is one
        dropdown of primary/banner/disc/logo/thumb/list
        (viewSettings.template.html) -- and this client used to write it to
        a `viewType` key nothing over there has ever read. A library put in
        list view from a browser came up here as a grid."""
        b, _src = self._grid(imageType="list")
        _n, _h, types = self._scene(b)
        self.assertNotIn("img", types, "web's list view drew as a grid")

    def test_the_key_this_client_used_to_write_still_reads(self):
        """Whatever is already stored has to keep working."""
        b, _src = self._grid(viewType="List")
        _n, _h, types = self._scene(b)
        self.assertNotIn("img", types)

    def test_turning_the_list_on_writes_where_web_looks(self):
        b, src = self._grid()
        b._page_for(b.route)._open_view_settings()
        _n, handlers = build_scene(b)
        handlers["vs-listview"]["click"]()
        self.assertEqual([s[1:3] for s in src.saved_view_settings],
                         [("imageType", "list")])

    def test_leaving_the_list_is_picking_an_artwork_type(self):
        """Which is what it is in web: its dropdown has no 'off'."""
        b, src = self._grid(imageType="list")
        b._page_for(b.route)._open_view_settings()
        _n, handlers = build_scene(b)
        handlers["vs-listview"]["click"]()
        self.assertEqual([s[1:3] for s in src.saved_view_settings],
                         [("imageType", "primary")])
        _n, _h, types = self._scene(b)
        self.assertIn("img", types, "it stayed a table")

    def test_leaving_a_LEGACY_list_retires_the_key_holding_it_there(self):
        """The checkbox writes web's key; ``is_list`` also honours the one
        earlier builds of this client wrote. Writing only the first left a
        library that was already in list view stuck in it -- the legacy key
        went on outvoting the save and the box came back ticked, which reads
        as a dead control."""
        b, src = self._grid(viewType="List")
        b._page_for(b.route)._open_view_settings()
        _n, handlers = build_scene(b)
        handlers["vs-listview"]["click"]()
        self.assertIn(("viewType", "Poster"),
                      [s[1:3] for s in src.saved_view_settings],
                      "the legacy key was left forcing the list")
        b._dialog = None
        _n, _h, types = self._scene(b)
        self.assertIn("img", types, "it stayed a table")

    def test_the_legacy_key_is_retired_by_the_artwork_picker_too(self):
        """Same door, other handle: picking any artwork type is how you leave
        the list in web, so it has to leave this one as well."""
        b, src = self._grid(viewType="List", imageType="primary")
        page = b._page_for(b.route)
        page._set_view("imageType", "thumb")
        self.assertIn(("viewType", "Poster"),
                      [s[1:3] for s in src.saved_view_settings])
        _n, _h, types = self._scene(b)
        self.assertIn("img", types, "it stayed a table")

    def test_retiring_the_legacy_key_survives_a_no_op_artwork_write(self):
        """The box unticks by writing the imageType the library already had.
        That write is a no-op and returns early, so the retire cannot ride
        *behind* it -- nor behind the snapshot _set_view takes of the view,
        which would put the legacy value straight back."""
        b, src = self._grid(viewType="List", imageType="primary")
        page = b._page_for(b.route)
        page._set_view("imageType", "primary")   # the no-op
        self.assertEqual([s[1:3] for s in src.saved_view_settings],
                         [("viewType", "Poster")])
        self.assertEqual(page._view("viewType"), "Poster")
        _n, _h, types = self._scene(b)
        self.assertIn("img", types)

    def test_turning_the_list_ON_does_not_write_the_legacy_key(self):
        """It is a migration, not a setting: nothing writes ``viewType`` any
        more, and reviving it would put the door back."""
        b, src = self._grid()
        b._page_for(b.route)._open_view_settings()
        _n, handlers = build_scene(b)
        handlers["vs-listview"]["click"]()
        self.assertEqual([s[1] for s in src.saved_view_settings],
                         ["imageType"])

    def test_the_artwork_picker_reads_as_auto_while_listing(self):
        """There is no artwork being drawn to point at, and choosing an
        entry is how you leave the list."""
        b, _src = self._grid(imageType="list")
        page = b._page_for(b.route)
        page._open_view_settings()
        nodes, _h = build_scene(b)
        picker = [n for n in nodes if n.get("id") == "vs-imagetype"][0]
        self.assertEqual(picker.get("sel"), 0)

    def test_a_long_list_only_materializes_a_screenful(self):
        """"No overlays" was read as "no need to virtualize", and it is only
        half the cost: a row is still a Row, two margin Spacers and three
        cell Boxes to build, lay out and serialize on the loop thread every
        repaint. Infinite scroll grows the list without bound, and this is
        the view people choose *because* the library is large.

        Asserted on node count rather than on timing so it cannot go flaky
        on a slow machine — the nodes are what the cost is made of.
        """
        b, _src = self._grid(viewType="List")
        # What a user who kept scrolling has: infinite scroll appends into
        # `_items`, so the route holds every page they walked past. Set
        # directly because loading 2000 through the pager would be testing
        # the pager.
        b.route["_items"] = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Movie",
             "ProductionYear": 2000, "RunTimeTicks": 60 * 600000000}
            for i in range(2000)]
        nodes, _h, _t = self._scene(b)
        self.assertLess(len(nodes), 1500,
                        "the whole list was built: %d nodes" % len(nodes))
        # ...and what IS built is the top of the list, where the scroll is.
        texts = {n.get("text") for n in nodes}
        self.assertIn("Item 0", texts)
        self.assertNotIn("Item 1999", texts)

    def test_the_list_shows_the_year_and_runtime(self):
        b, _src = self._grid(viewType="List")
        nodes, _h, _t = self._scene(b)
        texts = {n.get("text") for n in nodes}
        self.assertIn("1999", texts)
        self.assertIn("1h 42m", texts)

    def test_the_table_keeps_its_year_column_whatever_showyear_says(self):
        """`showTitle`/`showYear` are CARD settings -- the checkboxes are
        "Show titles" and "Show years" and what they govern is the caption
        under a tile. People switch them off because their artwork already
        carries the title and year, and a table has no artwork, so the
        reason does not survive the trip.

        This used to consult it, which did not remove the column -- it
        emptied it, leaving a blank column holding its space, which is how
        the reporter met it. `showTitle` was never asked here either, so
        asking about the year was the odd one out as well as wrong."""
        b, _src = self._grid(viewType="List", showYear=False)
        nodes, _h, _t = self._scene(b)
        texts = {n.get("text") for n in nodes}
        self.assertIn("1999", texts, "the year column is empty")
        self.assertIn("Alpha", texts, "...and the name went with it")

    def _tile_h(self, b):
        nodes, _h = build_scene(b)
        hit = [n for n in nodes
               if re.match(r"^grid-\d+-", str(n.get("id", "")))
               and n.get("t") == "rect"]
        self.assertTrue(hit)
        return hit[0]["h"]

    def test_showtitle_off_takes_the_caption_space_with_it(self):
        """The strip reserves caption_h under every tile whether or not
        anything is drawn there, so leaving it would be a band of
        background."""
        self.assertLess(self._tile_h(self._grid(showTitle=False,
                                                showYear=False)[0]),
                        self._tile_h(self._grid()[0]))

    def test_the_year_survives_the_title_being_switched_off(self):
        """#613: turning titles off blanked the year with them, so "years
        only" was unreachable even though both are separate checkboxes."""
        b, _src = self._grid(showTitle=False)      # showYear defaults on
        tile = b.tiles._tile({"Id": "g1", "Name": "Alpha", "Type": "Movie",
                              "ProductionYear": 1999}, b.geom,
                             labels=(False, True))
        self.assertEqual(tile.title, "")
        self.assertEqual(tile.subtitle, "1999")

    def test_a_year_only_caption_is_one_line_tall(self):
        """Between the two-line caption and no caption at all -- the year
        moves up into the title's place rather than sitting a line below
        the artwork with a band of nothing above it."""
        both = self._tile_h(self._grid()[0])
        year_only = self._tile_h(self._grid(showTitle=False)[0])
        neither = self._tile_h(self._grid(showTitle=False,
                                          showYear=False)[0])
        self.assertLess(neither, year_only)
        self.assertLess(year_only, both)

    def test_both_off_blanks_a_line_show_year_does_not_govern(self):
        """An episode's "Show · S1E2" ignores showYear by design, but with
        both switched off the grid reserves NO caption space -- so it has to
        go too, or it draws into the row below."""
        b, _src = self._grid(showTitle=False, showYear=False)
        ep = {"Id": "e1", "Name": "Pilot", "Type": "Episode",
              "SeriesName": "Show", "ParentIndexNumber": 1,
              "IndexNumber": 2}
        self.assertEqual(b.tiles._tile(ep, b.geom, labels=(False, False))
                         .subtitle, "")
        self.assertEqual(b.tiles._tile(ep, b.geom, labels=(False, True))
                         .subtitle, "Show · S1E2")

    def test_showyear_off_still_leaves_a_live_tv_line_alone(self):
        """Every other subtitle branch is a channel, an air time or an
        episode number. Switching years off was not a request to blank
        those."""
        from jellyfin_mpv_shim.mpvtk_browser.components.labels import (
            episode_subtitle)
        ep = {"Type": "Episode", "SeriesName": "Show",
              "ParentIndexNumber": 1, "IndexNumber": 2}
        self.assertEqual(episode_subtitle(ep, show_year=False),
                         episode_subtitle(ep, show_year=True))

    def test_toggling_persists_the_web_spelling_of_a_boolean(self):
        """web compares these as strings, so a JSON boolean reads there as
        false and the setting appears to revert."""
        b, src = self._grid()
        b._page_for(b.route)._set_view("showTitle", False)
        self.assertEqual(src.saved_view_settings[0][1:3],
                         ("showTitle", False))

    def test_the_filter_row_no_longer_carries_them(self):
        """They live in the View modal now; the bar was overcrowded."""
        b, _src = self._grid()
        nodes, _h = build_scene(b)
        got = ids(nodes)
        for nid in ("grid-showtitle", "grid-showyear", "grid-listview",
                    "grid-imagetype"):
            self.assertNotIn(nid, got)
        self.assertIn("grid-viewcfg", got)

    def test_a_rejected_save_rolls_the_setting_back(self):
        b, src = self._grid()
        src.save_view_fails = True
        page = b._page_for(b.route)
        page._set_view("showTitle", False)
        self.assertTrue(page._view("showTitle"))


class PlayAllTest(unittest.TestCase):
    """Play All next to Shuffle.

    Shuffle alone was the whole of "play this library", which on a Home
    Videos folder of holiday clips in date order is the one thing you do
    not want.
    """

    def _page(self, **route):
        src = FakeSource()
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate(dict({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Home Videos"},
                        **route))
        return b, src

    def test_it_queues_what_the_source_returns(self):
        b, _src = self._page()
        b._page_for(b.route)._play_all()
        self.assertEqual(b.controller.played[0][0], ["g0", "g1", "g2"])

    def test_it_asks_in_the_grids_own_order_not_the_loaded_page(self):
        """What is on screen is one screenful of an infinite scroll, so a
        Play All built from it would play the first hundred items."""
        b, src = self._page()
        b.route["_sort"] = 1                      # Date Added, descending
        b.route["_filters"] = {"unplayed": True}
        b._page_for(b.route)._play_all()
        parent, sort_by, sort_order, filters = src.play_all_args
        self.assertEqual(parent, "lib1")
        self.assertEqual((sort_by, sort_order), ("DateCreated", "Descending"))
        self.assertEqual(filters, {"unplayed": True})

    def test_shuffle_is_still_random_rather_than_the_sorted_list(self):
        b, _src = self._page()
        b._page_for(b.route)._shuffle()
        self.assertEqual(b.controller.played[0][0], ["g0", "g5", "g9"])

    def test_an_empty_library_says_so_rather_than_doing_nothing(self):
        b, src = self._page()
        src.get_play_all_ids = lambda *a, **k: []
        b._page_for(b.route)._play_all()
        self.assertFalse(b.controller.played)
        self.assertTrue(b.status)

    def test_both_buttons_start_the_slideshow_rather_than_pausing_on_it(self):
        """These mean "run it". A queue that opens on a photo would sit
        paused on frame one -- clicking a single picture is what asks for a
        viewer."""
        b, _src = self._page()
        page = b._page_for(b.route)
        page._play_all()
        page._shuffle()
        self.assertEqual(b.controller.pause_stills, [False, False])

    def test_clicking_a_photo_still_opens_a_viewer(self):
        """The other half of the same rule, and the reason it is a
        parameter rather than a default change."""
        b, _src = self._page()
        b._play_list(["p1"], "srv1", 0, items=[{"Id": "p1"}])
        self.assertEqual(b.controller.pause_stills, [True])


class TypeIndicatorIconsTest(unittest.TestCase):
    """A Home Videos grid holds folders, albums, photos and clips side by
    side, and with artwork on all four nothing in the tile says which will
    open and which will start playing.

    jellyfin-web's ``getTypeIndicator``: four types, drawn on every card of
    those types wherever it appears."""

    def _tiles(self, types):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser as B

        b = B(app=None, source=FakeSource(), controller=FakeController())
        b._pool = _SyncPool()
        captured = []
        real = b.tiles._tile

        def spy(item, geom, image_type="Primary", parent_item=False,
                inherit=True, labels=None):
            tile = real(item, geom, image_type, parent_item, inherit, labels)
            captured.append(tile)
            return tile

        b.tiles._tile = spy
        items = [{"Id": "i%d" % i, "Name": "N", "Type": t}
                 for i, t in enumerate(types)]
        b.tiles.image_map(items, "x", b.geom)
        return captured

    def test_a_mixed_grid_marks_every_tile(self):
        kinds = [t.kind for t in
                 self._tiles(["Folder", "Photo", "PhotoAlbum", "Video"])]
        self.assertEqual(kinds, ["folder", "photo", "photo_album", "videocam"])

    def test_a_uniform_row_is_marked_too(self):
        """The regression this replaced: the marker was decided per ROW, on
        the reasoning that a row of one kind needs no telling apart. Which
        row a tile lands in is not something a user can see, so it read as
        icons flickering in and out while scrolling one folder."""
        kinds = [t.kind for t in self._tiles(["Video", "Video", "Video"])]
        self.assertEqual(kinds, ["videocam", "videocam", "videocam"])

    def test_types_outside_webs_four_get_nothing(self):
        """A movies or TV library is one kind of question, and web draws no
        indicator there either."""
        kinds = [t.kind for t in
                 self._tiles(["Movie", "Series", "Episode", "MusicAlbum"])]
        self.assertEqual(kinds, ["", "", "", ""])

    def test_every_icon_it_asks_for_is_rasterized(self):
        """A name missing from the generated set draws nothing at all, so an
        invented name fails silently -- which is how `videocam` shipped
        blank the first time."""
        from jellyfin_mpv_shim.mpvtk_browser.components.labels import (
            TYPE_INDICATOR_ICONS)
        from jellyfin_mpv_shim.ui_icon_paths import ICON_PATHS

        for name in set(TYPE_INDICATOR_ICONS.values()):
            with self.subTest(name):
                self.assertIn(name, ICON_PATHS)


if __name__ == "__main__":
    unittest.main()


class CoverSizeAppliesLiveTest(unittest.TestCase):
    """#616: Cover Size sat in Settings behind a restart, so the only way to
    find out what "Large" meant was to set it, restart, and walk back to a
    library. It applies live and is on the View menu."""

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        self._saved = settings.poster_scale
        self.addCleanup(setattr, settings, "poster_scale", self._saved)

    def _browser(self):
        src = FakeSource()
        src.grid_items = [{"Id": "g1", "Name": "Alpha", "Type": "Movie",
                           "ProductionYear": 1999,
                           "PrimaryImageAspectRatio": 2 / 3}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        return b

    def test_changing_it_resizes_the_tiles_without_a_restart(self):
        from jellyfin_mpv_shim.conf import settings
        b = self._browser()
        before = b.geom.tile_w
        settings.poster_scale = 1.7
        b.apply_cover_size()
        self.assertGreater(b.geom.tile_w, before)
        # ...and every shape that follows the setting, not just the poster.
        self.assertGreater(b.geom_square.tile_w, 0)
        self.assertEqual(b.strips.geom, b.geom,
                         "the strip store kept compositing at the old size")

    def test_it_goes_smaller_as_well_as_larger(self):
        """The old range only went up from the default, which is the
        direction fewest people want."""
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg
        values = [v for _l, v in cfg.LABELED_ENUMS["poster_scale"]
                  if v is not None]
        self.assertTrue(any(v < 1.0 for v in values), values)

    def test_the_view_menu_offers_it(self):
        b = self._browser()
        b._page_for(b.route)._open_view_settings()
        self.assertIn("vs-coversize", ids(build_scene(b)[0]))

    def test_scroll_offsets_are_dropped_with_the_row_pitch(self):
        """They are pixel offsets into a list whose rows just changed
        height, so keeping them lands you somewhere you never scrolled to."""
        from jellyfin_mpv_shim.conf import settings
        b = self._browser()
        b._scroll.on_scroll("grid", 900, 5000, lambda o, m: None)
        settings.poster_scale = 0.75
        b.apply_cover_size()
        self.assertFalse(b._scroll.offset("grid"))

    def test_the_grid_on_screen_resizes_too(self):
        """_grid_shape parks the RESOLVED geometry on the route -- on
        purpose, so a grid does not change shape as you page through it --
        which meant the library you were looking at went on drawing at the
        old cover size until it reloaded. Reported as "it works, but I have
        to leave the page and come back"."""
        from jellyfin_mpv_shim.conf import settings
        b = self._browser()
        build_scene(b)                       # parks the shape
        before = b.route["_grid_shape"][0].tile_w
        settings.poster_scale = 1.7
        b.apply_cover_size()
        build_scene(b)                       # re-derives it
        self.assertGreater(
            b.route["_grid_shape"][0].tile_w, before,
            "the grid on screen kept the old cover size")

    def test_it_clears_the_whole_stack_not_just_this_route(self):
        """Or going back lands on a library still parked at the old size."""
        from jellyfin_mpv_shim.conf import settings
        b = self._browser()
        build_scene(b)
        under = b.route
        b.navigate({"kind": "home", "server": "srv1"})
        settings.poster_scale = 0.75
        b.apply_cover_size()
        self.assertNotIn("_grid_shape", under,
                         "a route below the top kept its old geometry")
