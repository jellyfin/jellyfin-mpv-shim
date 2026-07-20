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


class HudController(FakeController):
    """FakeController with real HUD data (the catch-all recorder would
    return None for the data getters)."""

    def __init__(self):
        super().__init__()
        self.menu_state = {
            "has_media": True,
            "audio": [
                {"id": 1, "label": "English", "selected": True},
                {"id": 2, "label": "Commentary", "selected": False},
            ],
            "subtitles": [
                {"id": -1, "label": "None", "selected": True},
                {"id": 3, "label": "English", "selected": False},
            ],
            "quality": {"current": "No Transcode", "options": [
                {"id": "none", "label": "No Transcode", "selected": True},
                {"id": 20, "label": "20 Mbps", "selected": False},
            ]},
            "profiles": {"current": "None (Disabled)", "options": [
                {"id": "none", "label": "None (Disabled)",
                 "selected": True},
                {"id": "anime4k", "label": "Anime4K", "selected": False},
            ]},
            "sub_style": {
                key: {"current": "Default", "options": [
                    {"id": 0, "label": "Default", "selected": True},
                ]} for key in ("size", "position", "color")
            },
            "syncplay": {"enabled": False, "current": "None (Disabled)",
                         "groups": []},
            "allow_screenshot": True,
        }
        self.chapter_list = [
            {"title": "Opening", "time": 0.0},
            {"title": "Middle", "time": 40.0},
            {"title": "End", "time": 80.0},
        ]

    def use_hud(self):
        return True

    def hud_menu_state(self):
        return self.menu_state

    def chapters(self):
        return list(self.chapter_list)


class TestPlaybackHudLayout(unittest.TestCase):
    """Viewport tiers + chapter slits of the playback HUD bar (hud.py),
    laid out headlessly. The lifecycle itself is covered on a real mpv
    in tests/integration/test_mpvtk_hud.py."""

    def _browser(self):
        ctl = HudController()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._browsing = False
        b._hud_shown = True
        b._hud_state = {"stopped": False, "is_audio": False,
                        "title": "Movie", "position": 50.0,
                        "duration": 100.0, "paused": False}
        return b, ctl

    def test_wide_viewport_has_all_controls_and_marks(self):
        b, _ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        present = ids(nodes)
        for nid in ("hud-pp", "hud-seek-back", "hud-seek-fwd",
                    "hud-ch-prev", "hud-ch-next", "hud-chapters",
                    "hud-audio", "hud-sub", "hud-quality",
                    "hud-mute", "hud-vol", "hud-fs", "hud-clock"):
            self.assertIn(nid, present)
        self.assertTrue(any("Ends at" in (n.get("text") or "")
                            for n in nodes), "ends-at label missing")
        seek = next(n for n in nodes if n.get("id") == "hud-seek")
        self.assertEqual(seek.get("marks"), [0.4, 0.8],
                         "chapter slits should be the interior chapters")

    def test_narrow_viewport_drops_optional_controls(self):
        b, _ctl = self._browser()
        nodes, _h = build_scene(b, (460, 640))
        present = ids(nodes)
        for nid in ("hud-pp", "hud-prev", "hud-next",
                    "hud-audio", "hud-sub", "hud-mute", "hud-fs"):
            self.assertIn(nid, present)
        for nid in ("hud-seek-back", "hud-seek-fwd", "hud-ch-prev",
                    "hud-ch-next", "hud-chapters", "hud-quality",
                    "hud-vol", "hud-clock"):
            self.assertNotIn(nid, present)
        self.assertFalse(any("Ends at" in (n.get("text") or "")
                             for n in nodes),
                         "ends-at must drop below 1000px")

    def test_volume_mute_fullscreen_and_clock_toggle(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-mute"]["click"]()
        handlers["hud-vol"]["change"](30)
        handlers["hud-fs"]["click"]()
        names = [c[0] for c in ctl.transport]
        for n in ("toggle_mute", "set_volume", "toggle_fullscreen"):
            self.assertIn(n, names)
        # clock click flips total -> negative remaining
        clock = next(n for n in nodes
                     if (n.get("text") or "").startswith("0:50 / "))
        self.assertIn("1:40", clock["text"])
        handlers["hud-clock"]["click"]()
        self.assertTrue(b._hud_tc_remaining)
        nodes, _h = build_scene(b, (1280, 720))
        self.assertTrue(any((n.get("text") or "") == "0:50 / -0:50"
                            for n in nodes),
                        "remaining-time clock missing")

    def test_seek_bar_range_shading(self):
        b, _ctl = self._browser()
        b._hud_state["ranges"] = [[10.0, 40.0], [90.0, 100.0]]
        nodes, _h = build_scene(b, (1280, 720))
        seek = next(n for n in nodes if n.get("id") == "hud-seek")
        self.assertEqual(seek.get("ranges"), [[0.1, 0.4], [0.9, 1.0]])
        self.assertTrue(seek.get("hoverev"),
                        "seek bar must opt into hover events")

    def test_hover_bubble_shows_chapter_and_time(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertNotIn("hud-preview", ids(nodes))
        # need a laid-out slider rect for the float: fake node_rect via
        # a stub app that serves the previous scene's geometry
        seek = next(n for n in nodes if n.get("id") == "hud-seek")

        class GeoApp(StubHudApp):
            def node_rect(self, node_id):
                return seek if node_id == "hud-seek" else None

            def invalidate(self):
                pass

        b.app = GeoApp()
        handlers["hud-seek"]["hover"](45.0)
        self.assertEqual(b._hud_hover, 45.0)
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertIn("hud-preview", ids(nodes))
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("0:45", texts, "bubble timestamp missing")
        self.assertIn("Middle", texts,
                      "bubble chapter name missing (45s is in Middle)")
        handlers["hud-seek"]["hover_end"]()
        self.assertIsNone(b._hud_hover)
        nodes, _h = build_scene(b, (1280, 720))
        self.assertNotIn("hud-preview", ids(nodes))

    def test_hud_show_hide_adjusts_sub_margin(self):
        b, ctl = self._browser()
        b._on_hud(True)
        b._on_hud(False)
        self.assertIn(("hud_sub_margin", (True,)), ctl.transport)
        self.assertIn(("hud_sub_margin", (False,)), ctl.transport)

    def test_mid_viewport_keeps_quality_drops_chapters(self):
        b, _ctl = self._browser()
        nodes, _h = build_scene(b, (620, 640))
        present = ids(nodes)
        self.assertIn("hud-quality", present)
        self.assertIn("hud-seek-back", present)
        self.assertNotIn("hud-chapters", present)
        self.assertNotIn("hud-ch-prev", present)

    def test_seek_step_buttons_seek_relative(self):
        b, ctl = self._browser()
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-seek-back"]["click"]()
        handlers["hud-seek-fwd"]["click"]()
        self.assertIn(("seek_relative", (-10,)), ctl.transport)
        self.assertIn(("seek_relative", (30,)), ctl.transport)

    def test_chapter_jump_buttons(self):
        b, ctl = self._browser()
        _nodes, handlers = build_scene(b, (1280, 720))
        # pos=50: prev -> chapter at 40, next -> chapter at 80
        handlers["hud-ch-prev"]["click"]()
        handlers["hud-ch-next"]["click"]()
        self.assertIn(("seek", (40.0,)), ctl.transport)
        self.assertIn(("seek", (80.0,)), ctl.transport)

    def test_prev_chapter_within_grace_goes_further_back(self):
        b, ctl = self._browser()
        b._hud_state["position"] = 41.0  # within 2s of the 40s chapter
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-ch-prev"]["click"]()
        self.assertIn(("seek", (0.0,)), ctl.transport)


class StubHudApp:
    """Records the renderer-facing calls the HUD lifecycle makes."""

    def __init__(self):
        self.calls = []
        self.on_nav = None
        self.on_hud = None
        self.on_hud_skip = None

    def invalidate(self):
        pass

    def set_active(self, active):
        self.calls.append(("active", active))

    def set_hud(self, on, opts=None):
        self.calls.append(("hud", on))
        self.hud_opts = opts

    def set_hud_skip(self, label):
        self.calls.append(("skip", label))


class TestPlaybackHudMenusAndFavorite(unittest.TestCase):
    def _browser(self, size=(1280, 720)):
        ctl = HudController()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._browsing = False
        b._hud_shown = True
        b._hud_state = {"stopped": False, "is_audio": False,
                        "title": "Movie", "position": 50.0,
                        "duration": 100.0, "paused": False,
                        "favorite": False}
        return b, ctl

    def test_favorite_button_toggles(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertIn("hud-fav", ids(nodes))
        handlers["hud-fav"]["click"]()
        self.assertIn(("hud_action", ("toggle-favorite", None)),
                      ctl.transport)
        self.assertTrue(b._hud_state["favorite"], "optimistic flip")
        nodes, _h = build_scene(b, (460, 640))
        self.assertNotIn("hud-fav", ids(nodes),
                         "favorite hides below 560px")

    def test_settings_menu_root_and_speed_flow(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertIn("hud-settings", ids(nodes))
        self.assertNotIn("hud-menu", ids(nodes))
        handlers["hud-settings"]["click"]()
        self.assertEqual(b._hud_menu, "root")
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        labels = menu["items"]
        # parity with the lua gear sheet (+ SyncPlay, which the lua
        # keeps on its top bar the HUD doesn't have)
        for want in ("Quality", "Speed", "Aspect", "Profile",
                     "Subtitle Size", "Subtitle Position",
                     "Subtitle Color", "SyncPlay", "Playback Data",
                     "Screenshot", "Unwatched"):
            self.assertTrue(any(want.lower() in l.lower()
                                for l in labels),
                            "missing %r in %r" % (want, labels))
        idx = next(i for i, l in enumerate(labels)
                   if "Playback Speed" in l)
        handlers["hud-menu"]["select"](idx, labels[idx])
        self.assertEqual(b._hud_menu, "speed")
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        # controller has no real speed -> default 1.0 gets the check
        # (layout resolves icon names to path data; presence is enough)
        self.assertTrue(menu["icons"][menu["items"].index("1x")])
        self.assertFalse(menu["icons"][menu["items"].index("0.5x")])
        two = menu["items"].index("2x")
        handlers["hud-menu"]["select"](two, "2x")
        self.assertIn(("set_speed", (2.0,)), ctl.transport)
        self.assertIsNone(b._hud_menu, "leaf selection closes the menu")

    def test_settings_menu_back_and_dismiss(self):
        b, _ctl = self._browser()
        b._hud_menu = "aspect"
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertEqual(menu["items"][0], "Back")
        handlers["hud-menu"]["select"](0, "Back")
        self.assertEqual(b._hud_menu, "root")
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-menu"]["dismiss"]()
        self.assertIsNone(b._hud_menu)

    def test_top_bar_back_title_syncplay(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        present = ids(nodes)
        self.assertIn("hud-back", present)
        self.assertIn("hud-syncplay", present)
        # the title renders in the top header row, in the top strip
        title = next(n for n in nodes
                     if n.get("text") == "Movie" and n.get("y", 999) < 80)
        self.assertLess(title["y"], 80)
        # back yields to the library (stop_to_browser via controller)
        handlers["hud-back"]["click"]()
        self.assertIn(("stop", ()), ctl.transport)
        # the top SyncPlay button opens its sheet standalone: no Back
        # row, anchored at the button
        handlers["hud-syncplay"]["click"]()
        self.assertEqual(b._hud_menu, "syncplay")
        self.assertEqual(b._hud_menu_anchor, "hud-syncplay")
        nodes, _h = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertNotIn("Back", menu["items"])
        self.assertIn("None (Disabled)", menu["items"])
        # ... while the same sheet from the gear keeps its Back row
        b._hud_menu = None
        b._hud_menu_anchor = "hud-settings"
        b._hud_menu = "syncplay"
        nodes, _h = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertEqual(menu["items"][0], "Back")

    def test_no_syncplay_button_without_syncplay_state(self):
        b, ctl = self._browser()
        ctl.menu_state.pop("syncplay")
        nodes, _h = build_scene(b, (1280, 720))
        self.assertIn("hud-back", ids(nodes))
        self.assertNotIn("hud-syncplay", ids(nodes))

    def test_sub_style_submenu_routes_verb(self):
        b, ctl = self._browser()
        b._hud_menu = "sub_size"
        ctl.menu_state["sub_style"] = {"size": {
            "current": "Normal",
            "options": [{"id": 0, "label": "Small", "selected": False},
                        {"id": 1, "label": "Normal", "selected": True}],
        }}
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        idx = menu["items"].index("Small")
        handlers["hud-menu"]["select"](idx, "Small")
        self.assertIn(("hud_action", ("set-sub-size", 0)), ctl.transport)
        # a group the state blob doesn't carry renders only the Back row
        b._hud_menu = "sub_color"
        ctl.menu_state.pop("sub_style")
        nodes, _h = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertEqual(menu["items"], ["Back"])


class TestHudLifecycleWiring(unittest.TestCase):
    def test_set_app_rewires_callbacks(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b._hud_shown = True
        b.set_app(app)
        self.assertEqual(app.on_nav, b._on_nav_mode)
        self.assertEqual(app.on_hud, b._on_hud)
        self.assertEqual(app.on_hud_skip, b._on_hud_skip)
        self.assertFalse(b._hud_shown,
                         "a fresh renderer has no summoned HUD")

    def test_reassert_window_state(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.set_app(app)
        b._browsing = True
        b.reassert_window_state()
        self.assertEqual(app.calls[-1], ("active", True))
        b._browsing = False
        b._hud_state = {"stopped": False}
        b.reassert_window_state()
        self.assertEqual(app.calls[-1], ("hud", True),
                         "video in flight re-enters HUD mode")
        b._hud_state = None
        b.reassert_window_state()
        self.assertEqual(app.calls[-1], ("active", False))

    def test_video_playstate_engages_hud_when_already_yielded(self):
        """Playback that starts while minimized/yielded (cast, crash
        recovery) must still enter HUD mode — _yield only runs on the
        browsing -> video transition."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.set_app(app)
        b._browsing = False
        b.on_playstate({"stopped": False, "is_audio": False,
                        "title": "M", "position": 1.0,
                        "duration": 100.0, "paused": False,
                        "skip_label": "Skip Intro"})
        self.assertIn(("hud", True), app.calls)
        self.assertIn(("skip", "Skip Intro"), app.calls)


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

    def test_detaching_frees_the_tile_cache(self):
        """mpv going away (idle-quit) must drop the composited bitmaps: on
        libmpv they are in-process buffers the dead mpv read by address, so
        holding them leaks the memory the quit was meant to free."""
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=self.ctl)
        b.route["_data"] = {"libraries": b.source.libraries, "rows": []}
        build_scene(b)
        self.assertGreater(len(b.strips._cache), 0)
        b.strips.clear()
        self.assertEqual(len(b.strips._cache), 0)

    def test_app_can_be_swapped_and_rebuilt(self):
        """After an idle-quit the browser is pointed at a new MpvtkApp; its
        route stack and data survive, and invalidate() is a no-op while
        detached rather than a crash."""
        class FakeApp:
            def __init__(self):
                self.invalidated = 0

            def invalidate(self):
                self.invalidated += 1

            def set_active(self, on):
                pass

        b = MpvtkBrowser(app=FakeApp(), source=FakeSource(),
                         controller=self.ctl)
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        stack = list(b.nav_stack)

        b.app = None            # detached: mpv is gone
        b.invalidate()          # must not raise

        b.app = FakeApp()       # re-attached to the new handle
        b.invalidate()
        self.assertEqual(b.app.invalidated, 1)
        self.assertEqual(b.nav_stack, stack)

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

    def test_setting_note_renders_under_its_setting(self):
        """A note explains the default for the settings that follow it,
        so it has to land between them, not at the end of the group."""
        self.cfg.NOTES = {"osc_mode": "MPV keybinds are used by default."}
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("MPV keybinds are used by default.", texts)
        self.assertLess(texts.index("Osc Mode"),
                        texts.index("MPV keybinds are used by default."))
        self.assertLess(texts.index("MPV keybinds are used by default."),
                        texts.index("Lang"))

    def test_hud_key_settings_sit_under_the_keybind_note(self):
        """The real schema: the two HUD keyboard settings are curated
        into Interface directly below the note explaining the default,
        not buried in the auto-generated Advanced list."""
        from jellyfin_mpv_shim.mpvtk_browser import config as real

        interface = dict(real.sections())["Interface"]
        self.assertEqual(
            interface[interface.index("osc_style"):][:3],
            ["osc_style", "hud_grab_keys", "hud_wake_key"])
        self.assertIn("osc_style", real.NOTES)
        advanced = dict(real.sections()).get("Advanced", [])
        self.assertNotIn("hud_grab_keys", advanced)
        self.assertNotIn("hud_wake_key", advanced)

    def test_settings_without_notes_still_render(self):
        """NOTES is optional — a config object without it must not blow
        up the whole Settings view."""
        self.assertFalse(hasattr(self.cfg, "NOTES"))
        self.b._open_settings()
        nodes, _h = build_scene(self.b)
        self.assertIn("set-player_name", ids(nodes))

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
        # One library fits, so no arrows; a long row gets them, floating over
        # the strip's left and right edges.
        self.b.route["_data"] = {"libraries": self.b.source.libraries,
                                 "rows": []}
        nodes, _h = build_scene(self.b)
        self.assertNotIn("row-libs-pl", ids(nodes))

        many = [dict(self.b.source.libraries[0], Id="lib%d" % i,
                     Name="Library %d" % i) for i in range(30)]
        self.b.route["_data"] = {"libraries": many, "rows": []}
        nodes, _h = build_scene(self.b)
        by_id = {n["id"]: n for n in nodes}
        self.assertIn("row-libs-pl", by_id)
        self.assertIn("row-libs-pr", by_id)
        strip = by_id["row-libs"]
        left, right = by_id["row-libs-pl"], by_id["row-libs-pr"]
        pad = self.b.RING_PAD
        # Inset from the scroll container's edges by the ring padding.
        self.assertAlmostEqual(left["x"], strip["x"] + pad, places=1)
        self.assertAlmostEqual(right["x"] + right["w"],
                               strip["x"] + strip["w"] - pad, places=1)
        # Square, and small enough to cover little artwork.
        self.assertEqual(left["w"], left["h"])
        self.assertLess(left["h"], strip["h"] / 2)

    def test_arrows_punch_through_the_strip_bitmap(self):
        """An ASS button can't composite over a bitmap; it needs an occluder
        node so the renderer subtracts its rect from the strip below."""
        many = [dict(self.b.source.libraries[0], Id="lib%d" % i,
                     Name="Library %d" % i) for i in range(30)]
        self.b.route["_data"] = {"libraries": many, "rows": []}
        nodes, _h = build_scene(self.b)
        occ = [n for n in nodes if n["t"] == "occ"]
        self.assertEqual(len(occ), 2, "one occluder per arrow")

    def test_arrows_hold_repeat(self):
        many = [dict(self.b.source.libraries[0], Id="lib%d" % i,
                     Name="Library %d" % i) for i in range(30)]
        self.b.route["_data"] = {"libraries": many, "rows": []}
        nodes, _h = build_scene(self.b)
        by_id = {n["id"]: n for n in nodes}
        self.assertTrue(by_id["row-libs-pl"].get("rpt"))
        self.assertTrue(by_id["row-libs-pr"].get("rpt"))

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

    def test_plain_click_selects_only_that_row(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]({})
        h["pe-row-2"]["click"]({})
        self.assertEqual(self.b.route["_sel"], {2})

    def test_shift_click_selects_the_range_in_two_clicks(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]({})
        h["pe-row-2"]["click"]({"shift": True})
        self.assertEqual(self.b.route["_sel"], {0, 1, 2})

    def test_shift_click_works_upwards_too(self):
        nodes, h = self._open_edit()
        h["pe-row-2"]["click"]({})
        h["pe-row-0"]["click"]({"shift": True})
        self.assertEqual(self.b.route["_sel"], {0, 1, 2})

    def test_ctrl_click_toggles_additively(self):
        nodes, h = self._open_edit()
        h["pe-row-0"]["click"]({})
        h["pe-row-2"]["click"]({"ctrl": True})
        self.assertEqual(self.b.route["_sel"], {0, 2})
        h["pe-row-0"]["click"]({"ctrl": True})
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

    def test_correct_pin_with_no_source_is_not_a_bad_pin(self):
        """work_offline (or an unreachable server) leaves nothing to build a
        source from. Reporting that as a wrong PIN locked the client out for
        good — the PIN was right, so land on login instead."""
        self.ctl.connect_and_rebuild = lambda: None
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertIsNone(self.b._pin_error)
        self.assertEqual(self.b.route["kind"], "login")
        self.assertFalse(self.b._locked)

    def test_relock_gates_the_ui_again_on_reopen(self):
        """Unlocking covers that reopen, not the rest of the process's life:
        closing to the tray and re-raising has to re-prompt."""
        self.ctl.needs_unlock = lambda: True
        self.b.show_locked()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("1234")
        handlers["lock-unlock"]["click"]()
        self.assertEqual(self.b.route["kind"], "home")

        self.b.maybe_relock()
        self.assertEqual(self.b.route["kind"], "locked")
        self.assertTrue(self.b._locked)

    def test_relock_is_a_noop_without_a_startup_pin(self):
        self.ctl.needs_unlock = lambda: False
        self.b.maybe_relock()
        self.assertNotEqual(self.b.route["kind"], "locked")

    def test_relocking_twice_keeps_a_half_typed_pin(self):
        """The tray can fire show/hide at any moment; a second relock must
        not reset the gate under the user's fingers."""
        self.ctl.needs_unlock = lambda: True
        self.b.maybe_relock()
        _n, handlers = build_scene(self.b)
        handlers["lock-pin"]["change"]("12")
        self.b.maybe_relock()
        self.assertEqual(self.b._pin["pin"], "12")

    def test_tray_settings_cannot_bypass_the_gate(self):
        """Configure Servers / Show Console route straight to Settings — the
        logs and server list are behind the PIN too."""
        self.b.show_locked()
        self.b.open_settings("logs")
        self.assertEqual(self.b.route["kind"], "locked")

    def test_remote_display_content_cannot_bypass_the_gate(self):
        self.b.show_locked()
        self.b.display_item("s1", "item-1")
        self.assertEqual(self.b.route["kind"], "locked")


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
        # Same toolbar-driven shape as the playlist editor.
        for nid in ("q-top", "q-up", "q-down", "q-bottom", "q-remove"):
            self.assertIn(nid, ids(nodes))

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

    def test_queue_shift_select_then_block_move(self):
        self.b._open_queue()
        route = self.b.route
        pids = [e["pid"] for e in route["_data"]["entries"]]
        _n, h = build_scene(self.b)
        h["q-0"]["click"]({})
        h["q-1"]["click"]({"shift": True})
        self.assertEqual(route["_sel"], {0, 1})
        self.b._queue_move(route, "bottom")
        self.assertEqual([e["pid"] for e in route["_data"]["entries"]],
                         [pids[2], pids[0], pids[1]])

    def test_queue_remove_calls_controller_and_refreshes(self):
        self.b._open_queue()
        route = self.b.route
        _n, h = build_scene(self.b)
        h["q-0"]["click"]({})
        h["q-remove"]["click"]()
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
        # seek is commit-only (fires when the drag gesture ends); volume
        # stays live on change
        h["np-seek"]["commit"](42)
        self.assertNotIn("change", h["np-seek"],
                         "np-seek must not live-seek while dragging")
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


class DownloadsController(FakeController):
    """Controller whose downloads catalog has real hierarchy."""

    TREE = [
        {"kind": "playlist", "id": "PL9", "title": "Road Trip", "size": 9000,
         "count": 120, "children": []},
        {"kind": "series", "id": "sh1", "title": "The Show", "size": 3000,
         "count": 2,
         "children": [
             {"kind": "season", "id": "se1", "series_id": "sh1",
              "title": "Season 1", "size": 3000, "count": 2, "children": [
                  {"kind": "item", "id": "e1", "title": "Pilot",
                   "status": "complete", "size": 2000, "index": 1},
                  {"kind": "item", "id": "e2", "title": "Second",
                   "status": "pending", "size": 1000, "index": 2}]}]},
        {"kind": "movies", "id": None, "title": "Movies & Videos", "size": 500,
         "count": 1,
         "children": [{"kind": "item", "id": "m1", "title": "A Movie",
                       "status": "complete", "size": 500, "index": None}]},
    ]

    def __init__(self):
        super().__init__()
        self.deleted = []

    def list_downloads(self):
        import copy
        return copy.deepcopy(self.TREE)

    def delete_download(self, item_id=None, series_id=None, season_id=None,
                        playlist_id=None):
        self.deleted.append((item_id, series_id, season_id, playlist_id))

    def download_activity(self):
        return (0, 3)

    def list_users(self):
        return [{"id": "u1", "name": "Izzie", "locked": False, "active": True},
                {"id": "u2", "name": "Guest", "locked": True, "active": False}]

    def list_servers(self):
        return [{"uuid": "srv1", "name": "Home", "address": "http://h",
                 "username": "izzie", "connected": True},
                {"uuid": "srv2", "name": "Away", "address": "http://a",
                 "username": "izzie", "connected": False}]


class TestDownloadsPanel(unittest.TestCase):
    def setUp(self):
        self.ctl = DownloadsController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()
        self.b.open_settings("downloads")
        # First build kicks off the (inline) catalog load and shows a
        # spinner; the second renders the tree.
        build_scene(self.b)

    def test_tree_is_indented_by_level(self):
        """Series > season > episode each start further right."""
        nodes, _h = build_scene(self.b)
        text_x = {n["text"]: n["x"] for n in nodes if n["t"] == "text"}
        self.assertIn("The Show", text_x)
        self.assertIn("Season 1", text_x)
        self.assertIn("1. Pilot", text_x)
        self.assertLess(text_x["The Show"], text_x["Season 1"])
        self.assertLess(text_x["Season 1"], text_x["1. Pilot"])
        self.assertEqual(text_x["Season 1"] - text_x["The Show"],
                         self.b.INDENT)

    def test_every_level_can_be_deleted(self):
        _n, h = build_scene(self.b)
        for nid in ("dl-g1-rm", "dl-g1-s0-rm", "dl-g1-s0-e0-rm"):
            self.assertIn(nid, h, nid)

    def test_deleting_a_series_passes_series_id(self):
        _n, h = build_scene(self.b)
        h["dl-g1-rm"]["click"]()          # opens the confirm dialog
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.ctl.deleted, [(None, "sh1", None, None)])

    def test_deleting_an_episode_passes_item_id(self):
        _n, h = build_scene(self.b)
        h["dl-g1-s0-e0-rm"]["click"]()
        _n, h = build_scene(self.b)
        h["dlg-ok"]["click"]()
        self.assertEqual(self.ctl.deleted, [("e1", None, None, None)])

    def test_loose_movies_group_renders(self):
        """Items with no series land in one flat group at the end."""
        nodes, _h = build_scene(self.b)
        texts = [n["text"] for n in nodes if n["t"] == "text"]
        self.assertIn("Movies & Videos", texts)
        self.assertIn("A Movie", texts)
        self.assertIn("dl-g2-i0-rm", ids(nodes))

    def test_pending_items_show_their_status(self):
        nodes, _h = build_scene(self.b)
        texts = [n["text"] for n in nodes if n["t"] == "text"]
        self.assertTrue(any("pending" in t for t in texts))
        # A completed item shows only its size, not "complete".
        self.assertFalse(any("complete" in t for t in texts))


class TestServersPanel(unittest.TestCase):
    def setUp(self):
        self.ctl = DownloadsController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()
        self.b.open_settings("servers")

    def test_users_and_servers_both_render(self):
        nodes, _h = build_scene(self.b)
        self.assertIn("su-0", ids(nodes))
        self.assertIn("su-1", ids(nodes))
        self.assertIn("sv-0", ids(nodes))
        self.assertIn("sv-1", ids(nodes))

    def test_sections_span_the_pane(self):
        """Settings panels are forms; their cards should fill the width
        rather than shrink to their content."""
        nodes, _h = build_scene(self.b, size=(1280, 720))
        cards = [n for n in nodes if n["t"] == "rect" and n["w"] > 900]
        self.assertGreaterEqual(len(cards), 2, "expected two full-width cards")

    def test_locked_user_gets_the_lock_glyph(self):
        nodes, _h = build_scene(self.b)
        icons = [n for n in nodes if n["t"] == "icon"]
        self.assertTrue(icons)


class TestChromePolish(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=MultiServerSource())
        self.b._pool = _SyncPool()

    def _labels(self, size):
        nodes, _h = build_scene(self.b, size=size)
        return {n["text"] for n in nodes if n["t"] == "text"}

    def test_wide_top_bar_is_labelled(self):
        labels = self._labels((1920, 900))
        self.assertIn("Settings", labels)
        self.assertIn("SyncPlay", labels)

    def test_narrow_top_bar_drops_to_icons(self):
        """The bar collapses when the labelled version genuinely doesn't fit
        (measured, not a width threshold)."""
        labels = self._labels((760, 900))
        self.assertNotIn("SyncPlay", labels)
        self.assertNotIn("Settings", labels)
        nodes, _h = build_scene(self.b, size=(760, 900))
        # The buttons are still there, just icon-only.
        self.assertIn("nav-settings", ids(nodes))
        self.assertIn("nav-syncplay", ids(nodes))

    def test_collapse_depends_on_what_is_in_the_bar(self):
        """The bar collapses when its contents don't fit, not at a fixed
        width: adding the user switcher pushes it over sooner. A width
        constant can't express that."""
        class Users(FakeController):
            def list_users(self):
                return [{"id": "u1", "name": "Izzie", "locked": False,
                         "active": True},
                        {"id": "u2", "name": "Guest", "locked": True,
                         "active": False}]

        at = 1160, 900
        self.assertIn("Settings", self._labels(at))       # no switcher

        self.b = MpvtkBrowser(app=None, source=MultiServerSource(),
                              controller=Users())
        self.b._pool = _SyncPool()
        self.assertNotIn("Settings", self._labels(at))    # switcher present

    def test_top_bar_never_overflows_the_window(self):
        for w in (900, 1100, 1279, 1280, 1920):
            nodes, _h = build_scene(self.b, size=(w, 800))
            bar = [n for n in nodes if n.get("id") == "nav-settings"][0]
            self.assertLessEqual(bar["x"] + bar["w"], w,
                                 "top bar overflows at %dpx" % w)


class TestButtonColors(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def _accent_button_texts(self, nodes):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        accent = {n["id"] for n in nodes
                  if n["t"] == "rect" and n.get("fill") == theme.ACCENT}
        out = []
        for n in nodes:
            if n["t"] != "text":
                continue
            for a in accent:
                rect = next(r for r in nodes if r.get("id") == a)
                if (rect["x"] <= n["x"] <= rect["x"] + rect["w"]
                        and rect["y"] <= n["y"] <= rect["y"] + rect["h"]):
                    out.append(n)
        return out

    def test_accent_buttons_use_white_text(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        self.b.navigate({"kind": "series", "server": "srv1",
                         "item_id": "sh1", "title": "Show"})
        nodes, _h = build_scene(self.b)
        texts = self._accent_button_texts(nodes)
        self.assertTrue(texts, "expected at least one accent button")
        for n in texts:
            self.assertEqual(n["c"], theme.ACCENT_FG,
                             "%r should be white on blue" % n["text"])

    def test_next_up_is_a_primary_action(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        self.b.navigate({"kind": "series", "server": "srv1",
                         "item_id": "sh1", "title": "Show"})
        nodes, _h = build_scene(self.b)
        btn = [n for n in nodes if n.get("id") == "sa-nextup"][0]
        self.assertEqual(btn.get("fill"), theme.ACCENT)


class TestBanner(unittest.TestCase):
    def test_banner_is_two_thirds_of_a_16_9_box(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        bw, bh = b._banner_box(1280)
        self.assertAlmostEqual(bh / bw, 9 / 16 * 2 / 3, places=3)

    def test_heading_is_baked_into_the_banner(self):
        """Text over artwork has to be part of the bitmap — ASS would render
        underneath it."""
        from PIL import Image as PILImage
        b = MpvtkBrowser(app=None, source=FakeSource())
        art = PILImage.new("RGB", (800, 800), (40, 40, 40))
        plain = b._compose_banner(art, (600, 225))
        titled = b._compose_banner(art, (600, 225), title="The Show",
                                   meta="2020 · 45 min")
        self.assertEqual(titled.size, (600, 225))
        self.assertNotEqual(plain.tobytes(), titled.tobytes())


class TestDownloadsGrouping(unittest.TestCase):
    """The controller's grouping is where the 0 B / music-spam problems were,
    so exercise it against a fake catalog rather than only the view."""

    def _controller(self, rows, playlists=(), owned=None):
        from jellyfin_mpv_shim.mpvtk_browser.ui import _PlayerController

        class FakeDB:
            def list(self_inner):
                return list(rows)

            def list_playlists(self_inner):
                return list(playlists)

            def playlist_item_rows(self_inner, pid):
                return [r for r in rows if r.get("_pl") == pid]

            def playlist_ownership(self_inner):
                return dict(owned or {})

        class FakeSync:
            db = FakeDB()

        import jellyfin_mpv_shim.sync.manager as mgr
        real, mgr.syncManager = mgr.syncManager, FakeSync()
        self.addCleanup(lambda: setattr(mgr, "syncManager", real))
        return _PlayerController()

    def test_size_comes_from_the_real_columns(self):
        """The catalog stores size_bytes/downloaded_bytes; reading a "size"
        key showed 0 B for everything."""
        ctl = self._controller([
            {"item_id": "m1", "name": "A Movie", "status": "complete",
             "downloaded_bytes": 1024 * 1024, "size_bytes": 2 * 1024 * 1024},
        ])
        groups = ctl.list_downloads()
        self.assertEqual(groups[0]["size"], 1024 * 1024)

    def test_falls_back_to_expected_size_before_download_starts(self):
        ctl = self._controller([
            {"item_id": "m1", "name": "Queued", "status": "pending",
             "downloaded_bytes": 0, "size_bytes": 4096},
        ])
        self.assertEqual(ctl.list_downloads()[0]["size"], 4096)

    def test_playlists_are_collapsed_and_own_their_items(self):
        rows = [{"item_id": "t%d" % i, "name": "Track %d" % i,
                 "type": "Audio", "status": "complete",
                 "downloaded_bytes": 100, "_pl": "PL1"} for i in range(200)]
        ctl = self._controller(
            rows, playlists=[{"playlist_id": "PL1", "name": "Road Trip"}],
            owned={r["item_id"]: "PL1" for r in rows})
        groups = ctl.list_downloads()
        self.assertEqual(len(groups), 1, "tracks must not also list loose")
        pl = groups[0]
        self.assertEqual(pl["kind"], "playlist")
        self.assertEqual(pl["count"], 200)
        self.assertEqual(pl["size"], 200 * 100)
        self.assertEqual(pl["children"], [], "collapsed, not 200 rows")

    def test_video_playlists_list_their_items(self):
        """A playlist of films is a handful of rows, and the whole point of
        having it in the manager is removing one of them."""
        rows = [{"item_id": "m1", "name": "First", "type": "Movie",
                 "status": "complete", "downloaded_bytes": 100, "_pl": "PL1"},
                {"item_id": "m2", "name": "Second", "type": "Video",
                 "status": "complete", "downloaded_bytes": 200, "_pl": "PL1"}]
        ctl = self._controller(
            rows, playlists=[{"playlist_id": "PL1", "name": "Movie Night"}],
            owned={r["item_id"]: "PL1" for r in rows})
        groups = ctl.list_downloads()
        self.assertEqual(len(groups), 1, "items must not also list loose")
        pl = groups[0]
        self.assertEqual(pl["count"], 2)
        self.assertEqual([c["title"] for c in pl["children"]],
                         ["First", "Second"])
        self.assertEqual([c["id"] for c in pl["children"]], ["m1", "m2"])

    def test_mixed_and_untyped_playlists_stay_collapsed(self):
        """One video among the tracks doesn't make it a video playlist, and
        a row with no type must not be guessed into one."""
        mixed = [{"item_id": "a1", "name": "Track", "type": "Audio",
                  "status": "complete", "downloaded_bytes": 1, "_pl": "PL1"},
                 {"item_id": "v1", "name": "Clip", "type": "Video",
                  "status": "complete", "downloaded_bytes": 1, "_pl": "PL1"}]
        ctl = self._controller(
            mixed, playlists=[{"playlist_id": "PL1", "name": "Mixed"}],
            owned={r["item_id"]: "PL1" for r in mixed})
        self.assertEqual(ctl.list_downloads()[0]["children"], [])

        untyped = [{"item_id": "u1", "name": "?", "status": "complete",
                    "downloaded_bytes": 1, "_pl": "PL2"}]
        ctl = self._controller(
            untyped, playlists=[{"playlist_id": "PL2", "name": "Old"}],
            owned={r["item_id"]: "PL2" for r in untyped})
        self.assertEqual(ctl.list_downloads()[0]["children"], [])

    def test_series_nest_seasons_and_episodes(self):
        ctl = self._controller([
            {"item_id": "e1", "name": "Pilot", "series_id": "sh1",
             "series_name": "Show", "season_id": "s1", "parent_index": 1,
             "index_number": 1, "downloaded_bytes": 10, "status": "complete"},
            {"item_id": "e2", "name": "Two", "series_id": "sh1",
             "series_name": "Show", "season_id": "s1", "parent_index": 1,
             "index_number": 2, "downloaded_bytes": 20, "status": "complete"},
        ])
        show = ctl.list_downloads()[0]
        self.assertEqual(show["kind"], "series")
        self.assertEqual(show["size"], 30)
        self.assertEqual(show["count"], 2)
        self.assertEqual(len(show["children"]), 1)
        self.assertEqual(len(show["children"][0]["children"]), 2)


class LoginController(FakeController):
    def __init__(self):
        super().__init__()
        self.qc_calls = []
        self.cancelled_at = None

    def known_servers(self):
        return [{"address": "http://old.example", "name": "Old Server"}]

    approved = False

    def quick_connect(self, server, code_callback, should_cancel):
        self.qc_calls.append(server)
        self.codes_shown = []
        code_callback("ABC123")
        # Capture what the screen looked like while the code was live — the
        # call is blocking, so by the time it returns the route has moved on.
        self.codes_shown.append(dict(self.route_ref.get("_qc") or {}))
        self.cancelled_at = should_cancel()
        return self.approved


class TestAddServer(unittest.TestCase):
    def setUp(self):
        self.ctl = LoginController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_first_run_login_has_no_way_back(self):
        """With no servers there is no library behind the form."""
        self.b.server = None
        self.b.show_login()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("login-cancel", ids(nodes))

    def test_adding_another_server_can_be_cancelled(self):
        self.b.show_login()          # server is set -> pushed, not reset
        nodes, h = build_scene(self.b)
        self.assertIn("login-cancel", ids(nodes))
        h["login-cancel"]["click"]()
        self.assertNotEqual(self.b.route["kind"], "login")

    def test_known_servers_are_offered(self):
        self.b.show_login()
        nodes, h = build_scene(self.b)
        self.assertIn("login-known-0", ids(nodes))
        h["login-known-0"]["click"]()
        self.assertEqual(self.b._login["server"], "http://old.example")

    def test_quick_connect_needs_a_server_url(self):
        self.b.show_login()
        _n, h = build_scene(self.b)
        h["login-qc"]["click"]()
        self.assertEqual(self.ctl.qc_calls, [])
        self.assertIn("URL", self.b._login_error)

    def test_quick_connect_shows_the_code(self):
        self.b.show_login()
        self.b._login["server"] = "http://srv"
        self.ctl.route_ref = self.b.route
        _n, h = build_scene(self.b)
        h["login-qc"]["click"]()
        self.assertEqual(self.ctl.qc_calls, ["http://srv"])
        # The code reached the screen while the login was in flight.
        self.assertEqual(self.ctl.codes_shown[0].get("code"), "ABC123")
        # It wasn't approved, so we're back on the form with an explanation.
        nodes, _h = build_scene(self.b)
        self.assertIn("login-connect", ids(nodes))
        self.assertIn("Quick Connect", self.b._login_error)

    def test_quick_connect_code_renders(self):
        self.b.show_login()
        self.b.route["_qc"] = {"code": "ABC123", "status": "Waiting…",
                               "cancelled": False}
        nodes, _h = build_scene(self.b)
        self.assertIn("ABC123", [n.get("text") for n in nodes])
        self.assertNotIn("login-connect", ids(nodes))

    def test_quick_connect_can_be_cancelled(self):
        self.b.show_login()
        self.b._login["server"] = "http://srv"
        route = self.b.route
        route["_qc"] = {"code": "ZZZ", "status": "", "cancelled": False}
        _n, h = build_scene(self.b)
        h["login-qc-cancel"]["click"]()
        self.assertNotIn("_qc", route)
        nodes, _h = build_scene(self.b)
        self.assertIn("login-connect", ids(nodes))   # back to the form


class TestDownloadStatusBar(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def test_hidden_when_nothing_is_downloading(self):
        self.b.set_download_status(None)
        nodes, _h = build_scene(self.b)
        self.assertNotIn("dlbar-view", ids(nodes))

    def test_shows_progress_and_a_way_into_the_manager(self):
        self.b.set_download_status({"pending": 3, "name": "Pilot",
                                    "percent": 42})
        nodes, h = build_scene(self.b)
        self.assertIn("dlbar-view", ids(nodes))
        texts = " ".join(n.get("text", "") for n in nodes if n["t"] == "text")
        self.assertIn("Pilot", texts)
        self.assertIn("42%", texts)
        h["dlbar-view"]["click"]()
        self.assertEqual(self.b.route["kind"], "settings")
        self.assertEqual(self.b.route["_tab"], "downloads")

    def test_unknown_percentage_still_shows_the_bar(self):
        self.b.set_download_status({"pending": 1, "name": "X",
                                    "percent": None})
        nodes, _h = build_scene(self.b)
        self.assertIn("dlbar-view", ids(nodes))


class TestOneBlue(unittest.TestCase):
    """There is exactly one blue. A second, unrelated blue makes the UI look
    assembled from parts, so anything the app colours itself must come from
    the accent family."""

    ACCENT_FAMILY = None   # filled in setUp

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        self.theme = theme
        self.ACCENT_FAMILY = {theme.ACCENT, theme.ACCENT_HOVER,
                              theme.ACCENT_SOFT}
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()

    @staticmethod
    def _is_blue(hexstr):
        try:
            r, g, bl = (int(hexstr[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, TypeError, IndexError):
            return False
        # Blue-dominant and not a near-grey.
        return bl > r + 25 and bl > 40 and (bl - min(r, g)) > 25

    def _blues_in(self, nodes):
        out = set()
        for n in nodes:
            for key in ("fill", "c", "bc"):
                v = n.get(key)
                if isinstance(v, str) and self._is_blue(v):
                    out.add(v)
            hov = n.get("hover") or {}
            for key in ("fill", "c", "bc"):
                v = hov.get(key)
                if isinstance(v, str) and self._is_blue(v):
                    out.add(v)
        return out

    def _check(self, label):
        nodes, _h = build_scene(self.b)
        stray = self._blues_in(nodes) - self.ACCENT_FAMILY
        self.assertEqual(stray, set(),
                         "%s uses blues outside the accent family" % label)

    def test_home_tiles_hover_ring(self):
        self.b.route["_data"] = {"libraries": self.b.source.libraries,
                                 "rows": self.b.source.home_rows}
        self._check("home")

    def test_update_banner(self):
        self.b.notify_update("1.2.3", "http://x")
        self._check("update banner")

    def test_download_status_bar(self):
        self.b.set_download_status({"pending": 2, "name": "X", "percent": 50})
        self._check("download bar")

    def test_selected_rows(self):
        self.b.navigate({"kind": "playlist_edit", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        self.b.route["_sel"] = {0}
        self._check("playlist editor selection")

    def test_music_tabs_and_settings_tabs(self):
        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "lib1", "title": "Music"})
        self._check("music tabs")
        self.b.open_settings("general")
        self._check("settings tabs")

    def test_toolkit_widgets_take_the_app_accent(self):
        """The toolkit's own accented widgets (checkbox fill, hover ring,
        progress) follow the app palette rather than mpvtk's default."""
        from jellyfin_mpv_shim.mpvtk.layout import layout as lay
        from jellyfin_mpv_shim.mpvtk.widgets import Checkbox, Progress
        for widget in (Checkbox("x", True), Progress(0.5)):
            nodes, _h = lay(widget, 200, 50)
            self.assertEqual(
                self._blues_in(nodes) - self.ACCENT_FAMILY, set(),
                "%s used a blue outside the accent family"
                % type(widget).__name__)

    def test_checked_checkbox_in_a_real_view(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.b.route["_filters"] = {"unplayed": True, "favorite": True}
        self._check("grid filter bar with checked boxes")


class TestTrackListVirtualization(unittest.TestCase):
    """Track tables must window their rows. With the album-art column each
    visible row is one mpv overlay, so a few hundred tracks would blow the
    63-overlay budget outright — not just cost a slow repaint."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()
        self.tracks = [{"Id": "t%d" % i, "Name": "Track %d" % i,
                        "Type": "Audio", "IndexNumber": i + 1,
                        "RunTimeTicks": 2000000000} for i in range(400)]
        # _track_list reads _size when it computes the virtual window, so it
        # has to be set before the tree is built, not just before layout.
        self.b._size = (1280, 720)

    def _row_ids(self, node, size=(1280, 720)):
        nodes, _h = layout(node, *size)
        return {n["id"] for n in nodes
                if isinstance(n.get("id"), str) and n["id"].startswith("t-")
                and n["id"].count("-") == 1}

    def test_only_a_window_of_rows_is_built(self):
        node = self.b._track_list(self.tracks, "t", lambda i: None,
                                  scroll_id="album")
        ids = self._row_ids(node)
        self.assertGreater(len(ids), 0)
        self.assertLess(len(ids), 60, "should not materialize 400 rows")

    def test_window_follows_the_scroll_offset(self):
        top = self._row_ids(self.b._track_list(
            self.tracks, "t", lambda i: None, scroll_id="album"))
        self.b._scroll_off["album"] = 6000
        bottom = self._row_ids(self.b._track_list(
            self.tracks, "t", lambda i: None, scroll_id="album"))
        self.assertTrue(top and bottom)
        self.assertNotEqual(top, bottom)

    def test_without_a_scroll_id_nothing_is_windowed(self):
        """Short lists inside another scroll keep the simple path."""
        node = self.b._track_list(self.tracks[:5], "t", lambda i: None)
        self.assertEqual(len(self._row_ids(node)), 5)

    def test_art_column_stays_within_the_overlay_budget(self):
        from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode
        node = self.b._track_list(self.tracks, "t", lambda i: None,
                                  art=True, scroll_id="playlist")
        nodes, _h = layout(node, 1280, 720)
        images = [n for n in nodes if n["t"] == "img"]
        self.assertLess(len(images), 63, "exceeds mpv's overlay budget")
        _ = ImageNode


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
            self.b._scroll_off["playlist"] = off
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
                and not str(n["id"]).endswith(("-rm", "-tgl"))]

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


class TestRemoteDisplayContent(unittest.TestCase):
    """Jellyfin's DisplayContent ("show me this" from a phone) opens the
    item's page in the browser, which the remote's arrows can then drive.
    The legacy display_mirroring kiosk shows a static backdrop instead."""

    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_opens_the_item_page(self):
        self.b.display_item("srv1", "m1")
        self.assertEqual(self.b.route["kind"], "detail")
        self.assertEqual(self.b.route["item_id"], "m1")

    def test_routes_by_item_type(self):
        """A series lands on the series page, not a detail page — the same
        dispatch a click uses."""
        src = self.b.source
        src.get_item = lambda s, i: {"Id": i, "Name": "Show", "Type": "Series"}
        self.b.display_item("srv1", "sh1")
        self.assertEqual(self.b.route["kind"], "series")

    def test_wakes_a_minimized_client(self):
        self.b.minimize()
        self.b.display_item("srv1", "m1")
        self.assertFalse(self.b.minimized)
        self.assertTrue(self.b._browsing)
        self.assertEqual(self.b.route["kind"], "detail")

    def test_never_interrupts_playback(self):
        """jellyfin-web emits DisplayContent as you browse on the phone, so
        casting a page while something plays here must not stop it."""
        self.b._browsing = False        # video playing
        self.b.display_item("srv1", "m1")
        self.assertFalse(self.b._browsing, "took the window from playback")
        self.assertEqual(self.ctl.entered, 0)
        # The page is waiting when playback ends.
        self.assertEqual(self.b.route["kind"], "detail")

    def test_a_cast_track_opens_its_album_rather_than_playing(self):
        """Same reason: DisplayContent is a browse gesture, not a play one."""
        self.b.source.get_item = lambda s, i: {
            "Id": i, "Name": "Song", "Type": "Audio", "AlbumId": "al9",
            "Album": "The Album"}
        self.b.display_item("srv1", "so1")
        self.assertEqual(self.b.route["kind"], "album")
        self.assertEqual(self.b.route["item_id"], "al9")
        self.assertEqual(self.ctl.played, [], "must not start playback")

    def test_a_cast_track_with_no_album_falls_back(self):
        self.b.source.get_item = lambda s, i: {
            "Id": i, "Name": "Song", "Type": "Audio"}
        self.b.display_item("srv1", "so1")
        self.assertEqual(self.ctl.played, [], "must not start playback")

    def test_switches_server_when_the_cast_comes_from_another(self):
        self.b.display_item("srv2", "m1")
        self.assertEqual(self.b.server, "srv2")

    def test_go_to_settings_opens_the_settings_page(self):
        """GoToSettings used to alias to GoHome, which predates the browser
        having a settings page."""
        self.assertTrue(self.b.on_nav_command("settings"))
        self.assertEqual(self.b.route["kind"], "settings")

    def test_go_home_resets_to_the_library(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.assertTrue(self.b.on_nav_command("home"))
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1, "should reset the stack")

    def test_unknown_command_is_declined(self):
        self.assertFalse(self.b.on_nav_command("nope"))

    def test_a_missing_item_is_a_no_op(self):
        self.b.source.get_item = lambda s, i: None
        before = self.b.route["kind"]
        self.b.display_item("srv1", "nope")
        self.assertEqual(self.b.route["kind"], before)


class FakeThumbs:
    """Stands in for ThumbnailStore: records requests and lets the test
    decide how each one resolves."""

    def __init__(self):
        self.requests = []          # (key, url)
        self.gone = set()           # keys the "server" says don't exist
        self._cbs = {}              # key -> callback
        self._notify = None

    def get_cached(self, key):
        return None

    def is_gone(self, key):
        return key in self.gone

    def request(self, key, url, box, callback):
        self.requests.append((key, url))
        self._cbs[key] = callback

    def resolve(self, key, image):
        """Deliver a result the way pump() does — including failures."""
        self._cbs.pop(key)(image)

    def pump(self):
        return False


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
        return self.b._request_image("k1", "http://s/img", (10, 10))

    def test_transient_failure_is_retried_once_the_backoff_passes(self):
        self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 1)

        self.thumbs.resolve("k1", None)          # timeout / 5xx
        self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 1,
                         "must cool off before retrying")

        # ...and once the backoff elapses, it asks again
        attempts, _when = self.b._img_retry["k1"]
        self.b._img_retry["k1"] = (attempts, 0.0)
        self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 2, "never retried")

        self.thumbs.resolve("k1", "IMG")
        self.assertEqual(self._ask(), "IMG")
        self.assertEqual(len(self.thumbs.requests), 2)
        self.assertNotIn("k1", self.b._img_retry)

    def test_a_permanent_miss_is_not_retried(self):
        """The server saying "no such image" is an answer, not a failure
        to retry — otherwise every art-less item re-asks forever."""
        self._ask()
        self.thumbs.gone.add("k1")
        self.thumbs.resolve("k1", None)
        for _ in range(3):
            self.b._img_retry.pop("k1", None)    # even with no cooldown
            self.assertIsNone(self._ask())
        self.assertEqual(len(self.thumbs.requests), 1)

    def test_retries_are_capped(self):
        self._ask()
        for _ in range(self.b.IMG_MAX_ATTEMPTS + 3):
            key = self.thumbs.requests[-1][0]
            if key in self.thumbs._cbs:
                self.thumbs.resolve(key, None)
            self.b._img_retry["k1"] = (self.b._img_retry["k1"][0], 0.0)
            self._ask()
        self.assertLessEqual(len(self.thumbs.requests),
                             self.b.IMG_MAX_ATTEMPTS + 1,
                             "a dead URL must stop being retried")

    def test_a_successful_image_is_not_refetched(self):
        self._ask()
        self.thumbs.resolve("k1", "IMG")
        for _ in range(3):
            self.assertEqual(self._ask(), "IMG")
        self.assertEqual(len(self.thumbs.requests), 1)


class TestTrackListArtWindowing(unittest.TestCase):
    """Art cells composite into the 48-entry strip LRU as they are built,
    so a long playlist must only build them for the visible window — the
    unwindowed version evicted (and freed the buffers of) the very rows
    on screen, which then drew blank on every repaint."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._size = (1280, 720)
        self.built = []
        self.b._art_cell = lambda tr, size=28: self.built.append(
            tr.get("Id")) or self.b._art_placeholder(size)

    def _tracks(self, n):
        return [{"Id": "t%d" % i, "Name": "Track %d" % i,
                 "Type": "Audio"} for i in range(n)]

    def test_only_the_visible_window_composites_art(self):
        tracks = self._tracks(300)
        self.b._track_list(tracks, "pl", on_play=lambda i: None,
                           art=True, scroll_id="playlist", head_h=70)
        self.assertLess(len(self.built), 48,
                        "must stay under the strip LRU bound")
        self.assertIn("t0", self.built, "the top rows are visible")
        self.assertNotIn("t299", self.built, "off-screen rows must not")

    def test_scrolling_moves_the_window(self):
        tracks = self._tracks(300)
        self.b._scroll_off["playlist"] = 100 * self.b.TRACK_ROW_H + 70
        self.b._track_list(tracks, "pl", on_play=lambda i: None,
                           art=True, scroll_id="playlist", head_h=70)
        self.assertNotIn("t0", self.built)
        self.assertIn("t100", self.built, "scrolled-to rows composite")
        self.assertLess(len(self.built), 48)

    def test_short_lists_are_unaffected(self):
        tracks = self._tracks(12)
        self.b._track_list(tracks, "pl", on_play=lambda i: None,
                           art=True, scroll_id="playlist", head_h=70)
        self.assertEqual(len(self.built), 12)


def _sub_item(default_sid=None, default_aid=None, subs=(3, 4), audios=(1,)):
    streams = [{"Type": "Audio", "Index": i, "DisplayTitle": "Audio %d" % i}
               for i in audios]
    streams += [{"Type": "Subtitle", "Index": i, "DisplayTitle": "Sub %d" % i}
                for i in subs]
    src = {"Id": "src1", "MediaStreams": streams}
    if default_sid is not None:
        src["DefaultSubtitleStreamIndex"] = default_sid
    if default_aid is not None:
        src["DefaultAudioStreamIndex"] = default_aid
    return {"Id": "m1", "Name": "Movie", "Type": "Movie",
            "MediaSources": [src]}


class TestTrackDefaults(unittest.TestCase):
    """The detail page's pickers must show the tracks that will actually
    play. They showed a hardcoded "None" for subtitles, and because a
    browser selection is taken as final downstream (explicit_tracks), that
    lie became the playback behaviour."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.route = {"kind": "detail", "server": "srv1"}

    def test_server_default_subtitle_is_preselected(self):
        item = _sub_item(default_sid=4)
        _aid, sid = self.b._effective_tracks(self.route, item)
        self.assertEqual(sid, 4, "showed None instead of the server default")

    def test_language_config_beats_the_server_default(self):
        import jellyfin_mpv_shim.language_config as lc

        item = _sub_item(default_sid=4)
        real, lc.apply = lc.apply, lambda rules, src, it: (None, 3)
        self.addCleanup(lambda: setattr(lc, "apply", real))
        _aid, sid = self.b._effective_tracks(self.route, item)
        self.assertEqual(sid, 3, "language_config must win")

    def test_explicit_none_is_not_overwritten_by_the_default(self):
        """-1 is a deliberate "no subtitles" and must survive; only an
        untouched picker (None) falls back to the default."""
        item = _sub_item(default_sid=4)
        self.route["_sid"] = -1
        _aid, sid = self.b._effective_tracks(self.route, item)
        self.assertEqual(sid, -1)

    def test_picking_audio_still_carries_the_subtitle_default(self):
        """The poisoning case: touching only Audio marked the play
        explicit with sid=None, so map_streams returned before applying
        DefaultSubtitleStreamIndex and subtitles came up off."""
        item = _sub_item(default_sid=4)
        self.route["_aid"] = 1
        aid, sid = self.b._effective_tracks(self.route, item)
        self.assertEqual((aid, sid), (1, 4))

    def test_no_subtitle_streams_reports_no_choice(self):
        """An item with no subtitles must not send a spurious index —
        that would mark the play explicit for no reason."""
        item = _sub_item(default_sid=None, subs=())
        _aid, sid = self.b._effective_tracks(self.route, item)
        self.assertIsNone(sid)

    def test_picker_shows_the_default_not_none(self):
        item = _sub_item(default_sid=4)
        from jellyfin_mpv_shim.mpvtk.widgets import Column

        rows = self.b._track_pickers(self.route, item)
        self.assertTrue(rows, "expected pickers")
        nodes, _h = layout(Column(rows), 1280, 720)
        dd = [n for n in nodes if n.get("id") == "dt-sub"]
        self.assertTrue(dd, "no subtitle picker rendered")
        # options are ["None", "Sub 3", "Sub 4"] -> index 2
        self.assertEqual(dd[0].get("sel"), 2)

    def test_defaults_are_resolved_once_per_source(self):
        """_effective_tracks runs from build(), so the language_config
        walk (which logs on every call) must not run per repaint."""
        import jellyfin_mpv_shim.language_config as lc

        calls = []
        item = _sub_item(default_sid=4)
        real = lc.apply
        lc.apply = lambda rules, src, it: (calls.append(1), (None, None))[1]
        self.addCleanup(lambda: setattr(lc, "apply", real))
        for _ in range(5):
            self.b._effective_tracks(self.route, item)
        self.assertEqual(len(calls), 1, "should be cached on the route")


class TestPlaylistTileShape(unittest.TestCase):
    """A Jellyfin playlist's own Primary image is square; rendering it in
    the 2:3 poster frame pillarboxes it."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_all_playlist_grid_is_square(self):
        items = [{"Id": "p1", "Type": "Playlist"},
                 {"Id": "p2", "Type": "Playlist"}]
        self.assertIs(self.b._square_geom(items), self.b.geom_square)

    def test_music_stays_square(self):
        self.assertIs(
            self.b._square_geom([{"Id": "a1", "Type": "MusicAlbum"}]),
            self.b.geom_square)

    def test_a_mixed_grid_keeps_posters(self):
        """One strip is composited at a single tile size, so a grid that
        mixes shapes has to pick the default rather than square everything."""
        items = [{"Id": "p1", "Type": "Playlist"},
                 {"Id": "m1", "Type": "Movie"}]
        self.assertIsNone(self.b._square_geom(items))

    def test_movies_are_not_square(self):
        self.assertIsNone(self.b._square_geom([{"Id": "m1", "Type": "Movie"}]))

    def test_empty_grid_keeps_the_default(self):
        self.assertIsNone(self.b._square_geom([]))

    def test_playlists_home_row_is_square(self):
        geom, itype = self.b._row_shape(
            {"collection_type": "playlists", "items": [
                {"Id": "p1", "Type": "Playlist"}]})
        self.assertIs(geom, self.b.geom_square)
        self.assertEqual(itype, "Primary")

    def test_an_untyped_playlist_row_is_still_square(self):
        geom, _t = self.b._row_shape(
            {"collection_type": None,
             "items": [{"Id": "p1", "Type": "Playlist"}]})
        self.assertIs(geom, self.b.geom_square)


class TestDownloadsGroupDelete(unittest.TestCase):
    """The flat "Movies & Videos" group has no server-side id, so its
    Remove button must enumerate its own rows. Passing no scope reached
    syncManager.delete() with every id None — the whole catalog."""

    def test_a_group_without_an_id_deletes_only_its_own_rows(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        group = {"kind": "movies", "id": None, "title": "Movies & Videos",
                 "children": [{"kind": "item", "id": "m1"},
                              {"kind": "item", "id": "m2"}]}
        self.assertEqual(b._dl_group_item_ids(group), ["m1", "m2"])

    def test_season_rows_are_collected_from_nested_children(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        group = {"kind": "series", "id": "sh1", "children": [
            {"kind": "season", "id": "s1", "children": [
                {"kind": "item", "id": "e1"}, {"kind": "item", "id": "e2"}]}]}
        self.assertEqual(b._dl_group_item_ids(group), ["e1", "e2"])

    def test_an_empty_group_yields_no_ids(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        self.assertEqual(b._dl_group_item_ids({"kind": "movies"}), [])


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
            cols = self.b._cols(w, geom)
            used = cols * geom.tile_w + (cols - 1) * geom.gap
            self.assertLessEqual(
                used, self.b._body_w(w),
                "%d columns don't fit at w=%d" % (cols, w))


class TestWatchedState(unittest.TestCase):
    """`(count or 0) == 0` reads a MISSING unplayed count as "nothing
    unplayed", i.e. fully watched — so a Series without UserData showed a
    watched tick and the toggle then marked an unwatched show unwatched."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_a_series_without_userdata_is_not_watched(self):
        self.assertFalse(self.b._is_watched({"Id": "s1", "Type": "Series"}))

    def test_a_series_with_no_unplayed_count_is_not_watched(self):
        self.assertFalse(self.b._is_watched(
            {"Id": "s1", "Type": "Series", "UserData": {}}))

    def test_zero_unplayed_is_watched(self):
        self.assertTrue(self.b._is_watched(
            {"Id": "s1", "Type": "Series",
             "UserData": {"UnplayedItemCount": 0}}))

    def test_remaining_episodes_are_not_watched(self):
        self.assertFalse(self.b._is_watched(
            {"Id": "s1", "Type": "Series",
             "UserData": {"UnplayedItemCount": 3}}))

    def test_played_flag_still_wins_for_movies(self):
        self.assertTrue(self.b._is_watched(
            {"Id": "m1", "Type": "Movie", "UserData": {"Played": True}}))

    def test_toggling_an_untouched_series_marks_it_watched(self):
        """The consequence of the bug: the first click was a no-op."""
        calls = []
        ctl = FakeController()
        ctl.set_watched = lambda srv, iid, w: calls.append(w) or True
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._act_watched({"Id": "s1", "Type": "Series"}, "srv1")
        self.assertEqual(calls, [True], "first click must mark it WATCHED")

    def test_a_failed_write_rolls_the_optimistic_flip_back(self):
        ctl = FakeController()
        ctl.set_watched = lambda srv, iid, w: False
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        item = {"Id": "m1", "Type": "Movie", "UserData": {"Played": False}}
        b._act_watched(item, "srv1")
        self.assertFalse(item["UserData"]["Played"],
                         "UI kept a tick for a change that never happened")


class TestNewPlaylistPrivacy(unittest.TestCase):
    """The server creates playlists PUBLIC unless told otherwise, so
    omitting the flag published every playlist to the whole server."""

    def test_new_playlists_default_to_private(self):
        calls = []
        ctl = FakeController()
        ctl.playlist_new = lambda *a, **kw: calls.append((a, kw))
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._addto_name = {"name": "Road Trip", "private": True}
        b._add_to_new("srv1", "m1")
        self.assertEqual(calls[0][1].get("is_public"), False)

    def test_unticking_private_creates_a_public_playlist(self):
        calls = []
        ctl = FakeController()
        ctl.playlist_new = lambda *a, **kw: calls.append((a, kw))
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._addto_name = {"name": "Shared", "private": False}
        b._add_to_new("srv1", "m1")
        self.assertEqual(calls[0][1].get("is_public"), True)


class TestPinSetup(unittest.TestCase):
    """Blank new+confirm compared equal and fell through to set_pin(None),
    so Save on a "Set PIN" dialog quietly REMOVED the lock."""

    def _dialog(self, locked=False):
        calls = []
        ctl = FakeController()
        ctl.set_user_pin = lambda uid, pin, require_startup=False: (
            calls.append((uid, pin, require_startup)) or True)
        ctl.unlock_user = lambda uid, pin: True
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._open_pin_setup({"id": "u1", "name": "Kid", "locked": locked})
        _n, handlers = build_scene(b)
        return b, handlers, calls

    def test_saving_with_blank_fields_does_not_clear_the_pin(self):
        b, handlers, calls = self._dialog(locked=True)
        handlers["ps-ok"]["click"]()
        self.assertEqual(calls, [], "blank Save removed the lock")
        # the dialog stays open reporting why
        nodes, _h = build_scene(b)
        texts = " ".join(n.get("text", "") for n in nodes if n.get("text"))
        self.assertIn("new PIN", texts)

    def test_a_matching_pin_is_saved(self):
        b, handlers, calls = self._dialog()
        handlers["ps-new"]["change"]("1234")
        handlers["ps-confirm"]["change"]("1234")
        handlers["ps-ok"]["click"]()
        self.assertEqual([c[1] for c in calls], ["1234"])

    def test_mismatched_pins_are_refused(self):
        _b, handlers, calls = self._dialog()
        handlers["ps-new"]["change"]("1234")
        handlers["ps-confirm"]["change"]("9999")
        handlers["ps-ok"]["click"]()
        self.assertEqual(calls, [])


class _FailingSource(FakeSource):
    """A source whose browse calls raise, like an unreachable server."""

    def __init__(self, fail=True):
        super().__init__()
        self.fail = fail
        self.calls = 0

    def _boom(self, *a, **k):
        self.calls += 1
        if self.fail:
            raise OSError("server unreachable")
        return []

    get_libraries = _boom
    get_home_rows = _boom


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
        b._on_grid_scroll(route, 0, 100)
        self.assertFalse(route.get("_loading"),
                         "_loading stuck: the grid can never page again")
        b._on_grid_scroll(route, 0, 100)
        self.assertEqual(len(calls), 2, "second page attempt never happened")

    def test_an_empty_page_ends_the_list(self):
        b, route, calls = self._grid(page_result=([], 100))
        b._on_grid_scroll(route, 0, 100)
        self.assertEqual(route["_total"], 20, "total not clamped to loaded")
        b._on_grid_scroll(route, 0, 100)
        self.assertEqual(len(calls), 1, "re-requested an empty page")

    def test_a_normal_page_appends(self):
        b, route, calls = self._grid(page_result=([{"Id": "x"}], 100))
        b._on_grid_scroll(route, 0, 100)
        self.assertEqual(len(route["_items"]), 21)
        self.assertEqual(route["_total"], 100)


class TestPlaylistQueueing(unittest.TestCase):
    """Clicking an entry in a video playlist must play the PLAYLIST from
    that point. It went through _open_item, so Play on the detail page
    queued the item's series instead — silently abandoning the playlist."""

    def setUp(self):
        self.ctl = FakeController()
        self.plays = []
        self.ctl.play_list = lambda ids, srv, i, **kw: self.plays.append(
            (list(ids), i, kw.get("offset_ticks")))
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _items(self):
        return [
            {"Id": "m1", "Type": "Movie", "Name": "One"},
            {"Id": "e1", "Type": "Episode", "Name": "Two",
             "UserData": {"PlaybackPositionTicks": 90000000}},
            {"Id": "m2", "Type": "Movie", "Name": "Three"},
        ]

    def test_clicking_an_entry_queues_the_playlist_from_there(self):
        items = self._items()
        ids = [i["Id"] for i in items]
        self.b._play_list(ids, "srv1", 1, items=items)
        played_ids, start, _off = self.plays[0]
        self.assertEqual(played_ids, ids, "queued something other than "
                         "the playlist")
        self.assertEqual(start, 1)

    def test_the_clicked_entry_resumes(self):
        items = self._items()
        self.b._play_list([i["Id"] for i in items], "srv1", 1, items=items)
        self.assertEqual(self.plays[0][2], 90000000, "resume offset lost")

    def test_an_entry_without_progress_starts_from_zero(self):
        items = self._items()
        self.b._play_list([i["Id"] for i in items], "srv1", 0, items=items)
        self.assertIsNone(self.plays[0][2])

    def test_a_missing_id_does_not_shift_the_queue(self):
        """Filtering empties out before using the caller's index moved the
        queue out from under the entry that was clicked."""
        items = [{"Id": None, "Type": "Movie"},
                 {"Id": "m2", "Type": "Movie"},
                 {"Id": "m3", "Type": "Movie"}]
        ids = [i["Id"] for i in items]
        self.b._play_list(ids, "srv1", 2, items=items)
        played_ids, start, _off = self.plays[0]
        self.assertEqual(played_ids[start], "m3",
                         "started the wrong entry")

    def test_an_out_of_range_index_falls_back_to_the_start(self):
        self.b._play_list(["m1", "m2"], "srv1", 9)
        self.assertEqual(self.plays[0][1], 0)

    def test_video_playlists_render_only_supported_types(self):
        route = {"kind": "playlist", "server": "srv1", "item_id": "P",
                 "title": "Mix", "_data": self._items() + [
                     {"Id": "x1", "Type": "Photo", "Name": "Nope"}]}
        self.b.nav_stack = [route]
        nodes, _h = build_scene(self.b)
        rendered = " ".join(str(n.get("id", "")) for n in nodes)
        self.assertNotIn("x1", rendered, "unsupported entry rendered a tile")
        self.assertIn("m1", rendered)


class TestWorkOfflineToggle(unittest.TestCase):
    """work_offline was persisted and then ignored until the next launch —
    the classic "setting written but not applied"."""

    def _browser(self, offline_source=None, live_source=None):
        ctl = FakeController()
        ctl.offline_source = lambda: offline_source
        ctl.connect_and_rebuild = lambda: live_source
        cfg = FakeConfig()
        cfg.schema["work_offline"] = "bool"
        cfg.values["work_offline"] = False
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl,
                         config=cfg)
        b._pool = _SyncPool()
        return b

    def test_turning_it_on_swaps_to_the_downloads(self):
        offline = FakeSource()
        b = self._browser(offline_source=offline)
        b._set_setting("work_offline", True)
        self.assertIs(b.source, offline, "still on the live source")

    def test_turning_it_off_reconnects(self):
        live = FakeSource()
        b = self._browser(offline_source=FakeSource(), live_source=live)
        b._set_setting("work_offline", True)
        b._offline = True
        b._set_setting("work_offline", False)
        self.assertIs(b.source, live)

    def test_nothing_downloaded_reports_instead_of_blanking(self):
        b = self._browser(offline_source=None)
        before = b.source
        b._set_setting("work_offline", True)
        self.assertIs(b.source, before, "swapped to an empty source")
        self.assertIn("Nothing downloaded", b.status)

    def test_other_settings_do_not_touch_the_source(self):
        b = self._browser(offline_source=FakeSource())
        before = b.source
        b._set_setting("player_name", "Bud")
        self.assertIs(b.source, before)
