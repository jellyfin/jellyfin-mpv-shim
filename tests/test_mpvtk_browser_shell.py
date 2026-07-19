"""Unit tests for the mpvtk browser shell (mpvtk_browser.app.MpvtkBrowser):
route stack, epoch-guarded async, and build() -> scene. Uses a fake data
source (no network) and app=None (invalidate is a no-op). Headless — the
tree is turned into a scene with the real layout engine and asserted on.
"""

import threading
import unittest

from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser


class FakeSource:
    """Minimal LibrarySource stand-in for the shell tests."""

    def __init__(self):
        self.libraries = [
            {"Id": "lib1", "Name": "Movies", "Type": "CollectionFolder",
             "CollectionType": "movies"},
        ]
        self.home_rows = [
            {"title": "Continue Watching", "items": [
                {"Id": "m1", "Name": "Alpha", "Type": "Movie",
                 "ProductionYear": 2001}],
             "collection_type": None},
        ]
        self.grid_items = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Movie"}
            for i in range(30)
        ]

    def servers(self):
        return [{"uuid": "srv1", "name": "Home Server"}]

    def get_libraries(self, server_uuid):
        return list(self.libraries)

    def get_home_rows(self, server_uuid, libraries=None):
        return list(self.home_rows)

    def get_library_items(self, server_uuid, parent_id, start_index=0,
                          **kw):
        page = self.grid_items[start_index:start_index + 20]
        return page, len(self.grid_items)

    def image_spec(self, item, image_type="Primary", width=280):
        return None  # no artwork in tests -> placeholder tiles, no network

    def image_url(self, *a, **k):
        return None

    def backdrop_spec(self, item):
        return None

    def backdrop_url(self, *a, **k):
        return None

    def get_item(self, server_uuid, item_id):
        return {"Id": item_id, "Name": "Detail %s" % item_id, "Type": "Movie",
                "Overview": "A short overview. " * 8, "ProductionYear": 2010,
                "RunTimeTicks": 90 * 600000000,
                "UserData": {"PlaybackPositionTicks": 30 * 10000000}}

    def get_similar(self, server_uuid, item_id, limit=12):
        return [{"Id": "s1", "Name": "Similar", "Type": "Movie"}]

    def get_seasons(self, server_uuid, series_id):
        return [{"Id": "se1", "Name": "Season 1", "Type": "Season",
                 "SeriesId": series_id},
                {"Id": "se2", "Name": "Season 2", "Type": "Season",
                 "SeriesId": series_id}]

    def get_episodes(self, server_uuid, series_id, season_id):
        return [{"Id": "e%d" % i, "Name": "Ep %d" % i, "Type": "Episode",
                 "ParentIndexNumber": 1, "IndexNumber": i} for i in range(5)]

    def search(self, server_uuid, term, limit=60):
        return [{"Id": "r1", "Name": "Result " + term, "Type": "Movie"}]

    def search_people(self, server_uuid, term, limit=60):
        return [{"Id": "p1", "Name": "Person", "Type": "Person"}]

    def get_music_albums(self, server_uuid, parent_id, **kw):
        return ([{"Id": "al%d" % i, "Name": "Album %d" % i,
                  "Type": "MusicAlbum"} for i in range(4)], 4)

    def get_album_artists(self, server_uuid, parent_id, **kw):
        return ([{"Id": "ar1", "Name": "Artist", "Type": "MusicArtist"}], 1)

    def get_music_genres(self, server_uuid, parent_id):
        return [{"Id": "gn1", "Name": "Jazz", "Type": "MusicGenre"}]

    def get_album_tracks(self, server_uuid, album_id):
        return [{"Id": "tk%d" % i, "Name": "Track %d" % i, "Type": "Audio",
                 "IndexNumber": i + 1, "RunTimeTicks": 200 * 10000000}
                for i in range(6)]

    def get_artist_albums(self, server_uuid, artist_id):
        return [{"Id": "al1", "Name": "Album", "Type": "MusicAlbum"}]

    def get_genre_albums(self, server_uuid, parent_id, genre_id, **kw):
        return ([{"Id": "al2", "Name": "GenreAlbum", "Type": "MusicAlbum"}], 1)

    def get_playlist_items(self, server_uuid, playlist_id):
        return [{"Id": "pi%d" % i, "Name": "Song %d" % i, "Type": "Audio"}
                for i in range(3)]


class _SyncPool:
    """Runs submitted work inline so route loaders complete deterministically
    within the test (no threads, no shutdown races)."""

    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


def build_scene(browser, size=(1280, 720)):
    nodes, handlers = layout(browser.build(size), *size)
    return nodes, handlers


def ids(nodes):
    return {n.get("id") for n in nodes}


def types(nodes):
    return [n["t"] for n in nodes]


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
        self.b.nav_stack.append({"kind": "connecting"})
        nodes, _h = build_scene(self.b)
        self.assertNotIn("nav-home", ids(nodes))


class FakeController:
    def __init__(self):
        self.entered = 0
        self.left = 0
        self.played = []

    def on_browse_enter(self):
        self.entered += 1

    def on_browse_leave(self):
        self.left += 1

    def play(self, item, server_uuid, offset_ticks=None):
        self.played.append((item.get("Id"), server_uuid, offset_ticks))

    def play_list(self, item_ids, server_uuid, start_index, offset_ticks=None):
        self.played.append((list(item_ids), server_uuid, start_index))

    def __getattr__(self, name):
        # Record transport calls (toggle_pause/stop/next/prev/…) without
        # declaring each one.
        if name.startswith(("_", "on_")) or name in ("play", "play_list"):
            raise AttributeError(name)
        calls = self.__dict__.setdefault("transport", [])
        return lambda *a, **k: calls.append((name, a))


class TestPlaybackLifecycle(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def test_click_playable_opens_detail(self):
        self.b._open_item({"Id": "m1", "Name": "Alpha", "Type": "Movie"})
        self.assertEqual(self.b.route["kind"], "detail")
        self.assertEqual(self.b.route["item_id"], "m1")
        self.assertTrue(self.b._browsing, "opening detail must not yield")

    def test_play_yields_and_starts(self):
        item = {"Id": "m1", "Name": "Alpha", "Type": "Movie"}
        self.b._play(item, "srv1", offset_ticks=123)
        self.assertFalse(self.b._browsing, "browser should yield to playback")
        self.assertEqual(self.ctl.left, 1)     # OSC handed back
        self.assertEqual(self.ctl.played, [("m1", "srv1", 123)])

    def test_yielded_build_is_empty(self):
        self.b._browsing = False
        nodes, _h = build_scene(self.b)
        # No strip overlays / chrome while yielded to the video + OSC.
        self.assertNotIn("img", types(nodes))
        self.assertNotIn("nav-home", ids(nodes))

    def test_playstate_stopped_returns_to_browse(self):
        self.b._browsing = False
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b._browsing)
        self.assertEqual(self.ctl.entered, 1)   # took the window + OSC off

    def test_playstate_playing_keeps_yielded(self):
        self.b._browsing = True
        self.b.on_playstate({"stopped": False, "position": 5})
        self.assertFalse(self.b._browsing)

    def test_enter_browse_calls_controller(self):
        self.b.enter_browse()
        self.assertTrue(self.b._browsing)
        self.assertGreaterEqual(self.ctl.entered, 1)

    def test_set_source_repopulates_and_resets_home(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b.navigate({"kind": "grid", "parent_id": "lib1"})
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.server, "srv1")
        self.assertEqual(b.route["kind"], "home")
        self.assertEqual(len(b.nav_stack), 1)


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


class TestNowPlaying(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def test_audio_play_keeps_browsing_and_shows_bar(self):
        self.b._play_list(["t1", "t2"], "srv1", 0, audio=True)
        self.assertTrue(self.b._browsing, "audio must not yield the window")
        self.assertIsNotNone(self.b._now_playing)

    def test_video_play_yields(self):
        self.b._play({"Id": "m1", "Type": "Movie"}, "srv1")
        self.assertFalse(self.b._browsing)
        self.assertIsNone(self.b._now_playing)

    def test_playstate_audio_populates_bar(self):
        self.b.on_playstate({"stopped": False, "is_audio": True,
                             "title": "Song", "artist": "Band",
                             "position": 65, "duration": 200, "paused": False})
        self.assertTrue(self.b._browsing)
        nodes, handlers = build_scene(self.b)
        self.assertIn("np-pp", ids(nodes))
        self.assertIn("np-stop", ids(nodes))
        # transport wired to the controller
        handlers["np-pp"]["click"]()
        handlers["np-next"]["click"]()
        names = [c[0] for c in getattr(self.ctl, "transport", [])]
        self.assertIn("toggle_pause", names)
        self.assertIn("next", names)

    def test_playstate_stopped_clears_bar(self):
        self.b.on_playstate({"stopped": False, "is_audio": True,
                             "title": "S", "duration": 10, "position": 1})
        self.b.on_playstate({"stopped": True})
        self.assertIsNone(self.b._now_playing)
        nodes, _h = build_scene(self.b)
        self.assertNotIn("np-pp", ids(nodes))

    def test_video_playstate_yields_no_bar(self):
        self.b.on_playstate({"stopped": False, "is_audio": False,
                             "title": "Movie", "position": 5, "duration": 100})
        self.assertFalse(self.b._browsing)
        self.assertIsNone(self.b._now_playing)


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
        self.b._set_music_tab(self.b.route, "artists")
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


if __name__ == "__main__":
    unittest.main()
