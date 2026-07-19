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

    def get_filter_values(self, server_uuid, parent_id=None):
        return {"genres": ["Action", "Comedy"], "years": [2020, 2021]}

    def get_shuffle_ids(self, server_uuid, parent_id, limit=200):
        return ["g0", "g5", "g9"]

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
                "UserData": {"PlaybackPositionTicks": 30 * 10000000},
                "People": [{"Id": "pp1", "Name": "Actor One", "Type": "Actor"}],
                "MediaSources": [{
                    "Id": "src1", "Container": "mkv",
                    "MediaStreams": [
                        {"Type": "Video", "Height": 1080,
                         "DisplayTitle": "1080p HEVC", "VideoRange": "HDR"},
                        {"Type": "Audio", "Index": 1,
                         "DisplayTitle": "English 5.1"},
                        {"Type": "Subtitle", "Index": 2,
                         "DisplayTitle": "English"}]}]}

    def get_similar(self, server_uuid, item_id, limit=12):
        return [{"Id": "s1", "Name": "Similar", "Type": "Movie"}]

    def get_person_items(self, server_uuid, person_id, start_index=0, **kw):
        items = [{"Id": "pf%d" % i, "Name": "Film %d" % i, "Type": "Movie"}
                 for i in range(4)]
        return items[start_index:start_index + 20], len(items)

    def get_next_up(self, server_uuid, series_id):
        return {"Id": "nu1", "Name": "Next Ep", "Type": "Episode",
                "SeriesId": series_id}

    def get_series_queue(self, server_uuid, series_id, start_item_id=None,
                         limit=100):
        return [{"Id": "e%d" % i} for i in range(3)]

    def get_seasons(self, server_uuid, series_id):
        return [{"Id": "se1", "Name": "Season 1", "Type": "Season",
                 "SeriesId": series_id},
                {"Id": "se2", "Name": "Season 2", "Type": "Season",
                 "SeriesId": series_id}]

    def get_episodes(self, server_uuid, series_id, season_id):
        return [{"Id": "e%d" % i, "Name": "Ep %d" % i, "Type": "Episode",
                 "ParentIndexNumber": 1, "IndexNumber": i} for i in range(5)]

    def search(self, server_uuid, term, limit=60):
        return [{"Id": "r1", "Name": "Movie " + term, "Type": "Movie"},
                {"Id": "r2", "Name": "Ep", "Type": "Episode"},
                {"Id": "r3", "Name": "Album", "Type": "MusicAlbum"},
                {"Id": "r4", "Name": "Song", "Type": "Audio"}]

    def search_people(self, server_uuid, term, limit=60):
        return [{"Id": "p1", "Name": "Person", "Type": "Person"}]

    def get_music_albums(self, server_uuid, parent_id, **kw):
        return ([{"Id": "al%d" % i, "Name": "Album %d" % i,
                  "Type": "MusicAlbum"} for i in range(4)], 4)

    def get_album_artists(self, server_uuid, parent_id, **kw):
        return ([{"Id": "ar1", "Name": "Artist", "Type": "MusicArtist"}], 1)

    def get_artists(self, server_uuid, parent_id, **kw):
        return ([{"Id": "ar2", "Name": "Artist 2", "Type": "MusicArtist"}], 1)

    def get_songs(self, server_uuid, parent_id, **kw):
        return ([{"Id": "so%d" % i, "Name": "Song %d" % i, "Type": "Audio",
                  "IndexNumber": i + 1} for i in range(5)], 5)

    def get_artist_songs(self, server_uuid, artist_id, limit=500):
        return [{"Id": "as%d" % i, "Name": "AS %d" % i, "Type": "Audio"}
                for i in range(4)]

    def get_genre_songs(self, server_uuid, parent_id, genre_id, limit=500):
        return [{"Id": "gs%d" % i, "Name": "GS %d" % i, "Type": "Audio"}
                for i in range(4)]

    def get_instant_mix(self, server_uuid, item_id, limit=200):
        return [{"Id": "mix%d" % i, "Type": "Audio"} for i in range(3)]

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
        return [{"Id": "pi%d" % i, "Name": "Song %d" % i, "Type": "Audio",
                 "PlaylistItemId": "e%d" % i} for i in range(3)]

    def get_playlists(self, server_uuid, limit=300):
        return [{"Id": "PL1", "Name": "Faves", "Type": "Playlist"},
                {"Id": "PL2", "Name": "Road Trip", "Type": "Playlist"}]

    def get_items_by_ids(self, server_uuid, ids):
        return [{"Id": i, "Name": "Queued " + i, "Type": "Audio"} for i in ids]


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
        self.minimized = 0
        self.played = []
        self.transport = []

    def on_browse_enter(self):
        self.entered += 1

    def on_browse_leave(self):
        self.left += 1

    def on_minimize(self):
        self.minimized += 1

    def play(self, item, server_uuid, offset_ticks=None, srcid=None,
             aid=None, sid=None):
        self.played.append((item.get("Id"), server_uuid, offset_ticks))
        self.__dict__.setdefault("tracks", []).append(
            {"srcid": srcid, "aid": aid, "sid": sid})

    def play_list(self, item_ids, server_uuid, start_index, offset_ticks=None,
                  srcid=None, aid=None, sid=None):
        self.played.append((list(item_ids), server_uuid, start_index))
        self.__dict__.setdefault("tracks", []).append(
            {"srcid": srcid, "aid": aid, "sid": sid})

    def get_queue(self):
        return {"items": [{"id": "q%d" % i, "playlist_item_id": "p%d" % i}
                          for i in range(3)], "current_id": "q1"}

    def get_sync_groups(self, server_uuid):
        return [{"id": "g1", "name": "Group 1"}]

    def download_estimate(self, server, item_id, item_type):
        return {"count": 3, "total_bytes": 5 * 1024 * 1024,
                "audio_only": False}

    def add_server(self, server, username, password):
        self.__dict__.setdefault("transport", []).append(
            ("add_server", (server, username, password)))
        return server == "good"

    def rebuild_source(self):
        return FakeSource()

    def unlock(self, pin):
        return pin == "1234"

    def connect_and_rebuild(self):
        return FakeSource()

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

    def test_minimize_releases_the_window_and_survives_a_cast(self):
        """"Minimized" is a player state (playback_abort + no force_window),
        not a hidden window: a cast while minimized must return to minimized
        when it ends, not pop the library open."""
        self.b.minimize()
        self.assertTrue(self.b.minimized)
        self.assertFalse(self.b._browsing)
        self.assertEqual(self.ctl.minimized, 1)

        self.b.on_playstate({"stopped": False, "is_audio": False})
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b.minimized, "cast ended -> back to minimized")
        self.assertFalse(self.b._browsing)

    def test_enter_browse_clears_minimized(self):
        self.b.minimize()
        self.b.enter_browse()
        self.assertFalse(self.b.minimized)
        self.assertTrue(self.b._browsing)

    def test_stop_while_not_minimized_still_opens_the_browser(self):
        self.b._browsing = False
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b._browsing)
        self.assertFalse(self.b.minimized)

    def test_yield_suspends_the_renderer(self):
        """An empty scene is not enough to hand input to the OSC — the
        renderer's forced mouse/wheel bindings have to be unbound too."""
        class FakeApp:
            def __init__(self):
                self.active = []

            def invalidate(self):
                pass

            def set_active(self, on):
                self.active.append(on)

        app = FakeApp()
        b = MpvtkBrowser(app=app, source=FakeSource(), controller=self.ctl)
        b._play({"Id": "m1", "Name": "A", "Type": "Movie"}, "srv1")
        self.assertEqual(app.active[-1], False)
        b.on_playstate({"stopped": True})
        self.assertEqual(app.active[-1], True)

    def test_set_source_repopulates_and_resets_home(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b.navigate({"kind": "grid", "parent_id": "lib1"})
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.server, "srv1")
        self.assertEqual(b.route["kind"], "home")
        self.assertEqual(len(b.nav_stack), 1)


class FakeConfig:
    # Mirrors the real mpvtk_browser.config surface the Settings view uses:
    # a schema, curated sections, enum tables and friendly labels.
    ENUMS = {"osc_mode": ["auto", "never"]}
    LABELED_ENUMS = {"lang": [("Unset", "unset"), ("Dubbed", "dub")]}

    def __init__(self):
        self.values = {"autoplay": True, "player_name": "Bud",
                       "seek_up": 60, "osc_mode": "auto", "lang": "unset"}
        self.schema = {"autoplay": "bool", "player_name": "str",
                       "seek_up": "int", "osc_mode": "str", "lang": "str"}

    def sections(self):
        return [("Interface", ["player_name", "osc_mode", "lang"]),
                ("Advanced", ["autoplay", "seek_up"])]

    @staticmethod
    def label_for(key):
        return key.replace("_", " ").title()

    def settings_schema(self):
        return dict(self.schema)

    def get_settings(self):
        return dict(self.values)

    def set_setting(self, key, value):
        kind = self.schema.get(key)
        if kind is None:
            return False
        try:
            self.values[key] = {"bool": bool, "int": int,
                                "str": str}[kind](value)
        except (ValueError, TypeError):
            return False
        return True


class TestSettings(unittest.TestCase):
    def setUp(self):
        self.cfg = FakeConfig()
        self.b = MpvtkBrowser(app=None, source=FakeSource(), config=self.cfg)

    def _advanced(self):
        """Reveal the Advanced section (autoplay/seek_up live there)."""
        self.b.route["_advanced"] = True

    def test_settings_nav_and_render(self):
        self.b._open_settings()
        self.assertEqual(self.b.route["kind"], "settings")
        self._advanced()
        nodes, _h = build_scene(self.b)
        self.assertIn("set-autoplay", ids(nodes))     # bool -> checkbox
        self.assertIn("set-player_name", ids(nodes))  # str -> textbox
        self.assertIn("set-osc_mode", ids(nodes))     # enum -> dropdown

    def test_settings_tabs_present(self):
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        for tab in ("general", "servers", "downloads", "logs"):
            self.assertIn("stab-" + tab, ids(nodes))

    def test_advanced_section_is_collapsed_by_default(self):
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("set-autoplay", ids(nodes))
        self.assertIn("set-adv", ids(nodes))

    def test_enum_dropdown_stores_value_not_label(self):
        self.b._open_settings()
        nodes, handlers = build_scene(self.b)
        handlers["set-lang"]["select"](1, "Dubbed")
        self.assertEqual(self.cfg.values["lang"], "dub")

    def test_setting_bool_toggle_saves(self):
        self.b._open_settings()
        self._advanced()
        nodes, handlers = build_scene(self.b)
        handlers["set-autoplay"]["click"]()
        self.assertFalse(self.cfg.values["autoplay"])

    def test_setting_text_submit_coerces(self):
        self.b._open_settings()
        self._advanced()
        nodes, handlers = build_scene(self.b)
        handlers["set-seek_up"]["submit"]("15")
        self.assertEqual(self.cfg.values["seek_up"], 15)  # coerced to int

    def test_setting_invalid_value_reports(self):
        self.b._open_settings()
        self._advanced()
        nodes, handlers = build_scene(self.b)
        handlers["set-seek_up"]["submit"]("not-a-number")
        self.assertIn("Invalid", self.b.status)


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


class TestTileShapes(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()

    def test_row_shape_classification(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            POSTER_GEOM, LANDSCAPE_GEOM, SQUARE_GEOM)
        g, _it = self.b._row_shape({"collection_type": "movies", "items": []})
        self.assertIs(g, POSTER_GEOM)
        g, _it = self.b._row_shape({"collection_type": "music", "items": []})
        self.assertIs(g, SQUARE_GEOM)
        g, it = self.b._row_shape(
            {"collection_type": None, "items": [{"Type": "Episode"}]})
        self.assertIs(g, LANDSCAPE_GEOM)
        self.assertEqual(it, "Thumb")
        # collection-type wins over a stray episode in the row
        g, _it = self.b._row_shape(
            {"collection_type": "tvshows", "items": [{"Type": "Episode"}]})
        self.assertIs(g, POSTER_GEOM)

    def test_scroll_arrows_appear_only_when_the_row_overflows(self):
        # One library fits, so no arrows; a long row gets them, pinned to the
        # window edges.
        self.b.route["_data"] = {"libraries": self.b.source.libraries,
                                 "rows": []}
        nodes, _h = build_scene(self.b)
        self.assertNotIn("row-libs-pl", ids(nodes))

        many = [dict(self.b.source.libraries[0], Id="lib%d" % i,
                     Name="Library %d" % i) for i in range(30)]
        self.b.route["_data"] = {"libraries": many, "rows": []}
        nodes, _h = build_scene(self.b)
        self.assertIn("row-libs-pl", ids(nodes))
        self.assertIn("row-libs-pr", ids(nodes))
        by_id = {n["id"]: n for n in nodes}
        self.assertEqual(by_id["row-libs-pl"]["x"], 0.0)
        # Flush right, inside the vertical scrollbar's gutter.
        from jellyfin_mpv_shim.mpvtk.layout import SCROLLBAR_W
        self.assertAlmostEqual(
            by_id["row-libs-pr"]["x"] + by_id["row-libs-pr"]["w"],
            self.b._size[0] - SCROLLBAR_W, places=1)

    def test_downloaded_and_glyph(self):
        self.b._downloaded = {"m1"}
        t = self.b._tile({"Id": "m1", "Name": "Alpha", "Type": "Movie"},
                         self.b.geom)
        self.assertTrue(t.downloaded)
        self.assertEqual(t.glyph, "A")
        t2 = self.b._tile({"Id": "a1", "Name": "Song", "Type": "Audio"},
                          self.b.geom)
        self.assertEqual(t2.glyph, "♪")

    def test_watched_series_fallback(self):
        t = self.b._tile({"Id": "s1", "Type": "Series",
                          "UserData": {"UnplayedItemCount": 0}}, self.b.geom)
        self.assertTrue(t.watched)
        t2 = self.b._tile({"Id": "s2", "Type": "Series",
                           "UserData": {"UnplayedItemCount": 3}}, self.b.geom)
        self.assertFalse(t2.watched)

    def test_season_episodes_are_landscape(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        self.b.navigate({"kind": "season", "server": "srv1", "item_id": "se1",
                         "series_id": "sh1", "title": "Season 1"})
        nodes, _h = build_scene(self.b)
        imgs = [n for n in nodes if n["t"] == "img"]
        self.assertTrue(imgs)
        self.assertEqual(imgs[0]["ih"], LANDSCAPE_GEOM.strip_h)


class TestDetailActions(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _detail(self):
        self.b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1",
                         "title": "Movie"})
        return build_scene(self.b)

    def test_action_row_and_pickers_render(self):
        nodes, _h = self._detail()
        for nid in ("act-watched", "act-fav", "act-download",
                    "dt-audio", "dt-sub"):
            self.assertIn(nid, ids(nodes))
        # cast row present, single source -> no version picker
        self.assertNotIn("dt-version", ids(nodes))
        self.assertTrue(any(k.startswith("detail-people-") for k in _h))

    def test_track_selection_passed_to_play(self):
        _n, h = self._detail()
        h["dt-audio"]["select"](0, "English 5.1")     # aid=1
        h["dt-sub"]["select"](1, "English")           # sid=2 (index 0 = None)
        _n, h = build_scene(self.b)
        h["btn-play"]["click"]()
        self.assertEqual(self.ctl.tracks[-1],
                         {"srcid": "src1", "aid": 1, "sid": 2})

    def test_mark_watched_from_detail(self):
        _n, h = self._detail()
        h["act-watched"]["click"]()
        self.assertIn("set_watched",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_cast_click_opens_person_route(self):
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        self.assertEqual(self.b.route["kind"], "person")
        nodes, _h = build_scene(self.b)
        self.assertIn("img", types(nodes))   # person filmography grid

    def test_episode_play_queues_season(self):
        ep = {"Id": "e1", "Type": "Episode", "SeriesId": "sh1"}
        self.b._play(ep, "srv1")
        ids_, srv, start = self.ctl.played[-1]
        self.assertEqual(len(ids_), 3)        # whole-season queue
        self.assertEqual(start, 0)

    def test_series_actions_next_up(self):
        self.b.navigate({"kind": "series", "server": "srv1", "item_id": "sh1",
                         "title": "Show"})
        _n, h = build_scene(self.b)
        self.assertIn("sa-nextup", ids(_n))
        h["sa-nextup"]["click"]()
        self.assertTrue(self.ctl.played)      # next-up episode played


class TestMusicDepth(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _music(self, tab=None):
        route = {"kind": "music", "server": "srv1", "parent_id": "ml",
                 "title": "Music"}
        if tab:
            route["_tab"] = tab
        self.b.navigate(route)
        return build_scene(self.b)

    def test_all_five_tabs(self):
        nodes, _h = self._music()
        for t in ("mtab-albums", "mtab-albumartists", "mtab-artists",
                  "mtab-songs", "mtab-genres"):
            self.assertIn(t, ids(nodes))

    def test_songs_tab_is_track_list(self):
        _n, h = self._music(tab="songs")
        self.assertTrue(any(k.startswith("song-") for k in h))
        h[next(k for k in h if k == "song-2")]["click"]()
        ids_, _srv, start = self.ctl.played[-1]
        self.assertEqual(start, 2)

    def test_album_action_bar(self):
        self.b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                         "title": "Album"})
        nodes, h = build_scene(self.b)
        for nid in ("album-play", "album-shuffle", "album-queue", "album-mix"):
            self.assertIn(nid, ids(nodes))
        h["album-queue"]["click"]()
        self.assertIn("queue_items",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_instant_mix_plays(self):
        self.b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                         "title": "Album"})
        _n, h = build_scene(self.b)
        h["album-mix"]["click"]()
        ids_, _srv, _s = self.ctl.played[-1]
        self.assertEqual(ids_, ["mix0", "mix1", "mix2"])

    def test_artist_action_bar_and_albums(self):
        self.b.navigate({"kind": "artist", "server": "srv1", "item_id": "ar1",
                         "title": "Artist"})
        nodes, h = build_scene(self.b)
        self.assertIn("art-play", ids(nodes))
        self.assertTrue(any(k.startswith("artist-") for k in h))


class TestGridFilters(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _grid(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        return build_scene(self.b)

    def test_filter_bar_present(self):
        nodes, _h = self._grid()
        for nid in ("grid-sort", "grid-genre", "grid-unplayed", "grid-fav",
                    "grid-shuffle", "grid-l-A", "grid-l-#"):
            self.assertIn(nid, ids(nodes))

    def test_sort_change_sets_and_reloads(self):
        _n, h = self._grid()
        h["grid-sort"]["select"](3, "Community Rating")
        self.assertEqual(self.b.route["_sort"], 3)

    def test_genre_filter(self):
        _n, h = self._grid()
        h["grid-genre"]["select"](1, "Action")   # index 0 = All Genres
        self.assertEqual(self.b.route["_filters"]["genre"], "Action")

    def test_unplayed_toggle(self):
        _n, h = self._grid()
        h["grid-unplayed"]["click"]()
        self.assertTrue(self.b.route["_filters"]["unplayed"])

    def test_letter_jump(self):
        _n, h = self._grid()
        h["grid-l-M"]["click"]()
        self.assertEqual(self.b.route["_filters"]["letter"], "M")

    def test_shuffle_plays(self):
        _n, h = self._grid()
        h["grid-shuffle"]["click"]()
        self.assertTrue(self.ctl.played)
        ids_, _srv, _s = self.ctl.played[-1]
        self.assertEqual(ids_, ["g0", "g5", "g9"])


class TestTileContextMenu(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_context_opens_menu(self):
        self.b._open_tile_menu({"Id": "m1", "Name": "A", "Type": "Movie"},
                               100, 200)
        nodes, _h = build_scene(self.b)
        self.assertTrue(any(n["t"] == "menu" for n in nodes))

    def test_mark_watched_calls_client_and_updates_item(self):
        item = {"Id": "m1", "Name": "A", "Type": "Movie",
                "UserData": {"Played": False}}
        self.b._open_tile_menu(item, 10, 10)
        self.b._menu_action(1, None)          # "Mark Watched"
        self.assertTrue(item["UserData"]["Played"])
        self.assertIsNone(self.b._menu)       # menu closed
        calls = getattr(self.ctl, "transport", [])
        self.assertIn("set_watched", [c[0] for c in calls])

    def test_toggle_favorite_calls_client(self):
        item = {"Id": "m1", "Type": "Movie", "UserData": {"IsFavorite": False}}
        self.b._open_tile_menu(item, 10, 10)
        self.b._menu_action(2, None)          # "Add to Favorites"
        self.assertTrue(item["UserData"]["IsFavorite"])
        self.assertIn("set_favorite",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_menu_play_audio_plays(self):
        item = {"Id": "s1", "Type": "Audio"}
        self.b._open_tile_menu(item, 10, 10)
        self.b._menu_action(0, None)          # "Play"
        self.assertTrue(self.ctl.played)

    def test_dismiss_closes_menu(self):
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        self.b._close_menu()
        self.assertIsNone(self.b._menu)


class TestPlaylistEdit(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _open_edit(self):
        self.b.navigate({"kind": "playlist_edit", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        return build_scene(self.b)

    def test_edit_renders_rows_and_toolbar(self):
        nodes, _h = self._open_edit()
        self.assertIn("pe-top", ids(nodes))
        self.assertIn("pe-row-0", ids(nodes))

    def test_move_down_reorders_and_calls_api(self):
        self._open_edit()
        route = self.b.route
        first = route["_items"][0]["PlaylistItemId"]
        self.b._pe_set_sel(route, {0})
        self.b._pe_move(route, "down")
        self.assertEqual(route["_items"][1]["PlaylistItemId"], first)
        self.assertEqual(route["_sel"], {1})
        self.assertIn("playlist_move",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_remove_drops_row_and_calls_api(self):
        self._open_edit()
        route = self.b.route
        self.b._pe_set_sel(route, {1})
        n0 = len(route["_items"])
        self.b._pe_remove(route)
        self.assertEqual(len(route["_items"]), n0 - 1)
        self.assertIn("playlist_remove",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_click_toggles_multi_select(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]()
        h["pe-row-2"]["click"]()
        self.assertEqual(self.b.route["_sel"], {0, 2})
        h["pe-row-0"]["click"]()          # toggles back off
        self.assertEqual(self.b.route["_sel"], {2})

    def test_block_move_keeps_selection_contiguous(self):
        self._open_edit()
        route = self.b.route
        ids0 = [i["PlaylistItemId"] for i in route["_items"]]
        self.b._pe_set_sel(route, {1, 2})
        self.b._pe_move(route, "top")
        self.assertEqual([i["PlaylistItemId"] for i in route["_items"]],
                         [ids0[1], ids0[2], ids0[0]])
        self.assertEqual(route["_sel"], {0, 1})

    def test_bulk_remove_sends_one_call(self):
        self._open_edit()
        route = self.b.route
        self.b._pe_set_sel(route, {0, 2})
        self.b._pe_remove(route)
        self.assertEqual(len(route["_items"]), 1)
        calls = [c for c in self.ctl.transport if c[0] == "playlist_remove"]
        self.assertEqual(len(calls), 1)


class TestVirtualizedGrid(unittest.TestCase):
    """Long grids must only composite the rows near the viewport: rendering
    all of them blew past the strip cache and mpv's 63-overlay budget, which
    showed as tiles that came back blank after scrolling away and back."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        # A library far taller than one screen.
        self.b.route["_items"] = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Movie"}
            for i in range(600)]
        self.b.route["_total"] = 600

    def _strip_count(self, nodes):
        return len([n for n in nodes if n["t"] == "img"])

    def test_only_a_window_of_rows_is_composited(self):
        nodes, _h = build_scene(self.b)
        n = self._strip_count(nodes)
        self.assertGreater(n, 0)
        self.assertLess(n, 40, "should not materialize every row")

    def test_scrolling_moves_the_window(self):
        build_scene(self.b)
        top = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.b._on_scroll("grid", 6000, 20000)
        bottom = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.assertTrue(top and bottom)
        self.assertNotEqual(top, bottom)

    def test_scrolling_back_re_materializes_the_original_rows(self):
        first = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.b._on_scroll("grid", 6000, 20000)
        build_scene(self.b)
        self.b._on_scroll("grid", 0, 20000)
        again = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.assertEqual(first, again)


class TestMusicPaging(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()

    def test_near_end_scroll_pages_the_tab(self):
        src = self.b.source
        page = [{"Id": "al%d" % i, "Name": "Album %d" % i,
                 "Type": "MusicAlbum"} for i in range(100)]
        calls = []

        def get_music_albums(server_uuid, parent_id, start_index=0, **kw):
            calls.append(start_index)
            return (page if start_index == 0 else page[:20]), 120
        src.get_music_albums = get_music_albums

        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "lib1", "title": "Music"})
        self.assertEqual(len(self.b.route["_data"]), 100)
        self.b._on_music_scroll(self.b.route, 9500, 10000)
        self.assertEqual(calls, [0, 100])
        self.assertEqual(len(self.b.route["_data"]), 120)

    def test_far_from_the_end_does_not_page(self):
        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "lib1", "title": "Music"})
        self.b.route["_total"] = 500
        before = len(self.b.route["_data"])
        self.b._on_music_scroll(self.b.route, 100, 10000)
        self.assertEqual(len(self.b.route["_data"]), before)


class TestAddToPlaylist(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_add_to_dialog_lists_playlists_and_adds(self):
        self.b._open_add_to({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        nodes, handlers = build_scene(self.b)
        self.assertIn("add-pl-0", ids(nodes))
        self.assertIn("add-pl-1", ids(nodes))
        handlers["add-pl-0"]["click"]()
        self.assertIn("playlist_add",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIsNone(self.b._dialog)

    def test_menu_add_to_playlist_opens_dialog(self):
        self.b._pool = _SyncPool()
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        self.b._menu_action(3, None)   # "Add to Playlist"
        self.assertIsNone(self.b._menu)
        self.assertIsNotNone(self.b._dialog)

    def test_create_new_playlist(self):
        self.b._open_add_to({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        _n, h = build_scene(self.b)
        self.assertIn("add-newname", ids(_n))
        h["add-newname"]["change"]("Road Trip")
        h["add-create"]["click"]()
        self.assertIn("playlist_new", [c[0] for c in self.ctl.transport])
        self.assertIsNone(self.b._dialog)


class TestPlaylistExtras(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_playlist_shuffle_and_download_buttons(self):
        self.b.navigate({"kind": "playlist", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        nodes, _h = build_scene(self.b)
        for nid in ("pl-play", "pl-shuffle", "pl-download", "pl-edit"):
            self.assertIn(nid, ids(nodes))

    def test_playlist_edit_rename_and_public(self):
        self.b.navigate({"kind": "playlist_edit", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        nodes, h = build_scene(self.b)
        for nid in ("pe-name", "pe-rename", "pe-public"):
            self.assertIn(nid, ids(nodes))
        h["pe-name"]["change"]("Renamed")
        h["pe-rename"]["click"]()
        self.assertEqual(self.b.route["title"], "Renamed")
        self.assertIn("playlist_update", [c[0] for c in self.ctl.transport])
        # The Public toggle refuses until the server's real visibility has
        # been read, so a first click can't flip an already-public list.
        _n, h = build_scene(self.b)
        h["pe-public"]["click"]()
        self.assertFalse(self.b.route.get("_public"))
        self.b.route["_public_known"] = True
        _n, h = build_scene(self.b)
        h["pe-public"]["click"]()
        self.assertTrue(self.b.route["_public"])


class TestLogin(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_show_login_renders_form(self):
        self.b.show_login()
        self.assertEqual(self.b.route["kind"], "login")
        nodes, _h = build_scene(self.b)
        for fid in ("login-server", "login-user", "login-pass",
                    "login-connect"):
            self.assertIn(fid, ids(nodes))
        # login is chrome-free
        self.assertNotIn("nav-home", ids(nodes))

    def test_login_failure_shows_error(self):
        self.b.show_login()
        _n, handlers = build_scene(self.b)
        handlers["login-server"]["change"]("bad")
        handlers["login-user"]["change"]("u")
        handlers["login-pass"]["change"]("p")
        handlers["login-connect"]["click"]()
        self.assertIn("add_server",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIn("Could not connect", self.b._login_error)
        self.assertEqual(self.b.route["kind"], "login")

    def test_login_success_loads_source(self):
        self.b.show_login()
        _n, handlers = build_scene(self.b)
        handlers["login-server"]["change"]("good")
        handlers["login-connect"]["click"]()
        # success -> rebuild source -> home
        self.assertEqual(self.b.route["kind"], "home")
        self.assertIsNone(self.b._login_error)


class TestLocked(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_locked_renders_pin_field(self):
        self.b.show_locked()
        self.assertEqual(self.b.route["kind"], "locked")
        nodes, _h = build_scene(self.b)
        self.assertIn("lock-pin", ids(nodes))
        self.assertIn("lock-unlock", ids(nodes))
        self.assertNotIn("nav-home", ids(nodes))   # chrome-free

    def test_wrong_pin_shows_error(self):
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("0000")
        handlers["lock-unlock"]["click"]()
        self.assertIn("Incorrect", self.b._pin_error)
        self.assertEqual(self.b.route["kind"], "locked")

    def test_correct_pin_unlocks_to_home(self):
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertEqual(self.b.route["kind"], "home")
        self.assertIsNone(self.b._pin_error)


class TestDownloadDialog(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_download_dialog_shows_estimate_and_enqueues(self):
        self.b._open_download({"Id": "m1", "Name": "Movie", "Type": "Movie"})
        nodes, handlers = build_scene(self.b)
        self.assertIn("dl-ok", ids(nodes))
        self.assertIn("dl-watched", ids(nodes))
        self.assertEqual(self.b._dl["est"]["count"], 3)   # estimate fetched
        handlers["dl-ok"]["click"]()
        self.assertIn("download_enqueue",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIsNone(self.b._dl)

    def test_download_include_watched_toggles(self):
        self.b._open_download({"Id": "m1", "Type": "Movie"})
        self.assertFalse(self.b._dl["watched"])
        self.b._dl_toggle_watched()
        self.assertTrue(self.b._dl["watched"])

    def test_download_cancel_clears_state(self):
        self.b._open_download({"Id": "m1", "Type": "Movie"})
        self.b._close_download()
        self.assertIsNone(self.b._dl)
        self.assertIsNone(self.b._dialog)

    def test_menu_download_opens_dialog(self):
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        self.b._menu_action(4, None)   # "Download"
        self.assertIsNone(self.b._menu)
        self.assertIsNotNone(self.b._dl)


class TestDialogs(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _dialog_nodes(self):
        return build_scene(self.b)

    def test_message_dialog(self):
        self.b._message("Hello there")
        nodes, handlers = self._dialog_nodes()
        self.assertTrue(any(n["t"] == "layer" and n.get("kind") == "modal"
                            for n in nodes) or "dlg-ok" in ids(nodes))
        handlers["dlg-ok"]["click"]()
        _n, _h = self._dialog_nodes()
        self.assertIsNone(self.b._dialog)

    def test_confirm_runs_callback_on_ok(self):
        done = []
        self.b._confirm("Sure?", lambda: done.append(1))
        _n, handlers = self._dialog_nodes()
        handlers["dlg-ok"]["click"]()
        self.assertEqual(done, [1])
        self.assertIsNone(self.b._dialog)

    def test_confirm_cancel_does_not_run(self):
        done = []
        self.b._confirm("Sure?", lambda: done.append(1))
        _n, handlers = self._dialog_nodes()
        handlers["dlg-cancel"]["click"]()
        self.assertEqual(done, [])
        self.assertIsNone(self.b._dialog)

    def test_syncplay_dialog_lists_and_joins(self):
        self.b._open_syncplay()      # sync pool -> groups fetched, dialog shown
        nodes, handlers = self._dialog_nodes()
        self.assertIn("sp-join-0", ids(nodes))
        self.assertIn("sp-new", ids(nodes))
        handlers["sp-join-0"]["click"]()
        self.assertIn("sync_join",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIsNone(self.b._dialog)   # closes on join


class TestQueueView(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_queue_renders_entries_with_current(self):
        self.b._open_queue()
        nodes, handlers = build_scene(self.b)
        self.assertEqual(self.b.route["kind"], "queue")
        self.assertIn("q-0", ids(nodes))
        self.assertTrue(any(k.startswith("q-rm-") for k in handlers))

    def test_queue_row_play_skips(self):
        self.b._open_queue()
        _n, handlers = build_scene(self.b)
        handlers["q-play-0"]["click"]()
        self.assertIn("skip_to", [c[0] for c in self.ctl.transport])

    def test_queue_reorder(self):
        self.b._open_queue()
        route = self.b.route
        first = route["_data"]["entries"][0]["pid"]
        self.b._queue_select(route, 0)
        self.b._queue_move(route, "down")
        self.assertEqual(route["_data"]["entries"][1]["pid"], first)
        self.assertIn("queue_reorder", [c[0] for c in self.ctl.transport])

    def test_queue_remove_calls_controller_and_refreshes(self):
        self.b._open_queue()
        _n, handlers = build_scene(self.b)
        handlers["q-rm-0"]["click"]()
        self.assertIn("queue_remove",
                      [c[0] for c in getattr(self.ctl, "transport", [])])


class MultiServerSource(FakeSource):
    def servers(self):
        return [{"uuid": "srv1", "name": "Home"},
                {"uuid": "srv2", "name": "Remote"}]


class TestServerSwitcher(unittest.TestCase):
    def test_switcher_shown_and_switches(self):
        b = MpvtkBrowser(app=None, source=MultiServerSource())
        b._pool = _SyncPool()
        nodes, handlers = build_scene(b)
        self.assertIn("nav-server", ids(nodes))
        handlers["nav-server"]["select"](1, "Remote")
        self.assertEqual(b.server, "srv2")
        self.assertEqual(b.route["kind"], "home")

    def test_switcher_hidden_for_single_server(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        nodes, _h = build_scene(b)
        self.assertNotIn("nav-server", ids(nodes))


class TestBanners(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def test_update_banner_shows_and_dismisses(self):
        self.b.notify_update("2.5.0", "http://example/rel")
        nodes, handlers = build_scene(self.b)
        self.assertIn("banner-open", ids(nodes))
        self.assertIn("banner-dismiss", ids(nodes))
        handlers["banner-dismiss"]["click"]()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-dismiss", ids(nodes))

    def test_update_open_calls_controller(self):
        self.b.notify_update("2.5.0", "http://example/rel")
        _n, handlers = build_scene(self.b)
        handlers["banner-open"]["click"]()
        self.assertIn("open_url",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_offline_banner_toggles(self):
        self.b.set_offline(True)
        nodes, _h = build_scene(self.b)
        self.assertIn("banner-retry", ids(nodes))
        self.b.set_offline(False)
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-retry", ids(nodes))


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

    def test_bar_controls_seek_volume_repeat_favorite(self):
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 100, "volume": 50})
        nodes, h = build_scene(self.b)
        for nid in ("np-seek", "np-vol", "np-repeat", "np-fav"):
            self.assertIn(nid, ids(nodes))
        h["np-seek"]["change"](42)
        h["np-vol"]["change"](30)
        h["np-repeat"]["click"]()
        h["np-fav"]["click"]()
        names = [c[0] for c in getattr(self.ctl, "transport", [])]
        for n in ("seek", "set_volume", "set_repeat", "toggle_favorite"):
            self.assertIn(n, names)

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
