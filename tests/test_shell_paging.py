"""Paging, scrolling and windowing.

Both pagination modes, infinite scroll, and the width/threshold invariants
that keep a long list from re-laying-out on every frame.
"""

import unittest
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    DownloadsController,
    FakeConfig,
    FakeController,
    FakeSource,
    FakeThumbs,
    _SyncPool,
    build_scene,
    grid_scroll,
    music_scroll,
)


class TestRandomSortDoesNotPage(unittest.TestCase):
    """A Random-sorted library must stop after its first page.

    The server reshuffles on every request, so page two is drawn from a
    different ordering: paging it repeats some items and silently skips
    others. _page_more's "an empty in-range page ends the list" rule can
    never fire, because a reshuffle always returns something. Tk capped it
    for exactly this reason.
    """

    def _grid(self, sort_label):
        from jellyfin_mpv_shim.mpvtk_browser.app import SORTS
        idx = [s[0] for s in SORTS].index(sort_label)
        calls = []
        src = FakeSource()

        def get_library_items(srv, parent, **kw):
            calls.append(kw.get("start_index", 0))
            return ([{"Id": "i%d" % (kw.get("start_index", 0) + n)}
                     for n in range(20)], 500)

        src.get_library_items = get_library_items
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "_sort": idx}
        b.nav_stack = [route]
        b._load_route(route)
        return b, route, calls

    def test_random_reports_the_first_page_as_the_whole_list(self):
        b, route, calls = self._grid("Random")
        self.assertEqual(route["_total"], 20,
                         "a Random grid still thinks it has 500 items")
        grid_scroll(b, route, 0, 100)
        self.assertEqual(calls, [0], "Random paged and will duplicate items")

    def test_a_normal_sort_still_pages(self):
        """The cap must not leak into the other nine sorts."""
        b, route, calls = self._grid("Name")
        self.assertEqual(route["_total"], 500)
        grid_scroll(b, route, 0, 100)
        self.assertEqual(calls, [0, 20], "a Name-sorted grid stopped paging")

class TestListWidthsAreStable(unittest.TestCase):
    """A Table's *natural* width is whatever its materialized rows need, so
    inside a Column that doesn't stretch its children a virtualized table
    changed width as you scrolled, and a downloads listing was sized by its
    longest label rather than the pane."""

    def setUp(self):
        self.ctl = DownloadsController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()

    def _row_widths(self, prefix, size=(1280, 720)):
        self.b._size = size
        nodes, _h = layout(self.b.build(size), *size)
        return [n["w"] for n in nodes
                if n["t"] == "rect"
                and str(n.get("id", "")).startswith(prefix)]

    def test_playlist_rows_keep_their_width_while_scrolling(self):
        tracks = [{"Id": "t%d" % i, "Type": "Audio", "IndexNumber": i + 1,
                   "RunTimeTicks": 2000000000,
                   # Long titles far down the list: with an unstretched
                   # container these widened every row once they scrolled in.
                   "Name": ("An extremely long track title here " * 2)
                   if i > 40 else "Sh"} for i in range(400)]
        self.b.navigate({"kind": "playlist", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        self.b.route["_data"] = tracks
        seen = set()
        for off in (0, 3000, 9000):
            self.b._scroll.on_scroll("playlist", off, 100000)
            widths = self._row_widths("pl-")
            self.assertTrue(widths)
            seen.add(round(max(widths)))
        self.assertEqual(len(seen), 1,
                         "row width changed while scrolling: %s" % seen)

    @staticmethod
    def _card_rows(nodes, prefix):
        """Row cards only — not the toggle/Remove buttons living inside
        them, which are legitimately narrow."""
        return [n["w"] for n in nodes
                if n["t"] == "rect"
                and str(n.get("id", "")).startswith(prefix)
                # button rects, not rows: -rmw is "Remove Watched"
                and not str(n["id"]).endswith(("-rm", "-rmw", "-tgl"))]

    def test_download_rows_span_the_pane(self):
        self.b.open_settings("downloads")
        build_scene(self.b)          # first build kicks off the load
        nodes, _h = layout(self.b.build((1280, 720)), 1280, 720)
        widths = self._card_rows(nodes, "dl-g")
        self.assertTrue(widths)
        # Full width less the content padding, not the width of the text.
        self.assertGreater(min(widths), 1280 - 4 * self.b.CONTENT_PAD)
        # Every depth of the tree lines up.
        self.assertEqual(len(set(round(w) for w in widths)), 1)

    def test_queue_rows_span_the_pane(self):
        self.b._open_queue()
        nodes, _h = layout(self.b.build((1280, 720)), 1280, 720)
        widths = self._card_rows(nodes, "q-")
        self.assertTrue(widths)
        self.assertGreater(max(widths), 1280 - 4 * self.b.CONTENT_PAD)

class TestThumbnailRetry(unittest.TestCase):
    """A fetch that fails must not blank the tile permanently.

    The dedup marker was set before dispatch and never cleared, and the
    store dropped failed results without calling back — so one timed-out
    poster stayed a placeholder for the life of the process, through any
    amount of scrolling, re-navigating or reopening.
    """

    def setUp(self):
        self.thumbs = FakeThumbs()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              thumbs=self.thumbs)

    def _ask(self):
        return self.b.tiles._request_image("k1", "http://s/img", (10, 10))

    def test_transient_failure_is_retried_once_the_backoff_passes(self):
        self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 1)

        self.thumbs.resolve("k1", None)          # timeout / 5xx
        self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 1,
                         "must cool off before retrying")

        # ...and once the backoff elapses, it asks again
        attempts, _when = self.b.tiles._img_retry["k1"]
        self.b.tiles._img_retry["k1"] = (attempts, 0.0)
        self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 2, "never retried")

        self.thumbs.resolve("k1", "IMG")
        self.assertEqual(self._ask(), "IMG")
        self.assertEqual(len(self.thumbs.requests), 2)
        self.assertNotIn("k1", self.b.tiles._img_retry)

    def test_a_permanent_miss_is_not_retried(self):
        """The server saying "no such image" is an answer, not a failure
        to retry — otherwise every art-less item re-asks forever."""
        self._ask()
        self.thumbs.gone.add("k1")
        self.thumbs.resolve("k1", None)
        for _ in range(3):
            self.b.tiles._img_retry.pop("k1", None)    # even with no cooldown
            self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 1)

    def test_retries_are_capped(self):
        self._ask()
        for _ in range(self.b.tiles.IMG_MAX_ATTEMPTS + 3):
            key = self.thumbs.requests[-1][0]
            if key in self.thumbs._cbs:
                self.thumbs.resolve(key, None)
            self.b.tiles._img_retry["k1"] = (self.b.tiles._img_retry["k1"][0], 0.0)
            self._ask()
        self.assertLessEqual(len(self.thumbs.requests),
                             self.b.tiles.IMG_MAX_ATTEMPTS + 1,
                             "a dead URL must stop being retried")

    def test_a_successful_image_is_not_refetched(self):
        self._ask()
        self.thumbs.resolve("k1", "IMG")
        for _ in range(3):
            self.assertEqual(self._ask(), "IMG")
        self.assertEqual(len(self.thumbs.requests), 1)

class TestBodyWidth(unittest.TestCase):
    """Content wrapped at "window minus padding" is a scrollbar too wide,
    so line tails run under the scrollbar — and which words land there
    changes with the window size, which read as unstable wrapping."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_body_width_excludes_padding_and_the_scrollbar(self):
        from jellyfin_mpv_shim.mpvtk.layout import SCROLLBAR_W

        w = 1280
        self.assertEqual(
            self.b._body_w(w),
            w - 2 * self.b.CONTENT_PAD - SCROLLBAR_W)

    def test_paragraphs_fit_inside_the_scroll_view(self):
        from jellyfin_mpv_shim.mpvtk.layout import SCROLLBAR_W, layout
        from jellyfin_mpv_shim.mpvtk.widgets import Column, VScroll

        txt = ("An overview long enough to wrap several times so the line "
               "ends can be compared against the container they must fit "
               "inside, at more than one window width.")
        for w in (1280, 1000, 800, 640):
            tree = VScroll(Column([self.b._paragraph(txt, 18,
                                                     self.b._body_w(w))],
                                  pad=self.b.CONTENT_PAD, align="stretch"),
                           flex=1)
            nodes, _h = layout(tree, w, 720)
            scroll = next(n for n in nodes if n["t"] == "scroll")
            bar = SCROLLBAR_W if scroll.get("bar") else 0
            limit = scroll["x"] + scroll["w"] - bar - self.b.CONTENT_PAD
            for n in [x for x in nodes if x["t"] == "text"]:
                self.assertLessEqual(
                    n["x"] + n["w"], limit + 0.5,
                    "text overflows the scroll view at w=%d" % w)

    def test_grid_columns_leave_room_for_the_scrollbar(self):
        geom = self.b.geom
        for w in range(600, 1930, 7):
            cols = self.b.tiles.cols(w, geom)
            used = cols * geom.tile_w + (cols - 1) * geom.gap
            self.assertLessEqual(
                used, self.b._body_w(w),
                "%d columns don't fit at w=%d" % (cols, w))

class TestGridPaging(unittest.TestCase):
    def _grid(self, page_result=None, fail=False):
        src = FakeSource()
        calls = []

        def get_library_items(srv, parent, **kw):
            calls.append(kw.get("start_index", 0))
            if fail:
                raise OSError("boom")
            return page_result if page_result is not None else ([], 100)

        src.get_library_items = get_library_items
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "_items": [{"Id": "m%d" % i} for i in range(20)],
                 "_total": 100}
        b.nav_stack = [route]
        return b, route, calls

    def test_a_failed_page_does_not_deadlock_paging(self):
        b, route, calls = self._grid(fail=True)
        grid_scroll(b, route, 0, 100)
        self.assertFalse(route.get("_loading"),
                         "_loading stuck: the grid can never page again")
        grid_scroll(b, route, 0, 100)
        self.assertEqual(len(calls), 2, "second page attempt never happened")

    def test_an_empty_page_ends_the_list(self):
        b, route, calls = self._grid(page_result=([], 100))
        grid_scroll(b, route, 0, 100)
        self.assertEqual(route["_total"], 20, "total not clamped to loaded")
        grid_scroll(b, route, 0, 100)
        self.assertEqual(len(calls), 1, "re-requested an empty page")

    def test_a_normal_page_appends(self):
        b, route, calls = self._grid(page_result=([{"Id": "x"}], 100))
        grid_scroll(b, route, 0, 100)
        self.assertEqual(len(route["_items"]), 21)
        self.assertEqual(route["_total"], 100)

class TestPagersShareTheirInvariants(unittest.TestCase):
    """The grid, music-tab and genre pagers were three copies of the same
    logic, and each had learned the invariants separately — the genre one had
    never been tested at all, and the music one had no on_error, so a failed
    page left _loading set and that tab could not page again for the rest of
    the session.

    They are one _page_more now, so assert the invariants once per view: what
    used to differ between the copies is exactly what regressions look like.
    """

    def _make(self, view, fail=False, page=None):
        src = FakeSource()
        calls = []

        def fetch(*a, **kw):
            calls.append(kw.get("start_index", 0))
            if fail:
                raise OSError("boom")
            return page if page is not None else ([], 100)

        items = [{"Id": "i%d" % i, "Name": "N%d" % i} for i in range(20)]
        if view == "grid":
            src.get_library_items = fetch
            route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                     "_items": list(items), "_total": 100}
            read = lambda r: r["_items"]           # noqa: E731
        elif view == "music":
            src.get_music_albums = fetch
            route = {"kind": "music", "server": "srv1", "parent_id": "lib1",
                     "_tab": "albums", "_data": list(items), "_total": 100}
            read = lambda r: r["_data"]            # noqa: E731
        else:
            src.get_genre_albums = fetch
            route = {"kind": "music_genre", "server": "srv1",
                     "parent_id": "lib1", "item_id": "g1",
                     "_data": {"albums": list(items), "total": 100}}
            read = lambda r: r["_data"]["albums"]  # noqa: E731

        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        b.nav_stack = [route]
        scroll = {"grid": lambda r, o, m: grid_scroll(b, r, o, m),
                  "music": lambda r, o, m: music_scroll(b, r, o, m),
                  "genre": lambda r, o, m: music_scroll(b, r, o, m)}[view]
        return b, route, calls, scroll, read

    VIEWS = ("grid", "music", "genre")

    def test_a_failed_page_does_not_deadlock_paging(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, _r = self._make(view, fail=True)
                scroll(route, 0, 100)
                self.assertFalse(route.get("_loading"),
                                 "_loading stuck: it can never page again")
                scroll(route, 0, 100)
                self.assertEqual(len(calls), 2,
                                 "second page attempt never happened")

    def test_a_failed_page_tells_the_user(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, _r = self._make(view, fail=True)
                scroll(route, 0, 100)
                self.assertTrue(b.status, "the failure was silent")

    def test_an_empty_page_ends_the_list(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, read = self._make(
                    view, page=([], 100))
                scroll(route, 0, 100)
                scroll(route, 0, 100)
                self.assertEqual(len(calls), 1, "re-requested an empty page")

    def test_a_normal_page_appends(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, read = self._make(
                    view, page=([{"Id": "x", "Name": "X"}], 100))
                scroll(route, 0, 100)
                self.assertEqual(len(read(route)), 21)

    def test_far_from_the_bottom_does_not_page(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, _r = self._make(view)
                scroll(route, 0, 10_000)
                self.assertEqual(calls, [], "paged from the top of the list")

    def test_a_scroll_for_a_route_you_left_is_ignored(self):
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, _r = self._make(view)
                b.nav_stack = [{"kind": "home", "server": "srv1"}]
                scroll(route, 0, 100)
                self.assertEqual(calls, [], "paged a route that is not shown")

    def test_it_never_pages_from_an_empty_list(self):
        """start_index=0 is the initial load, which the route loader owns."""
        for view in self.VIEWS:
            with self.subTest(view=view):
                b, route, calls, scroll, _r = self._make(view)
                if view == "genre":
                    route["_data"] = {"albums": [], "total": 100}
                elif view == "music":
                    route["_data"] = []
                else:
                    route["_items"] = []
                scroll(route, 0, 100)
                self.assertEqual(calls, [], "re-ran the initial load")

class TestGenreResolutionIsNotRacy(unittest.TestCase):
    """_resolve_play_ids read self.route from a pool thread, so a genre
    could resolve against a page the user had already left."""

    def test_the_parent_is_captured_not_read_late(self):
        seen = []
        src = FakeSource()
        src.get_genre_songs = lambda srv, parent, gid: (
            seen.append(parent) or [{"Id": "t1"}])
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b.nav_stack = [{"kind": "music_genre", "server": "srv1",
                        "parent_id": "lib-music", "item_id": "g1"}]
        # resolve with the parent captured at call time, then navigate away
        parent = b.route.get("parent_id")
        b.nav_stack = [{"kind": "home", "server": "srv1"}]
        b._resolve_play_ids({"Id": "g1", "Type": "MusicGenre"}, "srv1", parent)
        self.assertEqual(seen, ["lib-music"],
                         "resolved against the wrong library")

    def test_it_no_longer_touches_the_route(self):
        """Belt and braces on a threading rule a behavioural test can only
        catch by luck. The docstring names self.route, so check the body."""
        import inspect

        src = inspect.getsource(MpvtkBrowser._resolve_play_ids)
        body = src.split('"""')[2] if src.count('"""') >= 2 else src
        self.assertNotIn("self.route", body,
                         "still reads live route state off the loop thread")

class TestScrollRerenderThreshold(unittest.TestCase):
    """Continuous (sub-row) wheel scrolling arrives as many small offset
    steps. The virtualized window must still be rebuilt as the view drifts, or
    rows fall out of the built window and render as blank spacers. The rebuild
    threshold is measured from the last RENDER, not the previous event."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.n = 0
        self.b.invalidate = lambda: setattr(self, "n", self.n + 1)

    def test_threshold_is_distance_since_last_render(self):
        step = self.b.SCROLL_STEP
        # First event always renders (no baseline yet).
        self.b._on_scroll("grid", 80, 100000)
        self.assertEqual(self.n, 1)
        # +80 (< SCROLL_STEP from the render at 80): no rebuild.
        self.b._on_scroll("grid", 160, 100000)
        self.assertEqual(self.n, 1)
        # 160 is now >= SCROLL_STEP from the last render (80): rebuild, and the
        # baseline moves to here.
        self.b._on_scroll("grid", 80 + step + 1, 100000)
        self.assertEqual(self.n, 2)

    def test_slow_subrow_scroll_keeps_rebuilding(self):
        # 80px steps never span SCROLL_STEP between adjacent events, yet a long
        # slow scroll must keep the window fresh -- the old per-event compare
        # rebuilt exactly once and then went stale (the empty-void bug).
        off = 0
        for _ in range(20):            # 1600px of travel
            off += 80
            self.b._on_scroll("grid", off, 100000)
        # Baseline advances to each crossing, so the cadence is ~1 rebuild per
        # (SCROLL_STEP rounded up to a step) -- here ~10. The point is it stays
        # proportional to travel; the old per-event compare rebuilt just once.
        self.assertGreaterEqual(
            self.n, 6,
            "slow sub-row scrolling stopped rebuilding the window")

class TestPagination(unittest.TestCase):
    """The paginate-tile-grids engine (settings.paginated). Exercised at the
    method level -- the render path needs a real geom/source, but the paging,
    clamping, prefetch, cache pruning and bar are pure route-dict logic."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b.invalidate = lambda: None
        # Run async fetches inline so a page is populated by the time the
        # call returns (the real pool would need a join).
        def sync(work, on_done, epoch, on_error=None, always=None):
            try:
                res = work()
            except Exception as e:      # noqa: BLE001
                if on_error:
                    on_error(e)
            else:
                on_done(res)
            finally:
                if always:
                    always()
        # On the AsyncRunner, not the shell's run_async forwarder: Paginator
        # holds the runner directly, so patching the forwarder would leave
        # its fetches on the real pool and the assertions racing them.
        self.b._async.run = sync

    def _fetch_of(self, total):
        """A fetch(start, limit) over a synthetic list of `total` ints."""
        data = list(range(total))
        return lambda start, limit: (data[start:start + limit], total)

    def test_page_count_ceils(self):
        self.assertEqual(self.b._page_count({"_total": 250}, 24), 11)
        self.assertEqual(self.b._page_count({"_total": 48}, 24), 2)
        self.assertEqual(self.b._page_count({"_total": 0}, 24), None)
        self.assertIsNone(self.b._page_count({}, 24))

    def test_ensure_page_loads_current_and_neighbours(self):
        route = {"kind": "grid", "_total": 250, "_page": 2}
        items = self.b._ensure_page(route, 10, self._fetch_of(250))
        self.assertEqual(items, list(range(20, 30)), "page 2 (0-based) items")
        self.assertEqual(route["_npages"], 25)
        # current + both neighbours prefetched, nothing else.
        self.assertEqual(sorted(route["_pages"]), [1, 2, 3])

    def test_ensure_page_clamps_out_of_range(self):
        route = {"kind": "grid", "_total": 30, "_page": 99}
        items = self.b._ensure_page(route, 10, self._fetch_of(30))
        self.assertEqual(route["_page"], 2, "clamped to the last page")
        self.assertEqual(items, list(range(20, 30)))

    def test_ensure_page_prunes_far_pages(self):
        route = {"kind": "grid", "_total": 1000, "_page": 0}
        self.b._ensure_page(route, 10, self._fetch_of(1000))
        route["_page"] = 50
        self.b._ensure_page(route, 10, self._fetch_of(1000))
        # Only a window around the current page is retained.
        self.assertEqual(sorted(route["_pages"]), [49, 50, 51])

    def test_ensure_page_seeds_page0_without_a_fetch(self):
        fetched = []
        def fetch(start, limit):
            fetched.append(start)
            return (list(range(start, start + limit)), 100)
        route = {"kind": "grid", "_total": 100, "_page": 0}
        seed = list(range(100))
        items = self.b._ensure_page(route, 10, fetch, seed=seed)
        self.assertEqual(items, list(range(10)))
        self.assertNotIn(0, fetched, "page 0 came from the seed, not a fetch")

    def test_ps_change_drops_the_cache(self):
        route = {"kind": "grid", "_total": 250, "_page": 2}
        self.b._ensure_page(route, 10, self._fetch_of(250))
        self.assertEqual(route["_page_size"], 10)
        self.b._ensure_page(route, 24, self._fetch_of(250))
        self.assertEqual(route["_page_size"], 24)
        # Rebuilt at the new size: pages hold 24 items, not 10.
        self.assertEqual(len(route["_pages"][2]), 24)

    def test_reset_pagination(self):
        route = {"_pages": {1: []}, "_page_size": 10, "_npages": 5,
                 "_page_loading": {1}, "_page": 4}
        self.b._reset_pagination(route)
        self.assertEqual(route["_page"], 0)
        for k in ("_pages", "_page_size", "_npages", "_page_loading"):
            self.assertNotIn(k, route)

    def test_page_jump_parses_1_based(self):
        route = {"kind": "grid"}
        self.b._page_jump(route, "7")
        self.assertEqual(route["_page"], 6)
        self.b._page_jump(route, "  bogus ")
        self.assertEqual(route["_page"], 6, "an unparseable jump is ignored")
        self.b._page_jump(route, "0")
        self.assertEqual(route["_page"], 0, "page 0 in the box clamps to first")

    def test_bar_hidden_unless_paginated_and_pageable(self):
        route = {"kind": "grid", "_npages": 10, "_page": 0}
        self.b._paginated = lambda: False
        self.assertIsNone(self.b._pagination_bar(route, 1280))
        self.b._paginated = lambda: True
        self.assertIsNone(self.b._pagination_bar({"kind": "detail",
                                                  "_npages": 10}, 1280))
        self.assertIsNone(self.b._pagination_bar({"kind": "grid"}, 1280),
                          "no page count yet -> no bar")
        bar = self.b._pagination_bar(route, 1280)
        self.assertIsNotNone(bar)

class TestPaginatedToggle(unittest.TestCase):
    """The inline Paginated checkbox writes the global setting."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b.invalidate = lambda: None

    def test_toggle_writes_global_setting(self):
        saved = {}

        class Cfg:
            def set_setting(self, k, v):
                saved[k] = v
                return True
        self.b._config = lambda: Cfg()
        self.b._paginated = lambda: False
        self.b._toggle_paginated()
        self.assertEqual(saved, {"paginated": True}, "flips the global flag")


class FakeRenderer:
    """The renderer's scroll bookkeeping, and only that.

    Two behaviours of ``renderer.lua`` matter here and they are the whole
    bug between them: ``set_scroll`` publishes an offset clamped to the
    container that is on screen, and ``reconcile`` DROPS the offset of a
    container that has left the scene, so one that comes back comes back at
    the top.
    """

    def __init__(self):
        self.scroll = {}
        self.on_screen = set()

    def invalidate(self):
        pass

    def scroll_offsets(self):
        return {k: v for k, v in self.scroll.items() if k in self.on_screen}


class _Albums(FakeSource):
    def get_music_albums(self, server_uuid, parent_id, **kw):
        start = kw.get("start_index", 0)
        albums = [{"Id": "al%d" % i, "Name": "Album %d" % i,
                   "Type": "MusicAlbum"} for i in range(300)]
        return albums[start:start + kw.get("limit", 300)], len(albums)


class TestAReturningScrollContainerStartsAtTheTop(unittest.TestCase):
    """A virtualized grid that leaves the scene and comes back rendered
    blank: it was windowed around the offset it had before it left, while
    the real container had been reset to the top.

    Two ways in, both reported from a real session. Ticking Paginated
    replaces the scroller with a fixed page and unticking brings it back;
    changing a sort drops to the busy screen, which takes the scroller with
    it for as long as the reload is in flight.
    """

    def _browser(self, source=None):
        app = FakeRenderer()
        b = MpvtkBrowser(app=app, source=source or FakeSource())
        b._pool = _SyncPool()
        return b, app

    def setUp(self):
        self._handlers = {}

    def _tiles(self, b, prefix, size=(1280, 720)):
        nodes, handlers = layout(b.build(size), *size)
        self._handlers = handlers
        return [n.get("id") for n in nodes
                if (n.get("id") or "").startswith(prefix)]

    def _click(self, node_id):
        """Press a widget in the scene last built by _tiles.

        The Paginated checkbox is pressed rather than ``Paginator.toggle``
        being called directly: which scroll containers a flip tears down is
        the view's knowledge, and calling the paginator straight past it
        would test the argument I passed instead of the wiring.
        """
        self._handlers[node_id]["click"]()

    def _paginated_flag(self, b):
        """Drive the setting through the Paginator without touching the
        user's real config file."""
        flag = {"on": False}
        b._pages.enabled = lambda: flag["on"]
        b._pages._set_enabled = lambda v: flag.__setitem__("on", v)
        return flag

    def test_unticking_paginated_shows_the_music_grid_again(self):
        b, app = self._browser(_Albums())
        self._paginated_flag(b)
        b.navigate({"kind": "music", "server": "srv1", "parent_id": "ml",
                    "title": "Music"})
        app.on_screen = {"music-grid"}
        self.assertTrue(self._tiles(b, "music-"))

        # Scroll deep enough that the old window holds no visible row.
        app.scroll["music-grid"] = 6000
        b._on_scroll("music-grid", 6000, 11000)
        self._tiles(b, "music-")                  # rebuild, keep the handlers

        self._click("music-paginated")            # Paginated on
        app.on_screen = set()                     # the scroller is gone
        self._tiles(b, "music-")

        self._click("music-paginated")            # Paginated off
        app.on_screen = {"music-grid"}
        app.scroll.pop("music-grid", None)        # reconcile dropped it
        # The FIRST row, not merely some row: a stale offset still draws a
        # screenful, just the wrong one, and on a library short enough to
        # run out of rows it draws nothing at all. Both are the same defect
        # and only this pins it.
        self.assertIn(
            "music-0-al0", self._tiles(b, "music-"),
            "the album grid came back windowed at the offset it had before "
            "Paginated was ticked, not at the top where it actually is")

    def test_changing_a_sort_shows_the_library_grid_again(self):
        b, app = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        app.on_screen = {"grid"}
        app.scroll["grid"] = 1500
        b._on_scroll("grid", 1500, 8000)

        b._page_for(b.route)._set("_sort", 1)     # busy screen, then reload
        app.scroll.pop("grid")                    # the scroller left the scene
        self.assertTrue(
            self._tiles(b, "grid-0-"),
            "the library came back blank after a sort change")

    def test_back_navigation_returns_to_where_the_grid_was_left(self):
        """The offsets are parked on the ROUTE, which is what lets them
        survive the reset() that stops one view's offset bleeding into the
        next under the same container id.

        The restore itself is the renderer's (`off0` -> clamped against the
        content in the frame it lands in), so this asserts the offset
        reaches the scene node, not where the container ends up.
        """
        b, app = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        grid_route = b.route
        app.on_screen = {"grid"}
        app.scroll["grid"] = 1500
        b._on_scroll("grid", 1500, 8000)

        b.navigate({"kind": "detail", "server": "srv1", "item_id": "g1"})
        self.assertEqual(grid_route.get("_scroll", {}).get("grid"), 1500,
                         "the offset was not parked on the route we left")
        app.scroll.pop("grid")                # the scroller left the scene

        b.go_back()
        app.on_screen = {"grid"}
        nodes, _h = layout(b.build((1280, 720)), 1280, 720)
        grid = next(n for n in nodes
                    if n.get("id") == "grid" and n.get("t") == "scroll")
        self.assertEqual(grid.get("off0"), 1500,
                         "the grid came back at the top instead of where it "
                         "was left")

    def test_a_first_visit_carries_no_offset(self):
        """Only *returning* restores. A route opened fresh must not inherit
        an offset from anywhere, which is the bug ScrollState.reset() exists
        for -- container ids are per-view, not per-route."""
        b, app = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        app.on_screen = {"grid"}
        app.scroll["grid"] = 1500
        b._on_scroll("grid", 1500, 8000)
        # A *different* library, same container id.
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib2",
                    "title": "Shows"})
        nodes, _h = layout(b.build((1280, 720)), 1280, 720)
        grid = next(n for n in nodes
                    if n.get("id") == "grid" and n.get("t") == "scroll")
        self.assertIsNone(grid.get("off0"))

    def test_the_toggle_forgets_the_offset_without_a_live_snapshot(self):
        """mpv < 0.36 has no ``user-data``, so there is no live snapshot to
        outvote the recorded copy -- the toggle has to drop it itself."""
        b, _app = self._browser(_Albums())
        b.app = None                              # nothing to ask
        self._paginated_flag(b)
        b.navigate({"kind": "music", "server": "srv1", "parent_id": "ml",
                    "title": "Music"})
        b._on_scroll("music-grid", 6000, 11000)
        self._tiles(b, "music-")
        self._click("music-paginated")
        self._tiles(b, "music-")
        self._click("music-paginated")
        self.assertEqual(b._scroll.offset("music-grid"), 0.0)
        self.assertIn("music-0-al0", self._tiles(b, "music-"))


if __name__ == "__main__":
    unittest.main()
