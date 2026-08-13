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

def music_scroll(b, route, offset, maximum, scroll_id="music-grid"):
    """Scroll a windowed music route and let it ask for what that brings in.

    The tile tabs and the genre page are windowed since #617: there is no
    page-on-approach callback, so scrolling means moving the offset and
    rendering. Same shape as grid_scroll.
    """
    from jellyfin_mpv_shim.mpvtk_browser.pagination import Paginator
    Paginator.rewindow(route)     # what the view's on_scroll callback does
    b._scroll.on_scroll(scroll_id, offset, maximum)
    build_scene(b)


def music_songs_scroll(b, route, offset, maximum):
    """The SONGS tab's infinite scroll, which still appends on approach."""
    b._page_for(route)._on_scroll_end(offset, maximum)

def grid_scroll(b, route, offset, maximum):
    """Scroll a grid/person route and let it ask for what that brings in.

    The grid is *windowed* since #617: there is no page-on-approach callback
    any more, so scrolling it means moving the offset and rendering — the
    render is what asks ``Paginator.window`` for the newly visible range,
    from the same geometry the renderer composites.
    """
    from jellyfin_mpv_shim.mpvtk_browser.pagination import Paginator
    Paginator.rewindow(route)     # what the view's on_scroll callback does
    b._scroll.on_scroll("grid", offset, maximum)
    build_scene(b)

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
        #: Rows the "latest" batch adds. Kept apart from home_rows because
        #: HomePage.load fetches the two in stages and publishes between
        #: them; a fake that answered the same list for both made the whole
        #: staging invisible to the tests.
        self.home_latest_rows = [
            {"title": "Latest Movies", "items": [
                {"Id": "m2", "Name": "Beta", "Type": "Movie",
                 "ProductionYear": 2002}],
             "collection_type": "movies"},
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
        #: EnableContentDownloading, as can_download answers it.
        self.download_allowed = True
        #: item id -> DTO, consulted by get_item before its default. The
        #: book screens are the first to care what get_item returns for
        #: something that is not a movie.
        self.items = {}
        #: Whether an item is treated as HAVING backdrop artwork. This
        #: answered None unconditionally, which did not leave the
        #: has-artwork header untested -- it made it *unreachable* while
        #: every header test reported a pass, because a header that will
        #: get a banner lays out differently from one that never will
        #: (the heading is baked into the bitmap, so it must not also be
        #: drawn below it). `tools/audit_fake_contracts.py` cannot see
        #: this: `backdrop_spec` is provided, just never honestly.
        self.has_backdrop = False
        #: Whether items resolve to a Primary image. See image_spec: off by
        #: default so tiles stay placeholders, on for the tests that need
        #: the header's inset poster to exist at all.
        self.has_poster = False

    def servers(self):
        return [{"uuid": "srv1", "name": "Home Server"}]

    def get_libraries(self, server_uuid):
        return list(self.libraries)

    def get_home_prefs(self, server_uuid, refresh=False):
        return list(home_sections.DEFAULT_LAYOUT), frozenset()

    def get_home_rows(self, server_uuid, libraries=None, sections=None,
                      layout=None, latest_excludes=None):
        # sections is ("primary",) or ("latest",) -- see HomePage.load. None
        # means "everything", which is what the non-staged callers ask for.
        if sections is None:
            return list(self.home_rows) + list(self.home_latest_rows)
        if "latest" in sections:
            return list(self.home_latest_rows)
        return list(self.home_rows)

    def get_library_items(self, server_uuid, parent_id, start_index=0,
                          sort_by="SortName", sort_order="Ascending",
                          limit=100, filters=None, image_type=None,
                          collection_type=None):
        # Recorded, not swallowed. This took **kw and discarded sort/filters
        # entirely, so every filter, sort, unplayed-toggle and letter-jump
        # test asserted only on the browser's own scratch dict — if the view
        # stopped passing filters= to the source, all of them stayed green
        # and every filter in the app silently did nothing. image_type is
        # recorded for the same reason: it is what makes a Banner view show
        # banners, and a fake that dropped it would hide the whole feature.
        self.queries.append({
            "parent_id": parent_id, "start_index": start_index,
            "sort_by": sort_by, "sort_order": sort_order,
            "filters": dict(filters or {}), "image_type": image_type,
            "collection_type": collection_type,
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

    def get_filter_values(self, server_uuid, parent_id=None,
                          collection_type=None):
        self.filter_value_calls = getattr(self, "filter_value_calls", [])
        self.filter_value_calls.append((parent_id, collection_type))
        return {"genres": ["Action", "Comedy"], "years": [2020, 2021]}

    def get_shuffle_ids(self, server_uuid, parent_id, limit=200):
        return ["g0", "g5", "g9"]

    def get_play_all_ids(self, server_uuid, parent_id, sort_by="SortName",
                         sort_order="Ascending", filters=None, limit=200,
                         collection_type=None):
        # Echoes what it was asked with, so a caller that drops the sort or
        # the filters is visible rather than merely untested.
        self.play_all_args = (parent_id, sort_by, sort_order, filters)
        self.play_all_ctype = collection_type
        return ["g0", "g1", "g2"]

    def image_spec(self, item, image_type="Primary", width=280,
                   inherit=True):
        """Which image an item resolves to, or None.

        Off by default so tiles stay placeholders and nothing reaches the
        network -- but *switchable*, because answering None unconditionally
        is how a path goes untested while every test still passes. That is
        what `has_backdrop` was added for next door, after no shell test had
        ever rendered a header with artwork; the header's inset poster (#7)
        reads this one and would have had the same hole.
        """
        if not self.has_poster:
            return None
        return (item.get("Id") or "x", image_type, "ptag0")

    def image_url(self, *a, **k):
        # A url only when there is artwork to fetch, for the same reason
        # backdrop_url below has that rule: _request_image bails on a falsy
        # url before recording anything, so always answering None leaves
        # the request path unreachable.
        return "http://fake/img.jpg" if self.has_poster else None

    def backdrop_spec(self, item):
        if not self.has_backdrop:
            return None
        return (item.get("Id") or "x", "Backdrop", "tag0")

    def backdrop_url(self, *a, **k):
        # A URL only when there is artwork to fetch: `_request_image` bails
        # on a falsy url before it records anything, so a fake that always
        # answered None left the request path unreachable too.
        return "http://s/backdrop" if self.has_backdrop else None

    def get_item(self, server_uuid, item_id):
        if item_id in self.items:
            return dict(self.items[item_id])
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

    def can_download(self, server_uuid):
        """`EnableContentDownloading`. Real, not a stub returning True: for a
        book this is the difference between a Read button and an explanation,
        and a fake that could only say yes would leave the refusal path with
        nowhere to be observed."""
        return self.download_allowed

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
        # SeriesName, because the season screen puts it in the title bar
        # and every Season stand-in omitting it left that path untestable
        # -- the field the feature is named after had nowhere to live.
        return [{"Id": "se1", "Name": "Season 1", "Type": "Season",
                 "SeriesId": series_id, "SeriesName": "A Show"},
                {"Id": "se2", "Name": "Season 2", "Type": "Season",
                 "SeriesId": series_id, "SeriesName": "A Show"}]

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

    def search_artists(self, server_uuid, term, limit=100):
        # Its own request in the real source too: /Items does not answer
        # with artists reliably, so the search page asks /Artists.
        return [{"Id": "ar1", "Name": "Artist", "Type": "MusicArtist"}]

    def get_music_albums(self, server_uuid, parent_id, **kw):
        return ([{"Id": "al%d" % i, "Name": "Album %d" % i,
                  "Type": "MusicAlbum"} for i in range(4)], 4)

    def get_album_artists(self, server_uuid, parent_id, **kw):
        return ([{"Id": "ar1", "Name": "Artist", "Type": "MusicArtist"}], 1)

    def get_artists(self, server_uuid, parent_id, **kw):
        return ([{"Id": "ar2", "Name": "Artist 2", "Type": "MusicArtist"}], 1)

    def get_songs(self, server_uuid, parent_id, start_index=0, limit=100,
                  **kw):
        # start_index honoured, because the songs tab is windowed and its
        # play action asks the server from the clicked row (see
        # MusicLibraryPage._play_songs_from). A fake that ignored it would
        # make that indistinguishable from playing the first page.
        total = 5
        return ([{"Id": "so%d" % i, "Name": "Song %d" % i, "Type": "Audio",
                  "IndexNumber": i + 1}
                 for i in range(start_index,
                                min(total, start_index + limit))], total)

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
    running everything inline.

    ``release`` completes *one* job by position, which is what makes an
    interleaving expressible rather than just a delay: two jobs in flight and
    the second answering first is the ordinary case for a small query racing a
    large one, and it is the order that breaks a list. With _SyncPool (submit
    == run) no browser suite had ever had two jobs in flight at once.
    """

    def __init__(self):
        self.queued = []

    def submit(self, fn, *a, **k):
        self.queued.append((fn, a, k))

    def release(self, index=0):
        """Run one queued job. Returns False if there was nothing there, so a
        test that expected work to be in flight can say so."""
        if not self.queued or index >= len(self.queued):
            return False
        fn, a, k = self.queued.pop(index)
        fn(*a, **k)
        return True

    def release_last(self):
        return self.release(len(self.queued) - 1)

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
        #: item_id -> (status, absolute path or None), as the real gateway
        #: answers. Tests set entries to put a book on disk.
        self.book_downloads = {}
        self.opened = []
        self.open_result = (True, "fake")
        self.deleted_downloads = []
        self.enqueued = []
        #: item_id -> UserData dict, the server's side of the progress
        #: push/pull. Writable, so a push really does change what a pull
        #: reads back -- a fake that recorded the write without applying it
        #: could not show the dialog re-reading after a save.
        self.positions = {}
        self.positions_written = []
        self.set_position_ok = True
        #: What a reader asked to have recorded, whether or not the server
        #: took it. Separate from positions_written because the two answer
        #: different questions: that one is "did the server get it", this
        #: one is "did the reader report at all", and an offline reader
        #: still has to do the second.
        self.reading_positions = []
        #: Everything handed to the clipboard, in order. Recorded rather
        #: than dropped: what the reader copied is the only observable the
        #: copy menu has, and a fake that returned success without keeping
        #: the text would pass a test that copied the wrong paragraph.
        self.copied = []
        #: What ``copy_text`` answers: ``(ok, method, path)``. Settable, so
        #: a test can drive the "no clipboard on this box, saved to a file"
        #: message as well as the happy one.
        self.copy_result = (True, "fake", None)
        #: The comic reader's side of the window: what was handed to mpv to
        #: display, and every view change asked for. Recorded rather than
        #: dropped -- "which page is on screen" and "where is it" have no
        #: other observable, because the picture is mpv's and not a node in
        #: any scene this suite can read.
        self.pictures = []
        self.picture_views = []
        self.pictures_cleared = 0
        self.picture_views_reset = 0

    def show_picture(self, path):
        self.pictures.append(path)
        return True

    def clear_picture(self):
        self.pictures_cleared += 1

    def reset_picture_view(self):
        self.picture_views_reset += 1

    def set_picture_view(self, zoom=None, pan_x=None, pan_y=None):
        self.picture_views.append({"zoom": zoom, "pan_x": pan_x,
                                   "pan_y": pan_y})

    def copy_text(self, text):
        self.copied.append(text)
        return self.copy_result

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
        # In a parallel list, not appended to the tuple above: a lot of
        # tests assert on `played` by equality. Recorded at all because it
        # is load-bearing on its own -- resuming an audiobook is an index
        # AND an offset, and a fake that dropped the second would let a
        # resume that restarts the chapter pass a test named for it.
        self.__dict__.setdefault("play_offsets", []).append(offset_ticks)
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

    # -- books -------------------------------------------------------------
    #
    # Declared rather than left to __getattr__, which returns None for
    # everything. These four are *read*, not just called: the book page
    # unpacks a (status, path) pair and the progress dialog reads a UserData
    # blob out of one. A recorder returning None makes both raise inside a
    # try/except and leaves the screen looking exactly as it does when
    # nothing is downloaded -- so a test named "the button says Remove
    # Download" could never fail. That is the stand-in failure mode
    # tools/audit_fake_contracts.py exists for.

    def download_enqueue(self, server_uuid, item_id, item_type,
                         include_watched=False):
        """Queue a download AND write the row.

        Recording the call without writing a row is the `FakeManager.enqueue`
        failure this project has already had once: every later read sees a
        virgin catalog, so nothing that depends on the enqueue having
        happened -- a button changing, a pending open surviving -- can be
        observed at all.
        """
        self.enqueued.append((server_uuid, item_id, item_type,
                              include_watched))
        status, path = self.book_downloads.get(item_id, (None, None))
        if status is None and path is None:
            # No row yet -- which is what an entry of (None, None) means,
            # not merely a missing key. Seeding one is the whole point:
            # `is_complete` short-circuits a real enqueue, everything else
            # gets a PENDING row.
            self.book_downloads[item_id] = ("pending", None)

    def downloaded_ids(self):
        """(items, series, seasons, playlists), as the real gateway answers.

        Real rather than left to __getattr__, which returns None: the
        browser unpacks this into four arguments, so a recorder makes the
        refresh raise -- and everything else driven by the same
        notification (pending book opens, a page re-reading its own state)
        silently stopped happening behind it.
        """
        return (set(self.book_downloads), set(), set(), set())

    def book_download_state(self, item_id):
        return self.book_downloads.get(item_id, (None, None))

    def open_downloaded_file(self, item_id):
        self.opened.append(item_id)
        return self.open_result

    def delete_downloads(self, item_ids):
        self.deleted_downloads.append(list(item_ids))

    def get_position(self, server_uuid, item_id):
        return self.positions.get(item_id)

    def record_reading_position(self, server_uuid, item_id, ticks):
        """A reader's cursor. Declared rather than left to __getattr__:
        that would record the call and return a Mock-ish None, so nothing
        below could tell a page turn that reported from one that did not.
        The real one also writes the catalog and queues on refusal, which
        is tested against a real catalog in test_reading_position.py."""
        self.reading_positions.append((item_id, int(ticks)))
        return self.set_position(server_uuid, item_id, ticks)

    def set_position(self, server_uuid, item_id, ticks):
        if self.set_position_ok:
            self.positions.setdefault(item_id, {})["PlaybackPositionTicks"] \
                = int(ticks)
        self.positions_written.append((item_id, int(ticks)))
        return self.set_position_ok

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
        #: ``(is_on, is_auto)``, as PlayerManager.deinterlace_state answers
        #: it. Declared rather than left to __getattr__ for the reason the
        #: class docstring gives -- a recorder answers None, `_ctl_get`
        #: substitutes its default, and the gear row is then permanently
        #: unticked whatever the player is doing. Mutable, so a test can
        #: put the row in each of its three states (off, forced on, auto).
        self.deinterlace_answer = (False, False)
        self.chapter_list = [
            {"title": "Opening", "time": 0.0},
            {"title": "Middle", "time": 40.0},
            {"title": "End", "time": 80.0},
        ]
        self.player_stats_blob = {
            "hwdec": "no", "vo": "gpu-next", "fps": 23.974,
            "drops_vo": 0, "drops_dec": 3, "avsync": -0.012,
            "buffered": 42.5, "cache_speed": 1_500_000,
        }
        self.playback_info_blob = {
            "title": "Movie", "item_type": "Movie", "media_type": "Video",
            "play_method": "Transcode",
            "transcode_reasons": ["VideoCodecNotSupported"],
            "direct_path": False, "offline": False,
            "aid": 1, "sid": None,
            "source": {
                "Container": "mkv", "Size": 8400000000,
                "Path": "/media/Films/Film (2017)/film.mkv",
                "MediaStreams": [
                    {"Type": "Video", "Index": 0, "Codec": "hevc",
                     "Width": 3840, "Height": 2160, "BitDepth": 10},
                    {"Type": "Audio", "Index": 1, "Codec": "truehd",
                     "Channels": 8, "ChannelLayout": "7.1",
                     "IsDefault": True},
                    {"Type": "Subtitle", "Index": 2, "Codec": "subrip",
                     "Language": "eng", "IsExternal": True},
                ],
            },
        }

    def use_hud(self):
        return True

    def deinterlace(self):
        return self.deinterlace_answer

    def toggle_deinterlace(self):
        """Record it AND flip the state, like the real one does.

        Recording alone would leave the answer above frozen, so nothing
        could show the row's tick following the toggle -- and "the menu
        agreed that it worked" is most of what this control has to get
        right.
        """
        on, auto = self.deinterlace_answer
        self.deinterlace_answer = (not on, auto)
        self.transport.append(("toggle_deinterlace", ()))

    def hud_menu_state(self):
        return self.menu_state

    def chapters(self):
        return list(self.chapter_list)

    def player_stats(self):
        """Live mpv counters, as the real gateway answers them.

        Populated rather than empty, and with a *software* decoder and real
        drop counts, because the rows this feeds only exist when mpv has
        something to say -- a fake answering {} leaves every one of them
        with nowhere to live while the tests of them still pass.
        """
        return dict(self.player_stats_blob)

    def playback_info(self):
        """What the playback-info panel reads.

        A *transcode* by default, and with real streams on it, because the
        rows that only exist in that state (the reasons, the play method's
        qualifier) are the ones the panel was added for -- a fake answering
        DirectPlay with no streams leaves them with nowhere to live and
        every test of them passing against an empty panel.
        """
        return dict(self.playback_info_blob)

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

    def focus(self, node_id=None):
        # None is "the node this scene nominates" (a page's Play button);
        # an id is a specific node (the chrome's search box).
        self.calls.append(("focus", node_id))

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

    #: Which groups sit behind "Show advanced settings". Modelled because
    #: the renderer reads it: it used to key off a group being *called*
    #: "Advanced", and a stand-in without this makes every group look
    #: ordinary -- the disclosure then never renders and the test named
    #: after it passes against a form that has no disclosure at all.
    ADVANCED_GROUPS = frozenset({"Advanced"})

    def sections(self, tab=None):
        # The real one splits its groups across the General/Browse/Playback
        # tabs and takes which one to draw. This stand-in keeps one group on
        # General and returns nothing for the other two, which is enough for
        # a form test and keeps "an empty tab renders" exercised.
        if tab in ("browse", "playback"):
            return []
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
        #: Decoded images the store is holding. The real one keeps these in a
        #: byte-bounded LRU and the renderer reads *through* it, so a
        #: stand-in whose get_cached is a bare `return None` cannot show a
        #: cached image being drawn — or an evicted one being re-fetched,
        #: which is the failure mode that goes with it.
        self.cached = {}
        #: key -> the (w, h) box the caller asked the image to be decoded
        #: into. The real store applies it with `Image.thumbnail`, i.e.
        #: CONTAIN -- so this is what decides how much of the artwork
        #: survives, and a stand-in that dropped it (which this did) makes
        #: every "is the picture big enough" question unaskable while every
        #: test still passes. See BannerResolutionTest.
        self.boxes = {}

    def set_notify(self, notify):
        self._notify = notify

    def get_cached(self, key):
        return self.cached.get(key)

    def is_gone(self, key):
        return key in self.gone

    def request(self, key, url, box, callback):
        self.requests.append((key, url))
        self.boxes[key] = tuple(box)
        self._cbs[key] = callback

    def resolve(self, key, image):
        """Deliver a result the way pump() does — including failures.

        pump() files the image before calling back, and the order matters:
        the callback releases the dedup marker, so an image that was not in
        the cache by then would be requested again on the next frame.
        """
        if image is not None:
            self.cached[key] = image
        self._cbs.pop(key)(image)

    def evict(self, key):
        """Drop a decoded image the way the LRU's byte budget does, without
        touching what the renderer knows about it."""
        self.cached.pop(key, None)

    def trim_memory(self, max_bytes=None):
        """What a screen change does. Modelled rather than counted: a
        stand-in that increments `self.trims += 1` proves the call happened
        and nothing about whether anything was released — which is exactly
        how an unbounded second owner of every decoded image went unnoticed.
        """
        self.trims = getattr(self, "trims", 0) + 1
        if not max_bytes:
            self.cached.clear()

    def set_auth(self, *_a, **_kw):
        pass

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
