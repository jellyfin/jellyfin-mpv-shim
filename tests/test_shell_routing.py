"""Route stack, epoch-guarded async, and build() -> scene.

The shell's own behaviour: what a navigation does, what a stale async result
must not do, and that a failing route surfaces instead of freezing.
"""

import unittest
import threading
import time
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeConfig,
    FakeController,
    FakeSource,
    _DeferredPool,
    _FailingSource,
    _SyncPool,
    build_scene,
    ids,
    music_page,
    types,
)


class TestRouting(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_initial_route_is_home(self):
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_navigate_pushes_and_back_pops(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.assertEqual(self.b.route["kind"], "grid")
        self.assertEqual(len(self.b.nav_stack), 2)
        self.b.go_back()
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_back_stops_at_root(self):
        self.b.go_back()
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_navigate_reset_clears_stack(self):
        self.b.navigate({"kind": "grid", "parent_id": "lib1"})
        self.b.navigate({"kind": "home", "server": "srv1"}, reset=True)
        self.assertEqual(len(self.b.nav_stack), 1)
        self.assertEqual(self.b.route["kind"], "home")

    # -- the forward stack (the mouse's forward button) -------------------
    #
    # Mouse-only by design, so these drive the browser methods directly:
    # there is no forward arrow in the chrome to click, and the button
    # itself is renderer-side (tests/lua/test_renderer.lua).

    def _grid(self, ident, title):
        return {"kind": "grid", "server": "srv1", "parent_id": ident,
                "title": title, "_data": []}

    def test_back_then_forward_returns_to_the_page(self):
        self.b.navigate(self._grid("lib1", "Movies"))
        self.b.go_back()
        self.b.go_forward()
        self.assertEqual(self.b.route["parent_id"], "lib1")
        self.assertEqual(len(self.b.nav_stack), 2)

    def test_forward_at_the_end_does_nothing(self):
        self.b.navigate(self._grid("lib1", "Movies"))
        depth = len(self.b.nav_stack)
        self.b.go_forward()
        self.assertEqual(len(self.b.nav_stack), depth)

    def test_navigating_somewhere_new_discards_the_forward_stack(self):
        """Browser semantics: branching off the history abandons it. Without
        this, Back-then-a-tile leaves a forward press jumping to a page that
        is no longer anywhere in the history you can see."""
        self.b.navigate(self._grid("lib1", "Movies"))
        self.b.go_back()
        self.b.navigate(self._grid("lib2", "Shows"))
        self.b.go_forward()
        self.assertEqual(self.b.route["parent_id"], "lib2")

    def test_forward_survives_going_back_more_than_one_page(self):
        self.b.navigate(self._grid("lib1", "Movies"))
        self.b.navigate(self._grid("lib2", "Shows"))
        self.b.go_back()
        self.b.go_back()
        self.b.go_forward()
        self.b.go_forward()
        self.assertEqual(self.b.route["parent_id"], "lib2")

    def test_a_page_left_before_it_loaded_is_reloaded_on_the_way_forward(self):
        """Back bumps the epoch, so an in-flight fetch is dropped. Nothing
        else re-issues it: the route would come back with no data and no
        error, which the render path draws as a spinner forever."""
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies",
                         "_loading": True})
        self.b.go_back()
        loaded = []
        self.b._load_route = lambda route, epoch=None: loaded.append(route)
        self.b.go_forward()
        self.assertEqual([r["parent_id"] for r in loaded], ["lib1"])
        self.assertNotIn("_loading", self.b.route,
                         "the paging guard would cap the list for good")

    def test_a_loaded_page_is_not_refetched_on_the_way_forward(self):
        self.b.navigate(self._grid("lib1", "Movies"))
        self.b.go_back()
        loaded = []
        self.b._load_route = lambda route, epoch=None: loaded.append(route)
        self.b.go_forward()
        self.assertEqual(loaded, [])

    def test_a_deleted_playlist_is_pruned_from_the_forward_stack_too(self):
        """The same dead-page bug after_playlist_deleted exists to prevent,
        on the other stack: the page is gone from the history behind you and
        still one forward press away."""
        self.b.navigate({"kind": "playlist", "server": "srv1",
                         "item_id": "PL9", "title": "PL", "_data": []})
        self.b.go_back()
        self.b.after_playlist_deleted("PL9")
        self.b.go_forward()
        self.assertNotEqual(self.b.route.get("item_id"), "PL9")

    def test_the_history_menu_jumps_backward_and_forward(self):
        from jellyfin_mpv_shim.mpvtk_browser import window_chrome

        for i in range(1, 4):
            self.b.navigate(self._grid("lib%d" % i, "L%d" % i))
        self.b.go_back()                      # now on lib2, lib3 ahead
        rows = window_chrome.history_entries(self.b)
        self.assertEqual([r[0] for r in rows],
                         ["Home", "L1", "L2", "L3"])
        self.assertEqual([r[1] for r in rows],
                         ["chevron_left", "chevron_left", "check",
                          "chevron_right"])
        self.b.go_back_to(1)                  # the root
        self.assertEqual(self.b.route["kind"], "home")
        self.b.go_forward_to(4)               # all the way back out
        self.assertEqual(self.b.route["parent_id"], "lib3")

    def test_the_history_menu_keeps_the_current_page_when_it_is_trimmed(self):
        from jellyfin_mpv_shim.mpvtk_browser import window_chrome

        for i in range(30):
            self.b.navigate(self._grid("lib%d" % i, "L%d" % i))
        rows = window_chrome.history_entries(self.b)
        self.assertEqual(len(rows), window_chrome.HISTORY_MAX)
        self.assertIn("check", [r[1] for r in rows],
                      "a deep stack trimmed the page you are on")
        self.assertEqual(rows[-1][0], "L29")

    def test_epoch_bumps_on_navigation(self):
        e0 = self.b._epoch
        self.b.navigate({"kind": "grid", "parent_id": "lib1"})
        self.assertGreater(self.b._epoch, e0)

    def test_after_playlist_deleted_prunes(self):
        self.b.navigate({"kind": "grid", "parent_id": "PL9", "title": "PL"})
        self.b.after_playlist_deleted("PL9")
        self.assertTrue(all(r.get("parent_id") != "PL9"
                            for r in self.b.nav_stack))
        self.assertEqual(self.b.route["kind"], "home")

    def test_open_folder_item_navigates_to_grid(self):
        self.b._open_item({"Id": "lib1", "Name": "Movies",
                           "Type": "CollectionFolder",
                           "CollectionType": "movies"})
        self.assertEqual(self.b.route["kind"], "grid")
        self.assertEqual(self.b.route["parent_id"], "lib1")

class TestAsyncStaleness(unittest.TestCase):
    def test_superseded_result_is_dropped(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        applied = []
        gate = threading.Event()
        released = threading.Event()

        def work():
            gate.set()
            released.wait(2.0)
            return "value"

        b.run_async(work, lambda r: applied.append(r), epoch=b._epoch)
        self.assertTrue(gate.wait(2.0))     # worker is running
        b._bump_epoch()                      # user navigated away meanwhile
        released.set()
        b._pool.shutdown(wait=True)
        self.assertEqual(applied, [], "stale result must not be applied")

    def test_current_result_is_applied(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        applied = []
        b.run_async(lambda: "value", lambda r: applied.append(r),
                    epoch=b._epoch)
        b._pool.shutdown(wait=True)
        self.assertEqual(applied, ["value"])

class TestNavigationSurvivesAConcurrentBump(unittest.TestCase):
    """A navigation is `_bump_epoch()` then `_load_route()`, and those two
    statements are not atomic. Foreign threads do bump in between — the
    connect thread and the clientManager health check via `set_source`,
    `display_item`'s handler, `_work_offline`, `_do_unlock`.

    A review read that as a race that strands the route on a spinner, and
    the obvious fix is to thread the navigation's epoch down into the
    loader instead of letting it re-read `self._epoch`. **That fix is
    wrong and these tests exist to stop it being applied again.**

    Re-reading yields the NEWEST epoch, so a loader can never capture one
    that is already superseded. Threading the value down is what creates
    the stranding: an interloping bump makes the captured epoch stale,
    `run_async` drops the `on_done`, and since no `_error` is set the view
    spins forever with no retry. These tests fail against that version.
    """

    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=None)
        b._pool = _SyncPool()
        b.server = "srv1"
        return b

    def test_a_route_still_loads_when_something_bumps_mid_navigation(self):
        b = self._browser()

        # Bump from "another thread" in the window between the navigation's
        # bump and the loader reading the epoch. Re-reading absorbs this;
        # a threaded-down epoch would be stale and the load discarded.
        real_load = b._load_route

        def racing_load(route, epoch=None):
            b._bump_epoch()          # the interloper
            return real_load(route, epoch)

        b._load_route = racing_load
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})

        self.assertIsNotNone(
            b.route.get("_items"),
            "the page never loaded — a concurrent bump stranded it")

    def test_the_user_sees_content_not_a_spinner(self):
        """The consequence, asserted on the scene rather than route state."""
        b = self._browser()
        real_load = b._load_route

        def racing_load(route, epoch=None):
            b._bump_epoch()
            return real_load(route, epoch)

        b._load_route = racing_load
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        nodes, _h = build_scene(b)
        self.assertNotIn("busy", types(nodes),
                         "the view is still spinning after its data landed")

    def test_a_genuinely_stale_result_is_still_dropped(self):
        """The guard must keep working — this is not a licence to apply
        everything."""
        b = self._browser()
        applied = []
        ep = b._bump_epoch()
        b._bump_epoch()                      # navigation moved on again
        b.run_async(lambda: "value", lambda r: applied.append(r), epoch=ep)
        self.assertEqual(applied, [])

    def test_bump_epoch_reports_the_value_it_installed(self):
        b = self._browser()
        got = b._bump_epoch()
        self.assertEqual(got, b._epoch)

class TestBuild(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_home_loading_shows_spinner(self):
        # No data yet on the fresh route -> Busy spinner.
        self.b.route.pop("_data", None)
        nodes, _h = build_scene(self.b)
        self.assertIn("busy", types(nodes))

    def test_home_with_data_renders_strip_rows(self):
        src = self.b.source
        self.b.route["_data"] = {
            "libraries": src.libraries, "rows": src.home_rows}
        nodes, handlers = build_scene(self.b)
        self.assertIn("img", types(nodes))          # ImageMap strip present
        # tile hit-regions are registered as click handlers
        self.assertTrue(any(k.startswith("row-libs-") for k in handlers))

    def test_grid_with_data_renders(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.b.route["_items"] = self.b.source.grid_items
        self.b.route["_total"] = len(self.b.source.grid_items)
        nodes, _h = build_scene(self.b)
        self.assertIn("img", types(nodes))

    def test_chrome_present_on_grid(self):
        self.b.navigate({"kind": "grid", "parent_id": "lib1",
                         "title": "Movies"})
        self.b.route["_items"] = []
        self.b.route["_total"] = 0
        nodes, _h = build_scene(self.b)
        self.assertIn("nav-home", ids(nodes))
        self.assertIn("nav-back", ids(nodes))   # depth > 1

    def test_chrome_absent_on_chrome_free_route(self):
        """Uses a route that exists. It used to use "connecting", which
        nothing navigates to and which no renderer serves — so this asserted
        the chrome rule against a screen no user can reach."""
        for kind in ("login", "locked"):
            with self.subTest(kind=kind):
                b = MpvtkBrowser(app=None, source=FakeSource())
                b.nav_stack.append({"kind": kind})
                self.assertNotIn("nav-home", ids(build_scene(b)[0]))

    def test_chrome_is_present_on_an_ordinary_route(self):
        self.b.nav_stack.append({"kind": "grid", "server": "srv1",
                                 "parent_id": "lib1"})
        self.assertIn("nav-home", ids(build_scene(self.b)[0]))

class TestHomeButtonKeepsTheLoadedScreen(unittest.TestCase):
    """Pressing Home lands on the rows that are already loaded.

    The route dict is the page cache -- ``_data`` and the Page object both
    hang off it -- so pushing a fresh one meant a full home fetch behind
    ``chrome.busy()`` every time, on the screen the user is likeliest to
    already have. The tests below are written against a pool that completes
    nothing, because with the inline pool the refetch lands before anything
    is drawn and a spinner could never be observed either way.
    """

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()
        # Land on a loaded home, the way a session starts.
        self.b.navigate({"kind": "home", "server": self.b.server}, reset=True)
        self.home = self.b.route
        self.assertIsNotNone(self.home.get("_data"))
        # From here on nothing completes, so what is on screen is what was
        # cached.
        self.pool = self.b._pool = _DeferredPool()

    def _leave_home(self):
        self.b.navigate({"kind": "grid", "server": self.b.server,
                         "parent_id": "lib1", "title": "Movies"})
        self.pool.queued.clear()

    def test_pressing_home_paints_the_cached_rows_not_a_spinner(self):
        self._leave_home()
        self.assertTrue(self.b.on_nav_command("home"))
        nodes, _h = build_scene(self.b)
        self.assertNotIn("busy", types(nodes))
        self.assertIn("row-libs", ids(nodes))

    def test_the_cached_route_is_the_one_that_comes_back(self):
        """Same dict, so the Page instance and everything it keeps on itself
        comes back with the data rather than being rebuilt."""
        self._leave_home()
        self.b.on_nav_command("home")
        self.assertIs(self.b.route, self.home)
        self.assertEqual(self.b.nav_stack, [self.home])

    def test_it_still_re_reads_behind_the_cached_rows(self):
        """Stale-while-revalidate, not a cache that goes cold: what makes
        reuse safe is that Home refreshes in place, so the load must still be
        dispatched."""
        self._leave_home()
        self.b.on_nav_command("home")
        self.assertEqual(len(self.pool.queued), 1)
        # ...and the rows are on screen while it is in flight. Named, not
        # just "not busy": every other page in the app is also not busy.
        nodes, _h = build_scene(self.b)
        self.assertNotIn("busy", types(nodes))
        self.assertIn("row-libs", ids(nodes))

    def test_the_top_bar_button_goes_the_same_way(self):
        """Two doors to the same screen; the one people actually click is the
        one that must not have been left behind."""
        self._leave_home()
        _nodes, handlers = build_scene(self.b)
        handlers["nav-home"]["click"]()
        self.assertIs(self.b.route, self.home)

    def test_another_server_home_is_not_reused(self):
        """Switching servers pushes its own home. Landing on the previous
        server's rows would look like a load and be someone else's library."""
        self._leave_home()
        self.b.server = "srv2"
        self.b.on_nav_command("home")
        self.assertIsNot(self.b.route, self.home)
        self.assertIsNone(self.b.route.get("_data"))

    def test_a_home_with_nothing_cached_still_spins(self):
        """Reuse is not a claim that the screen is ready. An empty home comes
        back as the empty home it was -- pushing a second one would differ
        only in throwing away the Page object."""
        self._leave_home()
        self.home.pop("_data")
        self.b.on_nav_command("home")
        self.assertIs(self.b.route, self.home)
        self.assertIn("busy", types(build_scene(self.b)[0]))


class TestPhase1Views(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()

    def _load_and_render(self, route):
        self.b.navigate(route)   # sync pool -> loader already applied
        return build_scene(self.b)

    def test_open_series_navigates(self):
        self.b._open_item({"Id": "sh1", "Name": "Show", "Type": "Series"})
        self.assertEqual(self.b.route["kind"], "series")

    def test_open_season_navigates(self):
        self.b._open_item({"Id": "se1", "Name": "Season 1", "Type": "Season",
                           "SeriesId": "sh1"})
        self.assertEqual(self.b.route["kind"], "season")
        self.assertEqual(self.b.route["series_id"], "sh1")

    def test_detail_renders_backdrop_title_and_play(self):
        nodes, _h = self._load_and_render(
            {"kind": "detail", "server": "srv1", "item_id": "m1",
             "title": "Alpha"})
        self.assertIn("detail-bd", ids(nodes))     # backdrop placeholder/image
        self.assertIn("btn-play", ids(nodes))
        self.assertIn("btn-resume", ids(nodes))    # resume offset in FakeSource

    def test_series_renders_seasons_row(self):
        nodes, handlers = self._load_and_render(
            {"kind": "series", "server": "srv1", "item_id": "sh1",
             "title": "Show"})
        self.assertTrue(any(k.startswith("series-seasons-") for k in handlers))

    def test_season_renders_episodes_and_switcher(self):
        nodes, handlers = self._load_and_render(
            {"kind": "season", "server": "srv1", "item_id": "se1",
             "series_id": "sh1", "title": "Season 1"})
        self.assertIn("season-switch", ids(nodes))   # 2 seasons -> dropdown
        self.assertTrue(any(k.startswith("ep-") for k in handlers))

    def test_search_from_chrome_navigates_and_renders(self):
        self.b._search("matrix")
        self.assertEqual(self.b.route["kind"], "search")
        self.assertEqual(self.b.route["term"], "matrix")
        nodes, handlers = build_scene(self.b)
        self.assertTrue(any(k.startswith("search-") for k in handlers))

    def test_empty_search_is_ignored(self):
        before = len(self.b.nav_stack)
        self.b._search("   ")
        self.assertEqual(len(self.b.nav_stack), before)

    def test_search_groups_by_type(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b._search("x")
        nodes, h = build_scene(b)
        # movie/episode/album grouped rows + a songs track list
        self.assertTrue(any(k.startswith("search-Movies-") for k in h))
        self.assertTrue(any(k.startswith("search-Episodes-") for k in h))
        self.assertTrue(any(k.startswith("search-song-") for k in h))

    def test_offline_banner_configure_servers(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.set_offline(True)
        nodes, h = build_scene(b)
        self.assertIn("banner-servers", ids(nodes))
        h["banner-servers"]["click"]()
        self.assertEqual(b.route["kind"], "login")

class TestPhase2Views(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _load_and_render(self, route):
        self.b.navigate(route)   # sync pool -> loader already applied
        return build_scene(self.b)

    def test_open_music_library(self):
        self.b._open_item({"Id": "musiclib", "Name": "Music",
                           "Type": "CollectionFolder", "CollectionType": "music"})
        self.assertEqual(self.b.route["kind"], "music")

    def test_open_album_and_song_types(self):
        self.b._open_item({"Id": "al1", "Name": "A", "Type": "MusicAlbum"})
        self.assertEqual(self.b.route["kind"], "album")

    def test_click_song_plays_immediately(self):
        self.b._open_item({"Id": "sng", "Name": "S", "Type": "Audio"})
        # Audio: play but stay in browse (now-playing bar), don't yield.
        self.assertTrue(self.b._browsing)
        self.assertIsNotNone(self.b._now_playing)
        self.assertEqual(self.ctl.played, [(["sng"], "srv1", 0)])

    def test_music_tabs_render_and_switch(self):
        nodes, _h = self._load_and_render(
            {"kind": "music", "server": "srv1", "parent_id": "musiclib",
             "title": "Music"})
        for tab in ("mtab-albums", "mtab-artists", "mtab-genres"):
            self.assertIn(tab, ids(nodes))
        # switch to artists -> reload -> renders
        music_page(self.b)._set_tab("artists")
        self.assertEqual(self.b.route["_tab"], "artists")

    def test_album_tracklist_and_play(self):
        nodes, handlers = self._load_and_render(
            {"kind": "album", "server": "srv1", "item_id": "al1",
             "title": "Album"})
        self.assertIn("album-play", ids(nodes))
        self.assertTrue(any(k.startswith("trk-") for k in handlers))
        # clicking a track plays the whole album from that index
        handlers[next(k for k in handlers if k == "trk-2")]["click"]()
        self.assertTrue(self.ctl.played)
        ids_, srv, start = self.ctl.played[-1]
        self.assertEqual(start, 2)
        self.assertEqual(len(ids_), 6)

    def test_playlist_play_all(self):
        nodes, _h = self._load_and_render(
            {"kind": "playlist", "server": "srv1", "item_id": "pl1",
             "title": "My List"})
        self.assertIn("pl-play", ids(nodes))

    def test_artist_and_genre_render(self):
        nodes, h = self._load_and_render(
            {"kind": "artist", "server": "srv1", "item_id": "ar1",
             "title": "Artist"})
        self.assertTrue(any(k.startswith("artist-") for k in h))
        nodes, h = self._load_and_render(
            {"kind": "music_genre", "server": "srv1", "item_id": "gn1",
             "parent_id": "musiclib", "title": "Jazz"})
        self.assertTrue(any(k.startswith("mgenre-") for k in h))

class TestNavBack(unittest.TestCase):
    """BACK from a remote (or ESC) unwinds one layer at a time, and declines
    at the root so the player can keep ESC's old meaning."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def test_declines_at_the_root(self):
        self.assertFalse(self.b.on_back())

    def test_pops_the_nav_stack(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.assertTrue(self.b.on_back())
        self.assertEqual(self.b.route["kind"], "home")

    def test_closes_a_dialog_before_navigating(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.b._message("hello")
        self.assertTrue(self.b.on_back())
        self.assertIsNone(self.b._dialog)
        self.assertEqual(self.b.route["kind"], "grid", "stack must not pop")

    def test_closes_a_tile_menu_first(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.b._open_tile_menu({"Id": "m1", "Name": "A", "Type": "Movie"},
                               10, 10)
        self.assertTrue(self.b.on_back())
        self.assertIsNone(self.b._menu)
        self.assertEqual(self.b.route["kind"], "grid")

class TestRouteErrors(unittest.TestCase):
    """A failed load left the route's data at None with no error path, so
    the view spun forever — an unreachable server looked like a hang."""

    def setUp(self):
        self.src = _FailingSource()
        self.b = MpvtkBrowser(app=None, source=self.src)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def _render(self):
        self.b._load_route(self.b.route)
        nodes, handlers = build_scene(self.b)
        return nodes, handlers

    def test_a_failed_load_reports_instead_of_spinning(self):
        nodes, _h = self._render()
        self.assertIn("route-retry", ids(nodes), "no retry offered")
        texts = " ".join(n.get("text", "") for n in nodes if n.get("text"))
        self.assertIn("Failed to load", texts)
        self.assertNotIn("busy", [n["t"] for n in nodes])

    def test_retry_reloads_and_recovers(self):
        _n, handlers = self._render()
        before = self.src.calls
        self.src.fail = False
        handlers["route-retry"]["click"]()
        self.assertGreater(self.src.calls, before, "retry did not refetch")
        self.assertIsNone(self.b.route.get("_error"))

    def test_a_stale_failure_is_cleared_on_reload(self):
        self._render()
        self.assertIsNotNone(self.b.route.get("_error"))
        self.src.fail = False
        self.b._load_route(self.b.route)
        self.assertIsNone(self.b.route.get("_error"))

    def test_a_failed_home_falls_back_to_downloads(self):
        """With downloads present, a dead server should land in the offline
        library rather than on an error where the downloads are."""
        class OfflineSrc(FakeSource):
            pass

        ctl = FakeController()
        offline = OfflineSrc()
        ctl.offline_source = lambda: offline
        b = MpvtkBrowser(app=None, source=_FailingSource(), controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        b._load_route(b.route)
        self.assertIs(b.source, offline, "did not fall back to downloads")

    def test_no_fallback_when_nothing_is_downloaded(self):
        ctl = FakeController()
        ctl.offline_source = lambda: None
        b = MpvtkBrowser(app=None, source=_FailingSource(), controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        b._load_route(b.route)
        nodes, _h = build_scene(b)
        self.assertIn("route-retry", ids(nodes))

class TestFailuresAreVisible(unittest.TestCase):
    """Error paths that existed, were wired, and could never fire.

    A lower layer caught the exception and logged it, which silently defeated
    the caller's on_error. The project made _edit, queue_reorder and
    playlist_move_many raise for exactly this reason; these are the ones that
    were missed.
    """

    def _browser(self, **ctl):
        c = FakeController()
        for k, v in ctl.items():
            setattr(c, k, v)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=c,
                         config=FakeConfig())
        b._pool = _SyncPool()
        return b

    @staticmethod
    def _boom(*a, **k):
        raise RuntimeError("nope")

    def test_a_failed_download_removal_says_so(self):
        b = self._browser(delete_download=self._boom)
        b.nav_stack = [{"kind": "detail", "server": "srv1"}]
        b.tiles._downloaded = {"m1"}
        b._remove_download({"Id": "m1", "Type": "Movie"})
        self.assertIn("could not be removed", b.status.lower())

    def test_a_failed_delete_in_the_downloads_panel_says_so(self):
        b = self._browser(delete_download=self._boom)
        route = {"kind": "settings", "server": "srv1", "_tab": "downloads"}
        b.nav_stack = [route]
        b._delete_download(route, item_id="m1")
        self.assertIn("could not be removed", b.status.lower())

    def test_a_failed_add_to_queue_says_so(self):
        b = self._browser(queue_items=self._boom)
        b.nav_stack = [{"kind": "detail", "server": "srv1"}]
        b._queue_items(["m1"], "srv1")
        self.assertIn("queue", b.status.lower())
        self.assertTrue(b.status, "a rejected queue-add was silent")

    def test_a_failed_add_to_queue_from_the_tile_menu_says_so(self):
        """The menu path resolves ids first, so it has its own on_error."""
        b = self._browser(queue_items=self._boom)
        b.nav_stack = [{"kind": "grid", "server": "srv1"}]
        b._menu_queue({"Id": "m1", "Type": "Movie"}, "srv1")
        self.assertIn("queue", b.status.lower())

    def test_a_failed_download_estimate_does_not_claim_nothing_to_do(self):
        """It returned a zero estimate, which the dialog renders as
        "Nothing left to download." — the failure read as success and the
        Download button was withheld."""
        b = self._browser(download_estimate=self._boom)
        b.nav_stack = [{"kind": "detail", "server": "srv1"}]
        b._open_download({"Id": "m1", "Name": "A Movie", "Type": "Movie"})
        nodes, _h = layout(b._dialog(), 1280, 720)
        texts = " ".join(n.get("text") or "" for n in nodes).lower()
        self.assertNotIn("nothing left", texts,
                         "a failed estimate claimed there was nothing to do")
        self.assertIn("could not check", texts)

    def test_a_duplicate_user_name_is_reported_and_kept(self):
        """It went through _safe, so the field cleared and nothing happened
        — the box looked like it had accepted the name."""
        b = self._browser(add_user=self._boom)
        b._newuser["name"] = "Bud"
        b._add_user("Bud")
        self.assertIn("could not be added", b.status.lower())
        self.assertEqual(b._newuser["name"], "Bud",
                         "the name was cleared as though it had worked")

    def test_a_successful_add_still_clears_the_field(self):
        b = self._browser(add_user=lambda name: None)
        b._newuser["name"] = "Bud"
        b._add_user("Bud")
        self.assertEqual(b._newuser["name"], "")

    def test_a_failed_rename_says_so(self):
        b = self._browser(rename_user=self._boom)
        b._open_rename_user({"id": "u1", "name": "Old"})
        _n, h = build_scene(b)
        h["ru-ok"]["click"]()
        self.assertIn("could not be renamed", b.status.lower())

    def test_a_failed_pin_save_keeps_the_dialog_open_with_an_error(self):
        """set_user_pin returns False on failure and _safe discarded it, so
        the dialog closed and the user believed their account was locked."""
        b = self._browser(set_user_pin=lambda *a, **k: False)
        b._open_pin_setup({"id": "u1", "name": "Bud", "locked": False})
        _n, h = build_scene(b)
        h["ps-new"]["change"]("1234")
        h["ps-confirm"]["change"]("1234")
        h["ps-ok"]["click"]()
        nodes, _h2 = build_scene(b)
        texts = [n.get("text") or "" for n in nodes]
        self.assertIsNotNone(b._dialog, "the dialog closed on a failed save")
        self.assertTrue(any("could not be saved" in t.lower() for t in texts),
                        "no error shown: %r" % texts)

    def test_a_failed_download_start_says_so(self):
        b = self._browser(download_enqueue=self._boom)
        b._dl = {"server": "srv1", "item": {"Id": "m1", "Type": "Movie"},
                 "est": 0, "watched": False}
        b._dl_confirm()
        self.assertIn("could not be started", b.status.lower())
        self.assertIsNone(b._dl, "the dialog stayed up")

    def test_a_failed_syncplay_join_says_so(self):
        b = self._browser(sync_join=self._boom)
        b._sync_join("srv1", "g1")
        self.assertIn("could not join", b.status.lower())

    def test_a_failed_syncplay_leave_says_so(self):
        b = self._browser(sync_leave=self._boom)
        b._sync_leave("srv1")
        self.assertIn("could not leave", b.status.lower())

    def test_a_successful_download_start_is_silent(self):
        started = []
        b = self._browser(download_enqueue=lambda *a, **k: started.append(a))
        b._dl = {"server": "srv1", "item": {"Id": "m1", "Type": "Movie"},
                 "est": 0, "watched": False}
        b._dl_confirm()
        self.assertEqual(len(started), 1)
        self.assertNotIn("could not", b.status.lower())

    def test_a_successful_pin_save_closes_the_dialog(self):
        b = self._browser(set_user_pin=lambda *a, **k: True)
        b._open_pin_setup({"id": "u1", "name": "Bud", "locked": False})
        _n, h = build_scene(b)
        h["ps-new"]["change"]("1234")
        h["ps-confirm"]["change"]("1234")
        h["ps-ok"]["click"]()
        self.assertIsNone(b._dialog)

class TestTheControllerDoesNotSwallow(unittest.TestCase):
    """The other half of TestFailuresAreVisible, and the half that matters.

    Those tests stub FakeController, so they prove the *browser* reacts to a
    raising controller — and nothing at all about whether the real controller
    raises. Restoring the swallows in ui.py leaves every one of them green.
    That is precisely the shape the migration write-up recorded (deleted in
    c37bfc3e; jellyfin_mpv_shim/mpvtk/GUIDE.md keeps the surviving section): "a delete-rollback whose
    test passed because it stubbed the controller and bypassed _edit
    entirely." These drive the real PlayerGateway.
    """

    def _controller(self):
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway
        return PlayerGateway()

    def _patch(self, module_path, name, obj):
        import importlib
        mod = importlib.import_module(module_path)
        real = getattr(mod, name)
        setattr(mod, name, obj)
        self.addCleanup(lambda: setattr(mod, name, real))

    @staticmethod
    def _boom(*a, **k):
        raise RuntimeError("server refused")

    def test_delete_download_propagates(self):
        self._patch("jellyfin_mpv_shim.sync.manager", "syncManager",
                    type("S", (), {"delete": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().delete_download(item_id="m1")

    def test_download_enqueue_propagates(self):
        self._patch("jellyfin_mpv_shim.sync.manager", "syncManager",
                    type("S", (), {"enqueue": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().download_enqueue("srv1", "m1", "Movie")

    def test_add_user_propagates(self):
        self._patch("jellyfin_mpv_shim.users", "userManager",
                    type("U", (), {"add_user": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().add_user("Bud")

    def test_rename_user_propagates(self):
        self._patch("jellyfin_mpv_shim.users", "userManager",
                    type("U", (), {"rename_user": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().rename_user("u1", "New")

    def test_syncplay_actions_propagate(self):
        class Client:
            jellyfin = type("J", (), {
                "join_sync_play": staticmethod(TestTheControllerDoesNotSwallow._boom),
                # v2, not new_sync_play: the pre-10.7 call posts no body and
                # GroupName is required, so it is a flat 400 on any current
                # server, and the dialog's New Group button never worked
                # while the identical OSD-menu button did.
                "new_sync_play_v2": staticmethod(
                    TestTheControllerDoesNotSwallow._boom),
                "leave_sync_play": staticmethod(TestTheControllerDoesNotSwallow._boom),
            })()

        # ui.py binds clientManager at module scope (`from ..clients import
        # clientManager`), so patching jellyfin_mpv_shim.clients would not
        # reach it. syncManager and userManager are imported inside their
        # methods, so those patch at the source module.
        self._patch("jellyfin_mpv_shim.mpvtk_browser.gateway.deps",
                    "clientManager",
                    type("C", (), {"clients": {"srv1": Client()}})())
        ctl = self._controller()
        for call in (lambda: ctl.sync_join("srv1", "g1"),
                     lambda: ctl.sync_new("srv1"),
                     lambda: ctl.sync_leave("srv1")):
            with self.subTest(call=call):
                with self.assertRaises(RuntimeError):
                    call()

    def test_queue_remove_propagates(self):
        """The queue view passes an on_error and renders a message, and that
        path could never fire: queue_remove went through _act, which logs and
        returns. The call site was "fixed" last round without touching the
        controller, so the failure stayed invisible AND the test stayed
        green — this is the test that would have caught that."""
        # Importing jellyfin_mpv_shim.player reaches args.get_args(), which
        # parses sys.argv — under `-m unittest <dotted.path>` that argv is a
        # test name argparse rejects with SystemExit(2). Same guard the
        # Media tests use above.
        import sys
        real_argv = sys.argv
        sys.argv = [sys.argv[0]]
        self.addCleanup(lambda: setattr(sys, "argv", real_argv))

        self._patch("jellyfin_mpv_shim.player", "playerManager",
                    type("P", (), {
                        "queue_remove_many": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().queue_remove(["p1"])

    def test_queue_items_propagates(self):
        """"Add to play queue" swallowed twice — here AND in _client_call
        at the
        call site — so a rejected add was doubly invisible."""
        import sys
        real_argv = sys.argv
        sys.argv = [sys.argv[0]]
        self.addCleanup(lambda: setattr(sys, "argv", real_argv))

        self._patch("jellyfin_mpv_shim.player", "playerManager",
                    type("P", (), {
                        "has_video": staticmethod(lambda: True),
                        "get_video": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().queue_items("srv1", ["m1"])

    def test_download_estimate_propagates(self):
        """It returned a ZERO estimate on failure, which the dialog renders
        as "Nothing left to download." — a server error reported to the user
        as "already on disk", with the Download button withheld."""
        self._patch("jellyfin_mpv_shim.sync.manager", "syncManager",
                    type("S", (), {"estimate": staticmethod(self._boom)})())
        with self.assertRaises(RuntimeError):
            self._controller().download_estimate("srv1", "m1", "Movie")

    def test_set_user_pin_still_reports_false_rather_than_raising(self):
        """This one deliberately does NOT raise — it returns a bool, and the
        PIN dialog checks it. The contract just has to be honest."""
        self._patch("jellyfin_mpv_shim.users", "userManager",
                    type("U", (), {"set_pin": staticmethod(self._boom)})())
        self.assertFalse(self._controller().set_user_pin("u1", "1234"))

class TestLatentFixes(unittest.TestCase):
    """Items that were wrong regardless of Tk."""

    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_favorite_rolls_back_when_nothing_recorded_it(self):
        self.ctl.set_favorite = lambda s, i, f: False
        item = {"Id": "m1", "Type": "Movie", "UserData": {"IsFavorite": False}}
        self.b._act_favorite(item, "srv1")
        self.assertFalse(item["UserData"]["IsFavorite"],
                         "heart kept for a change that never happened")

    def test_favorite_sticks_when_it_worked(self):
        self.ctl.set_favorite = lambda s, i, f: True
        item = {"Id": "m1", "Type": "Movie", "UserData": {"IsFavorite": False}}
        self.b._act_favorite(item, "srv1")
        self.assertTrue(item["UserData"]["IsFavorite"])

    def test_the_move_button_moves_what_is_in_the_field(self):
        """It passed None, whose only effect was a status line telling you
        to press Enter — a button that could never do its own job."""
        moved = []
        self.b._move_downloads = lambda p: moved.append(p)
        cfg = FakeConfig()
        cfg.schema["sync_path"] = "str"
        cfg.values["sync_path"] = "/old"
        self.b._config_obj = cfg
        row = self.b._setting_row(cfg, cfg.settings_schema(),
                                  cfg.get_settings(), "sync_path")
        _n, h = layout(row, 1280, 720)
        h["set-sync_path"]["change"]("/new/path")
        h["set-sync-move"]["click"]()
        self.assertEqual(moved, ["/new/path"])

    def test_an_update_notice_shows_while_offline(self):
        """The offline banner is persistent, so checking it first meant an
        update was never surfaced offline."""
        self.b._offline = True
        self.b.notify_update("2.0", "http://x")
        nodes, _h = build_scene(self.b)
        texts = " ".join(n.get("text", "") for n in nodes if n.get("text"))
        self.assertIn("Update available", texts)

    def test_a_mixed_playlist_reads_as_a_track_list(self):
        route = {"kind": "playlist", "server": "srv1", "item_id": "P",
                 "title": "Mix", "_data": [
                     {"Id": "a", "Type": "Audio", "Name": "Song"},
                     {"Id": "m", "Type": "Movie", "Name": "Film"}]}
        self.b.nav_stack = [route]
        nodes, _h = build_scene(self.b)
        self.assertIn("pl-0", ids(nodes), "rendered as a grid, not a list")

class TestStatusReachesTheScreen(unittest.TestCase):
    """status was rendered in ONE place — the Settings tab — while being
    written from fourteen, including _edit_call's "the change could not be
    applied". A rejected Add to Playlist from the home screen wrote its
    error to a field that never reached a pixel, which is the exact thing
    _edit_call exists to prevent. Assert it renders, not that it is set."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def _texts(self):
        nodes, _h = build_scene(self.b)
        return " ".join(n.get("text", "") for n in nodes if n.get("text"))

    def test_a_message_shows_on_the_home_screen(self):
        self.b.set_status("Something went wrong")
        self.assertIn("Something went wrong", self._texts())

    def test_a_message_shows_on_a_grid(self):
        self.b.nav_stack = [{"kind": "grid", "server": "srv1",
                             "parent_id": "lib1", "_items": []}]
        self.b.set_status("Nope")
        self.assertIn("Nope", self._texts())

    def test_a_rejected_edit_is_visible_where_it_happened(self):
        """The end-to-end case: an add-to rejected from a non-settings
        route must say so on that route."""
        ctl = FakeController()

        def boom(srv, pid, ids):
            raise RuntimeError("rejected")

        ctl.playlist_add = boom
        src = FakeSource()
        src.get_playlists = lambda srv: [{"Id": "p1", "Name": "Mix"}]
        b = MpvtkBrowser(app=None, source=src, controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        b._open_add_to({"Id": "m1", "Type": "Movie"})
        _n, h = build_scene(b)
        h["add-pl-0"]["click"]()
        nodes, _h = build_scene(b)
        texts = " ".join(n.get("text", "") for n in nodes if n.get("text"))
        self.assertIn("could not be applied", texts)

    def test_it_clears_once_it_has_expired(self):
        self.b.set_status("Transient")
        self.b._status_at = time.time() - (self.b.TOAST_SECS + 1)
        self.assertNotIn("Transient", self._texts())

    def test_no_message_no_toast(self):
        nodes, _h = build_scene(self.b)
        self.assertFalse([n for n in nodes if n["t"] == "float"])

class TestRenderErrorBoundary(unittest.TestCase):
    """One exception in any view used to kill the whole UI loop."""

    def test_a_failing_build_does_not_kill_the_loop(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        app = MpvtkApp.__new__(MpvtkApp)
        app.size = (1280, 720)
        app._dirty = True

        def boom(_size):
            raise RuntimeError("view blew up")

        app._build = boom
        app._render()          # must not raise
        self.assertFalse(app._dirty)

class TestSeasonGridUsesEpisodeStills(unittest.TestCase):
    """A season listing is a list of episodes, so every cell must be that
    episode's own artwork.

    The Thumb chain inherits from the series (its thumb, then the season's,
    then their backdrops) before falling back to the episode's own image.
    That is right for a Continue Watching card -- which is a pointer back to
    the show -- and wrong here: it draws the same series artwork in every
    cell and the grid stops distinguishing anything.

    jellyfin-web is no help as a reference: it renders a season's episodes
    as a *list view* with a leading image (itemDetails/index.js:1423-1435),
    never as a card grid, so this behaviour is ours to pin.
    """

    def _specs_for(self, route):
        """Every (item, image_type, inherit) the page asked artwork for."""
        seen = []
        src = FakeSource()
        real = src.image_spec

        def spy(item, image_type="Primary", width=280, inherit=True):
            seen.append((item.get("Id"), image_type, inherit))
            return real(item, image_type, width, inherit=inherit)

        src.image_spec = spy
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate(route)
        b.route["_data"] = {
            "episodes": [{"Id": "ep1", "Type": "Episode", "Name": "One",
                          "SeriesId": "sh1"},
                         {"Id": "ep2", "Type": "Episode", "Name": "Two",
                          "SeriesId": "sh1"}],
            "seasons": [{"Id": "sea1", "Name": "Season 1"}]}
        build_scene(b)
        return seen

    def test_the_season_grid_does_not_inherit_series_artwork(self):
        specs = self._specs_for({"kind": "season", "server": "srv1",
                                 "item_id": "sea1", "series_id": "sh1",
                                 "title": "Season 1"})
        eps = [s for s in specs if s[0] in ("ep1", "ep2")]
        self.assertTrue(eps, "the season grid asked for no episode artwork")
        for item_id, image_type, inherit in eps:
            self.assertEqual(image_type, "Thumb",
                             "%s is not a landscape tile" % item_id)
            self.assertFalse(inherit,
                             "%s inherited the series artwork" % item_id)


class TestPageNavigationHandlersActuallyRun(unittest.TestCase):
    """Click the navigation buttons the converted pages build.

    Every one of these went through ``ctx.nav.navigate``, which did not exist
    -- ctx.nav was wired to the raw Navigator. Nothing caught it because they
    are all lambdas and no test had ever clicked them. Static resolution now
    covers the class (test_late_bound_calls.TestPageContextCallsResolve);
    these cover the behaviour.
    """

    def _browser(self, route):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate(route)
        return b

    def test_go_to_series_from_an_episode_detail(self):
        b = self._browser({"kind": "detail", "server": "srv1",
                           "item_id": "ep1", "title": "Ep"})
        b.route["_data"] = {"item": {"Id": "ep1", "Type": "Episode",
                                     "Name": "Ep", "SeriesId": "sh1",
                                     "SeriesName": "Show"}}
        _n, handlers = build_scene(b)
        self.assertIn("act-series", handlers, "no Go to Series button")
        handlers["act-series"]["click"]()
        self.assertEqual(b.route.get("kind"), "series")
        self.assertEqual(b.route.get("item_id"), "sh1")

    def test_to_series_from_a_season(self):
        b = self._browser({"kind": "season", "server": "srv1",
                           "item_id": "sea1", "series_id": "sh1",
                           "title": "Season 1"})
        _n, handlers = build_scene(b)
        self.assertIn("season-to-series", handlers)
        handlers["season-to-series"]["click"]()
        self.assertEqual(b.route.get("kind"), "series")
        self.assertEqual(b.route.get("item_id"), "sh1")

    def test_the_season_switcher_navigates(self):
        b = self._browser({"kind": "season", "server": "srv1",
                           "item_id": "sea1", "series_id": "sh1",
                           "title": "Season 1"})
        b.route["_data"] = {
            "episodes": [],
            "seasons": [{"Id": "sea1", "Name": "Season 1"},
                        {"Id": "sea2", "Name": "Season 2"}]}
        _n, handlers = build_scene(b)
        self.assertIn("season-switch", handlers, "no season dropdown")
        handlers["season-switch"]["select"](1, "Season 2")
        self.assertEqual(b.route.get("item_id"), "sea2")


if __name__ == "__main__":
    unittest.main()
