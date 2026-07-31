"""Shared fixtures for the mpvtk browser shell tests.

Split out of ``test_mpvtk_browser_shell.py`` when that file reached 8,560
lines and 661 tests in one module. Everything here is fixture, not
assertion: a fake data source (no network), pool doubles that make async
work synchronous or deliberately stuck, a fake player controller, and the
handful of seam helpers that reach a ``Page`` the way the shell would.

The ``*_page`` helpers exist because step 6c moved screen logic off
``MpvtkBrowser`` and onto ``Page`` objects. Tests go through
``b._page_for(route)`` rather than poking the shell, which is the same path
production takes.

Not named ``test_*`` so unittest discovery ignores it; imported as
``tests._shell_harness`` (see ``tests/_scene_snapshot.py`` for the same
pattern).
"""

import threading
import time
import unittest
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser import components, home_sections, live_tv
from jellyfin_mpv_shim.mpvtk_browser import themes
from jellyfin_mpv_shim.mpvtk_browser import tile_renderer
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

# Ignore the developer's own theme directory. The Settings screen lists the
# themes it can actually find, so without this a scene built here depends on
# what is in ~/.config/jellyfin-mpv-shim/themes -- the settings snapshot bakes
# the list into a dropdown's items, and anyone who had written a theme would
# get a failure that reproduced nowhere else. Shipped themes only.
_real_theme_dirs = themes.theme_dirs
themes.theme_dirs = lambda: (_real_theme_dirs()[0], None)
themes.load(force=True)


def editor_page(b, route):
    """The QueuePage / PlaylistEditPage for a route — the seam the queue and
    playlist-editor helpers moved to in 6c. Selection and item state still
    live in the route dict, so the page can be rebuilt at will."""
    return b._page_for(route)

def music_page(b, route=None):
    """The MusicLibraryPage (or album/artist/genre page) for a route — the
    seam the music screens' helpers moved to in 6c."""
    return b._page_for(route if route is not None else b.route)

def music_scroll(b, route, offset, maximum):
    """The music library's infinite-scroll handler, now on the page."""
    b._page_for(route)._on_scroll_end(offset, maximum)

def grid_scroll(b, route, offset, maximum):
    """The grid/person infinite-scroll handler, which moved onto GridPage in
    6c. Reached through the page the shell would build for that route."""
    b._page_for(route)._on_scroll_end(offset, maximum)

def detail_page(b, route):
    """A DetailPage bound to ``route`` — the seam the detail screen's private
    helpers (track pickers, media-info line, scenes row) moved to in 6c.

    Mutates ``route`` to carry its kind and returns the page cached on it, so
    per-route caching (``_def_tracks``) behaves as it does in production."""
    route.setdefault("kind", "detail")
    return b._page_for(route)

def series_page(b, route=None):
    """A SeriesPage, likewise, for ``_series_actions``."""
    return b._page_for(route if route is not None else {"kind": "series"})

def home_page(b):
    """The HomePage serving a home route, built the way the shell builds it.

    _row_shape moved onto the page in step 6c; this is the seam its tests
    now go through."""
    return b._page_for({"kind": "home"})

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
        # PrimaryImageAspectRatio because a real Jellyfin sets it from the
        # Primary image, and the library grid is shaped by the median of it
        # (GridPage._grid_shape). A fixture without one exercises only the
        # no-artwork fallback, which is not what a movies library is.
        self.grid_items = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Movie",
             "PrimaryImageAspectRatio": 2 / 3}
            for i in range(30)
        ]
        # What the view actually asked the source for. See
        # get_library_items — a fake that swallows its arguments turns every
        # test above it into a proxy assertion.
        self.queries = []

    def servers(self):
        return [{"uuid": "srv1", "name": "Home Server"}]

    def get_libraries(self, server_uuid):
        return list(self.libraries)

    def get_home_prefs(self, server_uuid, refresh=False):
        return list(home_sections.DEFAULT_LAYOUT), frozenset()

    def get_home_rows(self, server_uuid, libraries=None, sections=None,
                      layout=None, latest_excludes=None):
        return list(self.home_rows)

    def get_library_items(self, server_uuid, parent_id, start_index=0,
                          sort_by="SortName", sort_order="Ascending",
                          limit=100, filters=None):
        # Recorded, not swallowed. This took **kw and discarded sort/filters
        # entirely, so every filter, sort, unplayed-toggle and letter-jump
        # test asserted only on the browser's own scratch dict — if the view
        # stopped passing filters= to the source, all of them stayed green
        # and every filter in the app silently did nothing.
        self.queries.append({
            "parent_id": parent_id, "start_index": start_index,
            "sort_by": sort_by, "sort_order": sort_order,
            "filters": dict(filters or {}),
        })
        page = self.grid_items[start_index:start_index + 20]
        return page, len(self.grid_items)

    def get_view_settings(self, server_uuid, parent_id, collection_type):
        return dict(getattr(self, "view_settings", {}))

    def save_view_setting(self, server_uuid, parent_id, collection_type,
                          setting, value, key=None):
        self.saved_view_settings = getattr(self, "saved_view_settings", [])
        if getattr(self, "save_view_fails", False):
            raise RuntimeError("server refused")
        self.saved_view_settings.append((parent_id, setting, value, key))

    def get_by_name_sections(self, server_uuid, spec, limit=20):
        self.byname_specs = getattr(self, "byname_specs", [])
        self.byname_specs.append(dict(spec or {}))
        return getattr(self, "byname_rows", [
            {"key": "movies", "title": "Movies", "types": "Movie",
             "total": 40,
             "items": [{"Id": "bm1", "Name": "Film", "Type": "Movie"}]},
        ])

    def get_genre_sections(self, server_uuid, parent_id, collection_type,
                           limit=10, max_genres=40):
        return getattr(self, "genre_rows", [
            {"key": "g1", "title": "Action", "types": "Movie",
             "items": [{"Id": "gm1", "Name": "Film", "Type": "Movie",
                        "PrimaryImageAspectRatio": 2 / 3}]},
        ])

    def get_favorite_sections(self, server_uuid, limit=24):
        return getattr(self, "favorite_rows", [
            {"key": "movies", "title": "Movies", "types": "Movie",
             "items": [{"Id": "fm1", "Name": "Fav", "Type": "Movie"}]},
        ])

    def get_list(self, server_uuid, spec, sort_by="SortName",
                 sort_order="Ascending", start_index=0, limit=100,
                 filters=None):
        self.list_specs = getattr(self, "list_specs", [])
        self.list_specs.append(dict(spec or {}))
        items = getattr(self, "list_items", None)
        if items is None:
            items = self.grid_items
        page = items[start_index:start_index + 20]
        return page, len(items)

    def get_filter_values(self, server_uuid, parent_id=None):
        return {"genres": ["Action", "Comedy"], "years": [2020, 2021]}

    def get_shuffle_ids(self, server_uuid, parent_id, limit=200):
        return ["g0", "g5", "g9"]

    def get_play_all_ids(self, server_uuid, parent_id, sort_by="SortName",
                         sort_order="Ascending", filters=None, limit=200):
        # Echoes what it was asked with, so a caller that drops the sort or
        # the filters is visible rather than merely untested.
        self.play_all_args = (parent_id, sort_by, sort_order, filters)
        return ["g0", "g1", "g2"]

    def image_spec(self, item, image_type="Primary", width=280,
                   inherit=True):
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
        # Record the sort the caller asked for: the repository has always
        # accepted sort_by/sort_order and _load_person never passed them.
        self.person_sorts = getattr(self, "person_sorts", [])
        self.person_sorts.append((kw.get("sort_by"), kw.get("sort_order")))
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

    # -- Live TV -----------------------------------------------------------
    #
    # Enough for the Live TV screens to render. Times are built relative to
    # the clock so "is this airing" is true of the first programme, which is
    # what the guide's accent wash and the program page's Watch button key
    # off — pinning them to fixed dates would make those paths untested.

    live_tv = True

    @staticmethod
    def _program(index, offset_minutes=0, channel="c1"):
        import datetime

        start = (datetime.datetime.now().astimezone().replace(
            second=0, microsecond=0)
            + datetime.timedelta(minutes=offset_minutes))
        return {"Id": "pr%d" % index, "Name": "Program %d" % index,
                "Type": "Program", "ChannelId": channel,
                "ChannelName": "Channel One",
                "StartDate": start.astimezone(
                    datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "EndDate": (start + datetime.timedelta(minutes=30)).astimezone(
                    datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.0000000Z"),
                "IsSeries": True}

    def has_live_tv(self, server_uuid):
        return self.live_tv

    def get_live_tv_prefs(self, server_uuid, refresh=False):
        return live_tv.resolve_prefs({})

    def cache_live_tv_prefs(self, server_uuid, prefs):
        self.cached_live_tv_prefs = dict(prefs)

    def save_live_tv_prefs(self, server_uuid, prefs):
        self.saved_live_tv_prefs = dict(prefs)

    def get_program_sections(self, server_uuid, limit=12):
        return [{"key": "onnow", "title": "On Now",
                 "items": [self._program(0, -5)]},
                {"key": "movies", "title": "Upcoming Movies",
                 "items": [self._program(1, 60)]}]

    def get_channels(self, server_uuid, start_index=0, limit=100, prefs=None,
                     categories=(), add_current_program=True,
                     favorites_only=False):
        items = [{"Id": "c%d" % i, "Name": "Channel %d" % i,
                  "Type": "TvChannel", "Number": str(i)} for i in range(1, 4)]
        return items[start_index:start_index + limit], len(items)

    def get_guide_info(self, server_uuid):
        return {}

    def search_live_tv(self, server_uuid, term, limit=24):
        return {"channels": [{"Id": "c1", "Name": "Channel " + term,
                              "Type": "TvChannel"}],
                "programs": [self._program(7, 15)]}

    def get_guide(self, server_uuid, channel_ids, start, end, want_hd=False):
        return [self._program(0, -5), self._program(2, 30)]

    def get_live_program(self, server_uuid, program_id):
        return dict(self._program(0, -5), Id=program_id,
                    Overview="What it is about.")

    def get_channel_listing(self, server_uuid, channel_id, limit=200):
        return {"channel": {"Id": channel_id, "Name": "Channel 1",
                            "Type": "TvChannel", "Number": "1",
                            "UserData": {"IsFavorite": False}},
                "programs": [self._program(0, -5), self._program(2, 30)],
                "capped": False}

    def get_recordings(self, server_uuid, limit=60, is_in_progress=None,
                       series_timer_id=None):
        return [{"Id": "rec1", "Name": "A Recording", "Type": "Recording"}]

    def get_recording_folders(self, server_uuid):
        return [{"Id": "rf1", "Name": "Recordings", "Type": "Folder"}]

    def get_timers(self, server_uuid, is_active=None, is_scheduled=None,
                   series_timer_id=None):
        return [dict(self._program(3, 120), Id="tm1", Type="Timer",
                     Name="Scheduled Thing")]

    def get_timer(self, server_uuid, timer_id):
        return {"Id": timer_id, "Name": "Scheduled Thing",
                "ChannelName": "Channel One", "PrePaddingSeconds": 60,
                "PostPaddingSeconds": 120}

    def get_series_timers(self, server_uuid):
        return [{"Id": "st1", "Name": "A Series", "Type": "SeriesTimer",
                 "ChannelName": "Channel One"}]

    def get_series_timer(self, server_uuid, timer_id):
        return {"Id": timer_id, "Name": "A Series",
                "ChannelName": "Channel One", "RecordNewOnly": True,
                "KeepUpTo": 0, "PrePaddingSeconds": 60,
                "PostPaddingSeconds": 120}

class _SyncPool:
    """Runs submitted work inline so route loaders complete deterministically
    within the test (no threads, no shutdown races)."""

    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass

class _RecordingPool(_SyncPool):
    """Runs work inline like _SyncPool, but counts it — for asserting that a
    long job did *not* go to the shared pool."""

    def __init__(self):
        self.submitted = 0

    def submit(self, fn, *a, **k):
        self.submitted += 1
        return super().submit(fn, *a, **k)

class _DeferredPool:
    """Holds submitted work until drain(), so a test can move the epoch
    (navigate) while a call is "in flight" — the window _SyncPool closes by
    running everything inline."""

    def __init__(self):
        self.queued = []

    def submit(self, fn, *a, **k):
        self.queued.append((fn, a, k))

    def drain(self):
        while self.queued:
            fn, a, k = self.queued.pop(0)
            fn(*a, **k)

    def shutdown(self, *a, **k):
        pass

class _NeverPool:
    """Swallows submitted work, so async results never land — for testing
    the "still loading" state of a view."""

    def submit(self, fn, *a, **k):
        pass

    def shutdown(self, *a, **k):
        pass

def build_scene(browser, size=(1280, 720)):
    nodes, handlers = layout(browser.build(size), *size)
    return nodes, handlers

def menu_pick(browser, action):
    """Invoke the tile-menu entry whose action is ``action``.

    By action, not index: the menu is now built per item type, so a
    hardcoded index silently selects a different entry (or none).
    """
    entries = browser._tile_menu_entries(browser._menu["item"])
    for i, (_label, _icon, key) in enumerate(entries):
        if key == action:
            browser._menu_action(i, None)
            return
    raise AssertionError("no %r entry for this item: %r"
                         % (action, [e[2] for e in entries]))

def ids(nodes):
    return {n.get("id") for n in nodes}

def types(nodes):
    return [n["t"] for n in nodes]

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
                  srcid=None, aid=None, sid=None, pause_stills=True):
        self.played.append((list(item_ids), server_uuid, start_index))
        self.__dict__.setdefault("pause_stills", []).append(pause_stills)
        self.__dict__.setdefault("tracks", []).append(
            {"srcid": srcid, "aid": aid, "sid": sid})

    def get_queue(self):
        return {"items": [{"id": "q%d" % i, "playlist_item_id": "p%d" % i}
                          for i in range(3)], "current_id": "q1"}

    def get_sync_groups(self, server_uuid=None):
        return [{"id": "g1", "name": "Group 1", "server_uuid": "srv1",
                 "server_name": "Home"}]

    def sync_state(self):
        return None

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

    def edit_apis(self):
        return True

    def connect_and_rebuild(self):
        return FakeSource()

    def __getattr__(self, name):
        # Record transport calls (toggle_pause/stop/next/prev/…) without
        # declaring each one.
        if name.startswith(("_", "on_")) or name in ("play", "play_list"):
            raise AttributeError(name)
        calls = self.__dict__.setdefault("transport", [])
        # Keywords go in a parallel list: `transport` holds (name, args)
        # two-tuples that a lot of tests assert on by equality, and widening
        # it would break every one of them.
        kw_calls = self.__dict__.setdefault("transport_kw", [])

        def record(*a, **k):
            calls.append((name, a))
            kw_calls.append((name, a, k))

        return record

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

class StubHudApp:
    """Records the renderer-facing calls the HUD lifecycle makes."""

    def __init__(self):
        self.calls = []
        self.on_nav = None
        self.on_hud = None
        self.on_hud_skip = None
        self.on_clipboard_error = None
        self.on_forward = None

    def invalidate(self):
        pass

    def set_active(self, active):
        self.calls.append(("active", active))

    def set_hud(self, on, opts=None):
        self.calls.append(("hud", on))
        self.hud_opts = opts

    def set_hud_skip(self, label):
        self.calls.append(("skip", label))

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

class MultiServerSource(FakeSource):
    def servers(self):
        return [{"uuid": "srv1", "name": "Home"},
                {"uuid": "srv2", "name": "Remote"}]

class DownloadsController(FakeController):
    """Controller whose downloads catalog has real hierarchy."""

    # Mirrors what downloads.group_downloads actually produces, including
    # watched/watched_count — the panel gates "Remove Watched" on the count,
    # so a fixture without it silently loses the button.
    TREE = [
        {"kind": "playlist", "id": "PL9", "title": "Road Trip", "size": 9000,
         "count": 120, "watched_count": 0, "children": []},
        {"kind": "series", "id": "sh1", "title": "The Show", "size": 3000,
         "count": 2, "watched_count": 1,
         "children": [
             {"kind": "season", "id": "se1", "series_id": "sh1",
              "title": "Season 1", "size": 3000, "count": 2,
              "watched_count": 1, "children": [
                  {"kind": "item", "id": "e1", "title": "Pilot",
                   "status": "complete", "size": 2000, "index": 1,
                   "done": 2000, "total": 2000, "watched": True},
                  {"kind": "item", "id": "e2", "title": "Second",
                   "status": "pending", "size": 1000, "index": 2,
                   "done": 0, "total": 1000, "watched": False}]}]},
        {"kind": "movies", "id": None, "title": "Movies & Videos", "size": 500,
         "count": 1, "watched_count": 0,
         "children": [{"kind": "item", "id": "m1", "title": "A Movie",
                       "status": "complete", "size": 500, "index": None,
                       "done": 500, "total": 500, "watched": False}]},
    ]

    def __init__(self):
        super().__init__()
        self.deleted = []
        self.deleted_watched_only = []

    def list_downloads(self):
        import copy
        return copy.deepcopy(self.TREE)

    def delete_download(self, item_id=None, series_id=None, season_id=None,
                        playlist_id=None, watched_only=False):
        self.deleted.append((item_id, series_id, season_id, playlist_id))
        self.deleted_watched_only.append(bool(watched_only))

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

class FakeThumbs:
    """Stands in for ThumbnailStore: records requests and lets the test
    decide how each one resolves."""

    def __init__(self):
        self.requests = []          # (key, url)
        self.gone = set()           # keys the "server" says don't exist
        self._cbs = {}              # key -> callback
        self._notify = None

    def set_notify(self, notify):
        self._notify = notify

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
