"""The generic list route -- jellyfin-web's ``#/list?type=…``.

Every row on the home and Live TV screens is a top-N of something with no
way to see the rest. This is the rest, and the chevron on the heading is
the only route to it.
"""

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
        self.assertEqual(kw["is_favorite"], "true")
        self.assertTrue(kw["recursive"])
        self.assertIn("include_item_types", kw)

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
        self.assertEqual(g.calls[1][1]["include_item_types"], "Series")
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
        self.assertEqual(kw["person_ids"], "p1")
        self.assertIn("include_item_types", kw)
        self.assertTrue(kw["recursive"])

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
        filtering."""
        narrow = self._bar(760)
        self.assertLess(narrow["grid-genres"]["y"],
                        narrow["grid-sort"]["y"] - 20,
                        "the doors did not move above the filter row")

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
        that was already better."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        nodes, _h = build_scene(b)
        self.assertIn("grid-fav", ids(nodes))


class FilterOrderTest(unittest.TestCase):
    """The order of the movies filter row, which is a design decision and
    therefore the kind of thing that drifts."""

    def test_collections_sits_between_favorites_and_paginated(self):
        """It is a filter -- it swaps the grid for this library's
        collections -- so it belongs with the checkboxes, not with the
        buttons that leave. Paginated is not a filter at all and stays
        last."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        b.route["_collection_capable"] = True
        nodes, _h = build_scene(b, size=(1920, 720))
        want = ("grid-genres", "grid-sort", "grid-unplayed", "grid-fav",
                "grid-collections", "grid-paginated", "grid-shuffle")
        got = [i for _x, i in sorted(
            (n.get("x", 0), n["id"]) for n in nodes
            if n.get("id") in want)]
        self.assertEqual(got, list(want))


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

    def test_the_list_shows_the_year_and_runtime(self):
        b, _src = self._grid(viewType="List")
        nodes, _h, _t = self._scene(b)
        texts = {n.get("text") for n in nodes}
        self.assertIn("1999", texts)
        self.assertIn("1h 42m", texts)

    def test_showyear_off_empties_the_year_column(self):
        b, _src = self._grid(viewType="List", showYear=False)
        nodes, _h, _t = self._scene(b)
        self.assertNotIn("1999", {n.get("text") for n in nodes})

    def test_showtitle_off_takes_the_caption_space_with_it(self):
        """The strip reserves caption_h under every tile whether or not
        anything is drawn there, so leaving it would be a band of
        background."""
        b_on, _s = self._grid()
        b_off, _s2 = self._grid(showTitle=False)

        def tile_h(b):
            nodes, _h = build_scene(b)
            hit = [n for n in nodes
                   if re.match(r"^grid-\d+-", str(n.get("id", "")))
                   and n.get("t") == "rect"]
            self.assertTrue(hit)
            return hit[0]["h"]

        self.assertLess(tile_h(b_off), tile_h(b_on))

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


class MixedKindChipsTest(unittest.TestCase):
    """A Home Videos grid holds folders, albums, photos and clips side by
    side, and with artwork on all four nothing in the tile says which will
    open and which will start playing."""

    def _tiles(self, types):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser as B

        b = B(app=None, source=FakeSource(), controller=FakeController())
        b._pool = _SyncPool()
        captured = []
        real = b.tiles._tile

        def spy(item, geom, image_type="Primary", parent_item=False,
                inherit=True, labels=None, kind_chips=False):
            tile = real(item, geom, image_type, parent_item, inherit, labels,
                        kind_chips)
            captured.append(tile)
            return tile

        b.tiles._tile = spy
        items = [{"Id": "i%d" % i, "Name": "N", "Type": t}
                 for i, t in enumerate(types)]
        b.tiles.image_map(items, "x", b.geom)
        return captured

    def test_a_mixed_grid_chips_every_tile(self):
        kinds = [t.kind for t in self._tiles(["Folder", "Photo", "Video"])]
        self.assertEqual(kinds, ["▸", "▣", "▶"])

    def test_a_grid_of_one_kind_chips_nothing(self):
        """A movies library is all one kind, so a chip on every tile would
        be noise. Decided per row, not per tile."""
        kinds = [t.kind for t in self._tiles(["Video", "Video", "Video"])]
        self.assertEqual(kinds, ["", "", ""])

    def test_types_with_no_chip_do_not_force_one_on_the_others(self):
        """A movie next to a series is still one kind of question."""
        kinds = [t.kind for t in self._tiles(["Movie", "Series"])]
        self.assertEqual(kinds, ["", ""])

    def test_a_movie_among_folders_gets_no_chip_but_the_folders_do(self):
        kinds = [t.kind for t in self._tiles(["Folder", "Video", "Movie"])]
        self.assertEqual(kinds, ["▸", "▶", ""])


if __name__ == "__main__":
    unittest.main()
