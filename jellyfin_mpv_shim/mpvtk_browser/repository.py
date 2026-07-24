"""Browse-only data access for the library browser.

``LibrarySource`` is the seam the UI depends on. Today it is backed by live
Jellyfin connections; a future offline build can provide an object with the same
method surface backed by the local sync catalog without touching the views.

Every method returns plain Jellyfin item DTO dicts (or lists of them) so the
same shapes work whether they came from the server or a local cache.
"""

import json
import logging
import os
import random

from concurrent.futures import ThreadPoolExecutor

from jellyfin_apiclient_python import JellyfinClient

from ..constants import USER_APP_NAME, CLIENT_VERSION, USER_AGENT
from ..i18n import _
from ..sync.db import SyncDB, STATUS_COMPLETE
from . import home_sections

log = logging.getLogger("mpvtk_browser.repository")

# Fields requested for grids/rows. Kept lean for speed. Artists is included so
# music tiles (e.g. tracks in a playlist) can show the performer.
LIST_FIELDS = "PrimaryImageAspectRatio,Overview,ProductionYear,Artists"

# Concurrent home-screen fetches. The rows are independent, so this is bounded
# only to keep a many-library server from opening a burst of connections at
# once — well above the usual library count, so in practice the whole home
# screen is two waves: /Views, then everything else.
HOME_FANOUT = 8

# Row "kind" for the offline home rows. Namespaces their scroll-container
# ids ("row-downloaded-0"); deliberately not one of home_sections' types,
# because the offline rows are not the server's configurable sections.
# tests/integration/test_e2e_offline.py focuses these ids by name.
OFFLINE_ROW_KIND = "downloaded"

# Fields for music browse (albums/artists/tracks): artist/album labels, track
# runtime for the tabular list, and counts for artist tiles.
MUSIC_FIELDS = ("PrimaryImageAspectRatio,Artists,Album,AlbumId,RunTimeTicks,"
                "ItemCounts,ProductionYear")

# Fields requested for the detail view. Intentionally a superset (MediaSources,
# MediaStreams, People, ...) so cached DTOs are already complete for the eventual
# offline-sync feature.
DETAIL_FIELDS = (
    "Path,Overview,Genres,Studios,People,Taglines,SortName,"
    "OfficialRating,CommunityRating,CriticRating,ProductionYear,"
    "MediaSources,MediaStreams,Chapters,ProviderIds,PremiereDate,"
    "PrimaryImageAspectRatio,DateCreated"
)

# CollectionTypes we do not surface (video-only browser, phase 1). Playlists
# ARE surfaced (as a normal library tile): Jellyfin lets a playlist's declared
# type and its contents diverge, so we can't classify a playlist as music/video
# up front — instead we show every playlist and filter its *contents* to
# supported types when opened (see PLAYLIST_SUPPORTED_TYPES).
# Collections (CollectionType "boxsets") are intentionally NOT excluded here:
# the server decides whether collections appear in the main browse / whether
# movies are grouped into collections for a library request. We render whatever
# it returns and only request collections explicitly via the Movies-library
# "Collections" toggle (get_movie_collections) — no client-side exclusion.
# "musicvideos" is NOT excluded: a MusicVideo is an ordinary video item
# (it is in PLAYABLE_TYPES and plays through the normal video player), so a
# music-video library browses and plays like any other video library.
EXCLUDED_COLLECTION_TYPES = {"books", "livetv"}

# Item types that open the detail/play view rather than drilling deeper.
PLAYABLE_TYPES = {"Movie", "Episode", "Video", "MusicVideo"}
# Item types that drill into a series view.
SERIES_TYPES = {"Series"}
# Live TV entries, which play immediately rather than opening a detail view.
# A Program plays its ChannelId, not its own id.
LIVE_TYPES = {"Program", "TvChannel"}
# Item types that drill into another grid (by ParentId).
FOLDER_TYPES = {"CollectionFolder", "Folder", "BoxSet", "Season", "UserView"}
# Item types shown inside a playlist. A playlist can mix in music/other entries;
# only these are surfaced (and downloaded). Audio is included so music
# playlists play, queue, and download (the now-playing bar drives them).
PLAYLIST_SUPPORTED_TYPES = {"Movie", "Episode", "Video", "Audio"}


class ServerConn:
    """A single browse-only connection to one Jellyfin server."""

    def __init__(self, info: dict, device_id: str, player_name: str, verify_ssl: bool):
        self.uuid = info["uuid"]
        self.name = info.get("name") or info.get("address")
        self.address = info["address"].rstrip("/")
        self.user_id = info["user_id"]
        self.token = info["token"]

        client = JellyfinClient(allow_multiple_clients=True)
        client.config.app(USER_APP_NAME, CLIENT_VERSION, player_name, device_id)
        client.config.data["http.user_agent"] = USER_AGENT
        client.config.auth(self.address, self.user_id, self.token, verify_ssl)
        # We already hold a valid token, so skip authenticate() and just bring up
        # the HTTP session. Browse-only: no websocket, no capability registration.
        client.logged_in = True
        # keep_alive defaults True, and that default is load-bearing: with it
        # off the apiclient tears the session down after every request, so
        # each browse call would pay a fresh TLS handshake. Leave it alone.
        client.start(websocket=False)

        self.client = client
        self.api = client.jellyfin

    def stop(self):
        try:
            self.client.stop()
        except Exception:
            log.debug("Error stopping browse client", exc_info=True)


class LibrarySource:
    """Live, multi-server browse data source.

    The UI is given one of these and addresses servers by ``uuid``. Methods
    raise on network errors; callers run them off the UI thread and surface
    failures in the view.
    """

    def __init__(self, servers, device_id: str, player_name: str, verify_ssl: bool):
        self._conns = {}
        self._order = []
        # uuid -> (layout, latest_excludes). Two small requests that every home
        # load needs before it can build its task list, so they are cached
        # rather than paid on every back-navigation. Refreshed whenever the
        # settings screen reads them, and rewritten on save.
        self._home_prefs = {}
        # uuid -> whether this server offers Live TV to this user. Derived for
        # free from the /Views response get_libraries already fetches; see
        # has_live_tv for why that answer is authoritative.
        self._has_live_tv = {}
        for info in servers:
            try:
                conn = ServerConn(info, device_id, player_name, verify_ssl)
            except Exception:
                log.error("Failed to connect browse client for %s", info.get("name"),
                          exc_info=True)
                continue
            self._conns[conn.uuid] = conn
            self._order.append(conn.uuid)

    # -- server enumeration ------------------------------------------------

    def servers(self):
        return [{"uuid": uuid, "name": self._conns[uuid].name} for uuid in self._order]

    def _conn(self, server_uuid) -> ServerConn:
        return self._conns[server_uuid]

    def stop(self):
        for conn in self._conns.values():
            conn.stop()

    # -- browsing ----------------------------------------------------------

    def get_libraries(self, server_uuid):
        api = self._conn(server_uuid).api
        result = api.get_views() or {}
        out = []
        has_live_tv = False
        for item in result.get("Items", []):
            if item.get("CollectionType") in EXCLUDED_COLLECTION_TYPES:
                # Noted on the way past rather than fetched separately: the
                # server adds this view only when the user may use Live TV AND
                # a tuner is configured (UserViewManager consults
                # LiveTvManager.GetEnabledUsers, which is
                # EnableLiveTvAccess && tuner hosts exist). So its presence
                # here is the whole gate, at no extra request — which matters
                # because Live TV sits in the stock home layout, and without a
                # gate every user without a tuner would pay for a row that can
                # never have anything in it.
                has_live_tv = has_live_tv or item.get("CollectionType") == "livetv"
                continue
            out.append(item)
        self._has_live_tv[server_uuid] = has_live_tv
        return out

    def has_live_tv(self, server_uuid):
        """Whether this server offers Live TV, per the last get_libraries.

        Defaults to False when views have not been read yet: the cost of
        being wrong in that direction is one missing row until the next home
        load, against a pointless request on every home load for the large
        majority of users who have no tuner.

        getattr rather than a bare attribute read, because a LibrarySource
        built without __init__ (as the home-row tests do) must still answer
        "no Live TV" rather than raise from inside the fan-out.
        """
        return (getattr(self, "_has_live_tv", None) or {}).get(
            server_uuid, False)

    # -- home screen layout (shared with jellyfin-web) ---------------------

    def _display_prefs_dto(self, api):
        """The raw DisplayPreferencesDto. There is no partial-update path on
        this API, so a save has to GET the whole document, mutate CustomPrefs
        and POST it back — dropping fields we do not understand would clobber
        jellyfin-web's other settings (landing screens, tvhome, ...)."""
        return api.get_user_settings(
            client=home_sections.DISPLAY_PREFS_CLIENT) or {}

    def get_home_prefs(self, server_uuid, refresh=False):
        """(layout, latest_excludes) for a server, cached.

        ``layout`` is the ordered list of section types; ``latest_excludes``
        is the set of library ids the user unchecked under "Display in home
        screen sections", which we must apply ourselves for the Recently Added
        rows (see get_home_rows).

        Never raises: an unreachable or ancient server falls back to the
        default layout with nothing excluded, because a home screen with the
        stock rows beats no home screen at all.
        """
        if not refresh and server_uuid in self._home_prefs:
            return self._home_prefs[server_uuid]
        api = self._conn(server_uuid).api
        try:
            prefs = self._display_prefs_dto(api).get("CustomPrefs") or {}
            layout = home_sections.resolve_layout(prefs)
        except Exception:
            log.warning("Failed to read home layout; using defaults",
                        exc_info=True)
            layout = list(home_sections.DEFAULT_LAYOUT)
        try:
            excludes = self.get_latest_excludes(server_uuid)
        except Exception:
            log.warning("Failed to read library home-screen exclusions",
                        exc_info=True)
            excludes = frozenset()
        self._home_prefs[server_uuid] = (layout, excludes)
        return layout, excludes

    def save_home_layout(self, server_uuid, layout):
        """Persist the section layout back to the server. Raises on failure —
        the settings screen reports it rather than pretending it saved."""
        api = self._conn(server_uuid).api
        dto = self._display_prefs_dto(api)
        custom = dict(dto.get("CustomPrefs") or {})
        custom.update(home_sections.layout_to_prefs(layout))
        dto["CustomPrefs"] = custom
        # The DTO's own Id/Client round-trip unchanged; the server keys off the
        # query string, which must match what jellyfin-web uses or we write a
        # preference set only this client can see.
        api._post("DisplayPreferences/%s" % home_sections.DISPLAY_PREFS_ID,
                  json=dto,
                  params={"userId": "{UserId}",
                          "client": home_sections.DISPLAY_PREFS_CLIENT})
        excludes = self._home_prefs.get(server_uuid, (None, frozenset()))[1]
        self._home_prefs[server_uuid] = (list(layout), excludes)

    def get_latest_excludes(self, server_uuid):
        """Library ids excluded from the home screen's generated rows.

        The server applies this itself for Continue Watching and Next Up, but
        only when the query carries no ParentId — and the Recently Added rows
        are deliberately one request *per library*, which bypasses it. So this
        set has to be applied client-side there, exactly as jellyfin-web does
        in recentlyAdded.ts.
        """
        api = self._conn(server_uuid).api
        user = api.get_user() or {}
        config = user.get("Configuration") or {}
        return frozenset(config.get("LatestItemsExcludes") or ())

    #: Row groups get_home_rows can fetch. "primary" is Continue Watching,
    #: Continue Listening and Next Up — the above-the-fold rows; "latest" is
    #: the per-library Latest rows, which sit below the fold and are the slow
    #: part (one call each).
    HOME_SECTIONS = ("primary", "latest")

    def get_home_rows(self, server_uuid, libraries=None, sections=None,
                      layout=None, latest_excludes=None):
        """Return the ordered rows shown on the home screen.

        Each row is ``{"title", "items", "collection_type", "slot", "kind"}``;
        empty rows are dropped so the home screen only shows what exists.
        ``libraries`` (the get_libraries result) drives the per-library
        "Latest" rows; passing it in avoids a second views fetch when the
        caller already has it.

        ``sections`` limits which groups are fetched (see HOME_SECTIONS), so
        the caller can draw the above-the-fold rows without waiting on the
        Latest fan-out. Defaults to everything.

        ``layout`` is the user's ordered section list (see home_sections);
        defaults to jellyfin-web's stock layout. ``slot`` on each row is its
        index in that layout, which is what lets the caller merge the two
        fetch batches back into the user's order — the batches no longer
        concatenate cleanly now that Latest need not be last.

        ``latest_excludes`` is the set of library ids to skip when building
        Latest rows; see get_latest_excludes for why it is applied here and
        not by the server.
        """
        # `is None`, not `or`: an explicitly empty selection means "fetch
        # nothing", which the falsy test would have turned into "fetch
        # everything" — the opposite of what the caller asked for.
        sections = tuple(self.HOME_SECTIONS if sections is None else sections)
        layout = (list(home_sections.DEFAULT_LAYOUT) if layout is None
                  else list(layout))
        latest_excludes = frozenset(latest_excludes or ())
        api = self._conn(server_uuid).api

        # Per-library "Latest in X" rows, like jellyfin-web's home screen
        # (replaces the old global Recently Added Movies/Episodes pair).
        if libraries is None:
            try:
                libraries = self.get_libraries(server_uuid)
            except Exception:
                log.warning("Failed to list libraries for latest rows",
                            exc_info=True)
                libraries = []

        def resume_row(title, collection_type=None, **extra):
            def fetch():
                params = {
                    "Recursive": True,
                    "Filters": "IsResumable",
                    "SortBy": "DatePlayed",
                    "SortOrder": "Descending",
                    "Limit": 20,
                    "Fields": LIST_FIELDS,
                    "EnableImageTypes": "Primary,Thumb,Backdrop",
                    # One tag per image type: without it every backdrop tag
                    # comes back, and items routinely carry five to ten.
                    "ImageTypeLimit": 1,
                    # The row is capped at 20 anyway, so the server's separate
                    # COUNT(*) over the whole library is pure waste
                    # (jellyfin-web passes this on all three home queries).
                    "EnableTotalRecordCount": False,
                }
                params.update(extra)
                # Deliberately no ParentId: that is what lets the server apply
                # the user's "Display in home screen sections" exclusions for
                # us. Scoping this by library would silently bypass them.
                resume = api.user_items(params=params) or {}
                return (title, resume.get("Items", []), collection_type)
            return fetch

        def video_resume_row():
            # No CollectionType: these mixed rows keep the item-type heuristic.
            return resume_row(_("Continue Watching"),
                              IncludeItemTypes="Movie,Episode,Video")()

        def audio_resume_row():
            # MediaTypes rather than IncludeItemTypes, matching jellyfin-web:
            # it catches Audio and AudioBook without enumerating types. The
            # music collection_type gives the row square art.
            return resume_row(_("Continue Listening"), collection_type="music",
                              MediaTypes="Audio")()

        def next_up_row():
            nextup = api.get_next(
                limit=20, fields=LIST_FIELDS,
                enable_image_types="Primary,Thumb,Backdrop") or {}
            return (_("Next Up"), nextup.get("Items", []), None)

        def live_tv_row():
            # jellyfin-web's Live TV home section is a row of nav buttons plus
            # an "On Now" strip; the strip is the part that lists anything, so
            # it is the part reproduced here.
            #
            # api._get rather than a helper: jellyfin-apiclient-python has
            # get_channels but nothing for Programs. ChannelInfo is what adds
            # ChannelName/ChannelPrimaryImageTag to each program, which is the
            # only art most guide data carries.
            #
            # Not jellyfin-web's separate limit=1 probe: an empty row is
            # already dropped by the comprehension below, so probing first
            # would only add a round trip to reach the same result.
            onnow = api._get("LiveTv/Programs/Recommended", {
                "IsAiring": True,
                "Limit": 24,
                "Fields": LIST_FIELDS + ",ChannelInfo",
                "EnableImageTypes": "Primary,Thumb,Backdrop",
                "ImageTypeLimit": 1,
                "EnableTotalRecordCount": False,
                "EnableUserData": False,
            }) or {}
            return (_("On Now"), onnow.get("Items", []), "livetv")

        def latest_row(lib):
            def fetch():
                # NOT api.get_recently_added: that helper hardcodes
                # Fields=info(), a 28-field payload including MediaSources,
                # People, Studios and RecursiveItemCount. MediaSources forces
                # per-item media-source resolution and the rest add joins —
                # for 16 items times every library, none of which this row
                # renders. LIST_FIELDS is what the other browse calls use.
                latest = api.user_items("/Latest", {
                    "ParentId": lib.get("Id"),
                    # Collapse recently-added episodes into their series poster
                    # (what jellyfin-web's "Recently Added" does) instead of
                    # listing bare episodes as landscape thumbnails.
                    "GroupItems": True,
                    "Limit": 16,
                    "Fields": LIST_FIELDS,
                    "EnableImageTypes": "Primary,Thumb,Backdrop",
                    "ImageTypeLimit": 1,
                    "EnableTotalRecordCount": False,
                })
                # /Latest answers with a bare list, not an Items dict.
                items = (latest.get("Items", []) if isinstance(latest, dict)
                         else (latest or []))
                return (_("Latest %s") % lib.get("Name", ""), items,
                        lib.get("CollectionType"))
            return fetch

        builders = {
            home_sections.RESUME: video_resume_row,
            home_sections.RESUME_AUDIO: audio_resume_row,
            home_sections.NEXT_UP: next_up_row,
            home_sections.LIVE_TV: live_tv_row,
        }

        # (slot, kind, callable). The slot travels with the row so the caller
        # can restore the user's order after merging the two fetch batches.
        # Sections we cannot draw (Live TV, recordings, books) simply have no
        # entry in STAGE and contribute no work.
        tasks = []
        for slot, kind in enumerate(layout):
            stage = home_sections.STAGE.get(kind)
            if stage is None or stage == "local" or stage not in sections:
                continue
            if kind == home_sections.LIVE_TV and not self.has_live_tv(server_uuid):
                # No tuner (or no access): the request could only ever answer
                # empty, and this section is in the stock layout, so skipping
                # it here is what keeps Live TV free for everyone not using it.
                continue
            if kind == home_sections.LATEST:
                # One request per library, so this is where the user's
                # exclusions have to be honoured — the ParentId these carry
                # stops the server from doing it.
                tasks += [(slot, kind, latest_row(lib)) for lib in libraries
                          if lib.get("CollectionType") != "playlists"
                          and lib.get("Id") not in latest_excludes]
            else:
                tasks.append((slot, kind, builders[kind]))
        if not tasks:
            return []

        # Fanned out, not walked. These were strictly serial, so the home
        # screen cost (2 + one per library) round trips end to end before it
        # could draw anything — six libraries meant eight sequential waits.
        # jellyfin-web issues the same set concurrently, which is most of why
        # it felt faster. Ordering is preserved by collecting in submit order,
        # so the rows do not shuffle by whichever server call wins.
        #
        # One requests.Session per server is shared across these; that is the
        # same pattern ThumbnailStore already uses for its own worker pool.
        rows = []
        with ThreadPoolExecutor(
                max_workers=min(HOME_FANOUT, max(1, len(tasks))),
                thread_name_prefix="home") as pool:
            futures = [(slot, kind, pool.submit(task))
                       for slot, kind, task in tasks]
            for slot, kind, future in futures:
                try:
                    rows.append((slot, kind) + future.result())
                except Exception:
                    # One dead row must not cost the whole home screen.
                    log.warning("Failed to load a home row", exc_info=True)

        # Carry each row's CollectionType so the home view can pick poster vs
        # landscape by library kind — a TV "Latest" row mixes Series and stray
        # Episodes, so scanning item types alone mis-classifies it.
        return [{"title": t, "items": i, "collection_type": c,
                 "slot": slot, "kind": kind}
                for slot, kind, t, i, c in rows if i]

    @staticmethod
    def _filter_params(filters):
        """Translate the UI's filter dict into Jellyfin query params."""
        params = {}
        if not filters:
            return params
        active = []
        if filters.get("unplayed"):
            active.append("IsUnplayed")
        if active:
            params["Filters"] = ",".join(active)
        if filters.get("favorite"):
            params["IsFavorite"] = "true"
        if filters.get("genre"):
            params["Genres"] = filters["genre"]
        if filters.get("year"):
            params["Years"] = str(filters["year"])
        letter = filters.get("letter")
        if letter == "#":
            params["NameLessThan"] = "A"
        elif letter:
            params["NameStartsWith"] = letter
        return params

    def get_library_items(self, server_uuid, parent_id, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100,
                          filters=None):
        api = self._conn(server_uuid).api
        params = {
            "ParentId": parent_id,
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": LIST_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary,Thumb,Backdrop",
        }
        params.update(self._filter_params(filters))
        result = api.user_items(params=params) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_movie_collections(self, server_uuid, sort_by="SortName",
                              sort_order="Ascending", start_index=0, limit=100,
                              filters=None):
        """Collections (BoxSets) as a paged grid, for the Movies-library
        Collections toggle. Server-wide and recursive (a BoxSet can gather
        items from several libraries), mirroring jellyfin-web's Collections
        view. Returns ``(items, total)`` like ``get_library_items``."""
        api = self._conn(server_uuid).api
        params = {
            "IncludeItemTypes": "BoxSet",
            "Recursive": True,
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": LIST_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary,Thumb,Backdrop",
        }
        params.update(self._filter_params(filters))
        result = api.user_items(params=params) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_person_items(self, server_uuid, person_id, start_index=0, limit=100,
                         sort_by="SortName", sort_order="Ascending"):
        """A person's filmography (movies + series they appear in)."""
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "PersonIds": person_id,
            "Recursive": True,
            "IncludeItemTypes": "Movie,Series",
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": LIST_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary,Thumb,Backdrop",
        }) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    # -- music browse ------------------------------------------------------

    def _music_items(self, server_uuid, include, parent_id, sort_by,
                     sort_order, start_index, limit, filters=None,
                     extra=None):
        api = self._conn(server_uuid).api
        params = {
            "ParentId": parent_id,
            "IncludeItemTypes": include,
            "Recursive": True,
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": MUSIC_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary",
        }
        if extra:
            params.update(extra)
        params.update(self._filter_params(filters))
        result = api.user_items(params=params) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_music_albums(self, server_uuid, parent_id, sort_by="SortName",
                         sort_order="Ascending", start_index=0, limit=100,
                         filters=None):
        return self._music_items(server_uuid, "MusicAlbum", parent_id, sort_by,
                                 sort_order, start_index, limit, filters)

    def get_songs(self, server_uuid, parent_id, sort_by="SortName",
                  sort_order="Ascending", start_index=0, limit=100,
                  filters=None):
        return self._music_items(server_uuid, "Audio", parent_id, sort_by,
                                 sort_order, start_index, limit, filters)

    def get_genre_albums(self, server_uuid, parent_id, genre_id,
                         sort_by="SortName", sort_order="Ascending",
                         start_index=0, limit=100, filters=None):
        return self._music_items(server_uuid, "MusicAlbum", parent_id, sort_by,
                                 sort_order, start_index, limit, filters,
                                 extra={"GenreIds": genre_id})

    def _artist_list(self, server_uuid, method_name, parent_id, sort_by,
                     sort_order, start_index, limit):
        api = self._conn(server_uuid).api
        method = getattr(api, method_name, None)
        if method is None:
            return [], 0
        result = method(params={
            "ParentId": parent_id,
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": MUSIC_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary",
        }) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_album_artists(self, server_uuid, parent_id, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100):
        return self._artist_list(server_uuid, "get_album_artists", parent_id,
                                 sort_by, sort_order, start_index, limit)

    def get_artists(self, server_uuid, parent_id, sort_by="SortName",
                    sort_order="Ascending", start_index=0, limit=100):
        return self._artist_list(server_uuid, "get_artists", parent_id,
                                 sort_by, sort_order, start_index, limit)

    def get_music_genres(self, server_uuid, parent_id):
        api = self._conn(server_uuid).api
        try:
            result = api.get_genres(parent_id,
                                    include_item_types="MusicAlbum") or {}
        except TypeError:
            result = api.get_genres(parent_id) or {}  # older apiclient
        return result.get("Items", [])

    def get_album_tracks(self, server_uuid, album_id):
        """An album's tracks in disc/track order (children of the album)."""
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "ParentId": album_id,
            "SortBy": "ParentIndexNumber,IndexNumber,SortName",
            "SortOrder": "Ascending",
            "Fields": MUSIC_FIELDS,
        }) or {}
        return result.get("Items", [])

    def get_artist_albums(self, server_uuid, artist_id):
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "AlbumArtistIds": artist_id,
            "IncludeItemTypes": "MusicAlbum",
            "Recursive": True,
            "SortBy": "PremiereDate,ProductionYear,SortName",
            "SortOrder": "Descending",
            "Fields": MUSIC_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary",
        }) or {}
        return result.get("Items", [])

    def get_items_by_ids(self, server_uuid, ids):
        """Fetch DTOs for a list of ids, returned in the requested order (the
        server's Ids query does not preserve order). For the queue display.

        Batched: a big queue's ids as one ``Ids=`` param overflows the server's
        request-URI limit (HTTP 414). A partial (failed) batch just leaves those
        rows without metadata rather than losing the whole list.
        """
        ids = [i for i in ids if i]
        if not ids:
            return []
        api = self._conn(server_uuid).api
        unique = list(dict.fromkeys(ids))  # de-dup, preserve order
        by_id = {}
        CHUNK = 100  # ~100 GUIDs stays well under the URI length limit
        for start in range(0, len(unique), CHUNK):
            chunk = unique[start:start + CHUNK]
            try:
                result = api.user_items(params={
                    "Ids": ",".join(chunk), "Fields": MUSIC_FIELDS,
                }) or {}
            except Exception:
                log.warning("Failed to fetch a metadata batch of %d items",
                            len(chunk), exc_info=True)
                continue
            for i in result.get("Items", []):
                by_id[i.get("Id")] = i
        # De-dup-safe: a queue can hold the same id twice; map each slot.
        return [by_id[i] for i in ids if i in by_id]

    def get_artist_songs(self, server_uuid, artist_id, limit=500):
        """All audio tracks by an artist (for Play/Shuffle/Add-to-playlist)."""
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "ArtistIds": artist_id,
            "IncludeItemTypes": "Audio",
            "Recursive": True,
            "SortBy": "AlbumArtist,Album,ParentIndexNumber,IndexNumber,SortName",
            "Limit": limit,
            "Fields": MUSIC_FIELDS,
        }) or {}
        return result.get("Items", [])

    def get_genre_songs(self, server_uuid, parent_id, genre_id, limit=500):
        """All audio tracks in a genre. parent_id may be None (server-wide)."""
        api = self._conn(server_uuid).api
        params = {
            "GenreIds": genre_id,
            "IncludeItemTypes": "Audio",
            "Recursive": True,
            "SortBy": "AlbumArtist,Album,ParentIndexNumber,IndexNumber,SortName",
            "Limit": limit,
            "Fields": MUSIC_FIELDS,
        }
        if parent_id:
            params["ParentId"] = parent_id
        result = api.user_items(params=params) or {}
        return result.get("Items", [])

    def get_instant_mix(self, server_uuid, item_id, limit=200):
        api = self._conn(server_uuid).api
        get = getattr(api, "get_instant_mix", None)
        if get is None:
            return []
        try:
            result = get(item_id, limit=limit) or {}
        except Exception:
            return []
        return result.get("Items", [])

    def get_genres(self, server_uuid, parent_id=None):
        """Genre names available under a library (for the filter picker)."""
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id) or {}
        return [g.get("Name") for g in result.get("Items", []) if g.get("Name")]

    def get_filter_values(self, server_uuid, parent_id=None):
        """Filter-picker values: {"genres": [...], "years": [...]}. Years need
        apiclient >= 1.15 (get_filters); on older versions the year picker is
        simply empty and the genre fallback path is used."""
        api = self._conn(server_uuid).api
        if hasattr(api, "get_filters"):
            try:
                result = api.get_filters(parent_id) or {}
                # Newest first, deduped, ints — the server returns them in
                # its own order, and the offline source compares against
                # ProductionYear directly.
                years = set()
                for y in result.get("Years") or []:
                    try:
                        years.add(int(y))
                    except (TypeError, ValueError):
                        continue
                return {"genres": result.get("Genres") or [],
                        "years": sorted(years, reverse=True)}
            except Exception:
                log.warning("Items/Filters failed; falling back to genres",
                            exc_info=True)
        return {"genres": self.get_genres(server_uuid, parent_id), "years": []}

    def get_similar(self, server_uuid, item_id, limit=12):
        """"More Like This" items (apiclient >= 1.15; empty list before)."""
        api = self._conn(server_uuid).api
        if not hasattr(api, "get_similar"):
            return []
        result = api.get_similar(item_id, limit=limit, fields=LIST_FIELDS) or {}
        return result.get("Items", [])

    def get_trailers(self, server_uuid, item_id):
        """Local trailer items for an item (playable like any other item)."""
        api = self._conn(server_uuid).api
        try:
            result = api.get_local_trailers(item_id) or []
        except Exception:
            log.debug("Local trailers unavailable for %s", item_id,
                      exc_info=True)
            return []
        return result.get("Items", []) if isinstance(result, dict) else result

    def search_people(self, server_uuid, term, limit=20):
        """People matching a search term (apiclient >= 1.15; empty before)."""
        api = self._conn(server_uuid).api
        if not hasattr(api, "get_persons"):
            return []
        result = api.get_persons(search_term=term, limit=limit) or {}
        return result.get("Items", [])

    def get_playlists(self, server_uuid, limit=300):
        """All video playlists, for the add-to-playlist picker."""
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "IncludeItemTypes": "Playlist",
            "Recursive": True,
            "SortBy": "SortName",
            "Limit": limit,
        }) or {}
        return result.get("Items", [])

    def get_collections(self, server_uuid, limit=300):
        """All user collections (BoxSets), for the add-to-collection picker."""
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "IncludeItemTypes": "BoxSet",
            "Recursive": True,
            "SortBy": "SortName",
            "Limit": limit,
        }) or {}
        return result.get("Items", [])

    def get_shuffle_ids(self, server_uuid, parent_id, limit=200):
        """Random playable item ids under a library, for shuffle play. The
        server does the shuffling (SortBy=Random) so the sample spans the whole
        library, not just the loaded pages."""
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "ParentId": parent_id,
            "Recursive": True,
            "IncludeItemTypes": "Movie,Episode,Video",
            "SortBy": "Random",
            "Limit": limit,
            "EnableImages": False,
        }) or {}
        return [i["Id"] for i in result.get("Items", []) if i.get("Id")]

    def get_playlist_items(self, server_uuid, playlist_id):
        """A playlist's items in playlist order (not sorted).

        Returns the raw contents; the view filters to supported media types so
        it can tell an empty playlist from one that only holds unsupported
        (e.g. music) entries.
        """
        api = self._conn(server_uuid).api
        result = api.get_playlist_items(playlist_id, fields=LIST_FIELDS) or {}
        return result.get("Items", [])

    def get_playlist(self, server_uuid, playlist_id):
        """A playlist's metadata (``OpenAccess`` visibility + shares), for the
        editor's Public/Private control. Returns {} if the server or apiclient
        is too old to expose it."""
        api = self._conn(server_uuid).api
        get = getattr(api, "get_playlist", None)
        if get is None:
            return {}
        try:
            return get(playlist_id) or {}
        except Exception:
            return {}

    def get_seasons(self, server_uuid, series_id):
        api = self._conn(server_uuid).api
        result = api.get_seasons(series_id) or {}
        return result.get("Items", [])

    def get_episodes(self, server_uuid, series_id, season_id):
        api = self._conn(server_uuid).api
        result = api.get_season(series_id, season_id) or {}
        return result.get("Items", [])

    def get_item(self, server_uuid, item_id):
        api = self._conn(server_uuid).api
        return api.get_item(item_id, fields=DETAIL_FIELDS)

    def get_series_queue(self, server_uuid, series_id, start_item_id=None, limit=100):
        """Episodes for a series in aired order, ACROSS seasons, optionally
        starting at ``start_item_id`` — this is how the play queue crosses
        season boundaries (mirrors jellyfin-web's getEpisodes with startItemId
        and no SeasonId)."""
        api = self._conn(server_uuid).api
        result = api.get_episodes(series_id, start_item_id=start_item_id,
                                  fields=LIST_FIELDS, limit=limit) or {}
        return result.get("Items", [])

    def get_next_up(self, server_uuid, series_id):
        """The next episode to watch for a series (resume or next unwatched)."""
        api = self._conn(server_uuid).api
        result = api.get_next(limit=1, series_id=series_id, fields=LIST_FIELDS,
                              enable_image_types="Primary,Thumb,Backdrop") or {}
        items = result.get("Items", [])
        return items[0] if items else None

    def search(self, server_uuid, term, limit=60):
        api = self._conn(server_uuid).api
        result = api.search_media_items(
            term=term,
            media="Movie,Series,Episode,Video,MusicArtist,MusicAlbum,Audio",
            limit=limit,
        ) or {}
        return result.get("Items", [])

    # -- images ------------------------------------------------------------

    def image_spec(self, item, image_type="Primary", width=280):
        """Resolve which (item_id, type, tag) actually carries the image.

        Falls back from an item's own image to its series/parent image so
        episodes and seasons still show art. Returns ``None`` when there is no
        usable image (caller shows a placeholder).
        """
        tags = item.get("ImageTags") or {}
        # In a poster (2:3) context, an episode's OWN Primary is a wide 16:9
        # still — borrow the series poster so recently-added specials/episodes
        # match the surrounding series tiles instead of letterboxing a landscape
        # thumb. Episode lists ask for "Thumb", so they keep their stills.
        if (image_type == "Primary" and item.get("Type") == "Episode"
                and item.get("SeriesId") and item.get("SeriesPrimaryImageTag")):
            return item["SeriesId"], "Primary", item["SeriesPrimaryImageTag"]
        if image_type in tags:
            return item["Id"], image_type, tags[image_type]

        if item.get("Type") == "Playlist":
            # A playlist has its own (square) Primary image — ask the server
            # for it directly rather than borrowing a member's poster. Asked
            # for even with no tag in the DTO, since the server generates
            # playlist art; a genuine miss is a 404 that the thumbnail store
            # records and stops retrying.
            return item["Id"], "Primary", tags.get("Primary") or "playlist"

        if image_type == "Thumb":
            # Fall back to a primary image, then the series thumb/primary.
            if "Primary" in tags:
                return item["Id"], "Primary", tags["Primary"]

        if item.get("PrimaryImageTag"):
            # People entries carry a bare PrimaryImageTag instead of ImageTags.
            return item["Id"], "Primary", item["PrimaryImageTag"]

        if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
            return item["SeriesId"], "Primary", item["SeriesPrimaryImageTag"]

        if item.get("ParentThumbItemId") and item.get("ParentThumbImageTag"):
            # Live TV programs inherit the channel's thumb this way.
            return (item["ParentThumbItemId"], "Thumb",
                    item["ParentThumbImageTag"])

        if item.get("ChannelId") and item.get("ChannelPrimaryImageTag"):
            # Last resort for a program: the channel logo. Guide data often
            # carries no art of its own, and a wall of letter glyphs reads as
            # broken — the logo at least identifies what is on.
            return (item["ChannelId"], "Primary",
                    item["ChannelPrimaryImageTag"])

        if item.get("AlbumId") and item.get("AlbumPrimaryImageTag"):
            return item["AlbumId"], "Primary", item["AlbumPrimaryImageTag"]

        if "Primary" in tags:
            return item["Id"], "Primary", tags["Primary"]

        return None

    def image_url(self, server_uuid, item_id, image_type, tag, width,
                  height=None, fill=False, index=None):
        # .get, not a bare index: image_url runs on the Tk thread from tile
        # lazy-loading, and a rebuilt source can have dropped this server while
        # a view still shows tiles keyed to it. Art just stops resolving.
        conn = self._conns.get(server_uuid)
        if conn is None:
            return None
        api = conn.api
        if fill and height:
            # Crop to the exact tile aspect so wide library/banner art still
            # reads as a uniform poster instead of a letterboxed thumbnail.
            return api.image_url(item_id, image_type, index=index, tag=tag,
                                 fill_width=int(width), fill_height=int(height))
        return api.image_url(item_id, image_type, index=index, tag=tag,
                             max_width=int(width))

    def chapter_image_url(self, server_uuid, item_id, chapter_index, chapter,
                          width=320):
        """URL for a chapter thumbnail, or None when the chapter has none."""
        tag = (chapter or {}).get("ImageTag")
        if not tag:
            return None
        return self.image_url(server_uuid, item_id, "Chapter", tag, width,
                              index=chapter_index)

    @staticmethod
    def backdrop_spec(item):
        """(owner_item_id, tag) identifying which backdrop image backdrop_url
        would serve — the cache key must carry the real tag, or the same item
        cached from another source/an older backdrop is served forever."""
        tags = item.get("BackdropImageTags") or []
        if tags:
            return item["Id"], tags[0]
        parent_tags = item.get("ParentBackdropImageTags") or []
        if parent_tags and item.get("ParentBackdropItemId"):
            return item["ParentBackdropItemId"], parent_tags[0]
        return None

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        spec = self.backdrop_spec(item)
        if spec is None:
            return None
        owner_id, tag = spec
        return self.image_url(server_uuid, owner_id, "Backdrop", tag,
                              width, height=height, fill=fill, index=0)


class _OfflineSnapshot:
    """One immutable, internally-consistent view of the offline catalog.

    reload() builds a complete snapshot and publishes it with a single
    attribute assignment, so a reader that grabbed ``self._snap`` never sees
    a torn mix of new and old state (reload runs on an api-pool thread while
    the Tk thread reads for artwork). Nothing mutates a snapshot's dicts
    after publish — except ``art_cache``, a memo of resolved artwork paths
    (safe: values are deterministic for the snapshot, so a racing double
    compute is idempotent)."""

    def __init__(self, rows=None, items=None, series_server=None,
                 season_server=None, season_series=None, playlists=None,
                 playlist_items=None, playlist_server=None):
        self.rows = rows or {}
        self.items = items or []
        self.series_server = series_server or {}
        self.season_server = season_server or {}
        self.season_series = season_series or {}
        self.playlists = playlists or []
        self.playlist_items = playlist_items or {}
        self.playlist_server = playlist_server or {}
        self.art_cache = {}


class OfflineLibrarySource:
    """LibrarySource-compatible browser backed by the offline catalog.

    Mirrors the normal browsing UI (libraries → grids → series → seasons →
    episodes) filtered to downloaded content, with artwork from local files.
    Reads the catalog read-only and caches rows in memory.
    """

    def __init__(self, catalog_path):
        self.catalog_path = catalog_path
        self.root = os.path.dirname(catalog_path) if catalog_path else None
        self._snap = _OfflineSnapshot()
        self.reload()

    def reload(self):
        rows = []
        playlists = []
        playlist_rows = {}  # playlist_id -> ordered list of download rows
        if self.catalog_path:
            # reload() runs from __init__ (BrowserApp._enter_offline): a corrupt
            # or unreadable catalog must degrade to an empty offline library, not
            # crash the browser window. SyncDB already tolerates a missing file.
            try:
                db = SyncDB(self.catalog_path, read_only=True)
                try:
                    rows = db.list(status=STATUS_COMPLETE)
                    playlists = db.list_playlists()
                    for pl in playlists:
                        playlist_rows[pl["playlist_id"]] = db.playlist_item_rows(
                            pl["playlist_id"])
                finally:
                    db.close()
            except Exception:
                log.warning("Failed to open offline catalog %s",
                            self.catalog_path, exc_info=True)
                rows, playlists, playlist_rows = [], [], {}
        # Build into locals, then publish ONE snapshot object in a single
        # assignment. reload() can run on a browser api-pool thread (a download
        # finished while browsing offline), so a concurrent reader must never
        # observe a half-populated list or a torn mix of attributes.
        by_id = {r["item_id"]: r for r in rows}
        items = []
        series_server = {}  # series_id -> server_id (for series artwork)
        season_server = {}  # season_id -> server_id (for season artwork)
        season_series = {}  # season_id -> series_id (artwork fallback)
        for row in rows:
            item = self._item_from_row(row)
            if item is not None:
                items.append(item)
            if row.get("type") == "Episode" and row.get("series_id"):
                series_server.setdefault(row["series_id"], row.get("server_id"))
                if row.get("season_id"):
                    season_server.setdefault(row["season_id"], row.get("server_id"))
                    season_series.setdefault(row["season_id"], row["series_id"])
        # Playlist DTOs + their ordered downloaded items (drop empties defensively;
        # list_playlists already requires ≥1 complete item).
        playlist_dtos, playlist_items, playlist_server = [], {}, {}
        for pl in playlists:
            pid = pl["playlist_id"]
            pl_items = [self._item_from_row(r) for r in playlist_rows.get(pid, [])]
            pl_items = [i for i in pl_items if i is not None]
            if not pl_items:
                continue
            playlist_dtos.append({"Id": pid, "Name": pl.get("name") or _("Playlist"),
                                  "Type": "Playlist", "ImageTags": {}})
            playlist_items[pid] = pl_items
            playlist_server[pid] = pl.get("server_id")
        self._snap = _OfflineSnapshot(
            rows=by_id, items=items, series_server=series_server,
            season_server=season_server, season_series=season_series,
            playlists=playlist_dtos, playlist_items=playlist_items,
            playlist_server=playlist_server)

    def stop(self):
        pass

    def servers(self):
        return [{"uuid": "offline", "name": _("Downloaded")}]

    # -- browsing ----------------------------------------------------------

    @staticmethod
    def _aggregate_userdata(episodes):
        """UserData for a synthesized Series/Season DTO, derived from its
        downloaded episodes. Without it the watched badge/label lies offline:
        ``is_watched`` falls back to UnplayedItemCount for these types, and a
        missing UserData reads as never-watched. Counts only what's downloaded
        — offline that IS the visible library."""
        unplayed = sum(1 for e in episodes
                       if not (e.get("UserData") or {}).get("Played"))
        return {"Played": unplayed == 0, "UnplayedItemCount": unplayed}

    def get_libraries(self, server_uuid):
        snap = self._snap
        libs = []
        if any(i.get("Type") == "Movie" for i in snap.items):
            libs.append({"Id": "offline:movies", "Name": _("Movies"),
                         "Type": "CollectionFolder", "CollectionType": "movies",
                         "ImageTags": {}})
        # Home videos (Type=Video) are their own section, not lumped in Movies.
        if any(i.get("Type") == "Video" for i in snap.items):
            libs.append({"Id": "offline:videos", "Name": _("Videos"),
                         "Type": "CollectionFolder", "CollectionType": "homevideos",
                         "ImageTags": {}})
        if any(i.get("Type") == "Episode" for i in snap.items):
            libs.append({"Id": "offline:tv", "Name": _("TV Shows"),
                         "Type": "CollectionFolder", "CollectionType": "tvshows",
                         "ImageTags": {}})
        if snap.playlists:
            libs.append({"Id": "offline:playlists", "Name": _("Playlists"),
                         "Type": "CollectionFolder", "CollectionType": "playlists",
                         "ImageTags": {}})
        return libs

    def _series_list(self, snap=None):
        snap = snap or self._snap
        episodes_by_series, names, order = {}, {}, []
        for item in snap.items:
            if item.get("Type") != "Episode":
                continue
            sid = item.get("SeriesId")
            if not sid:
                continue
            if sid not in episodes_by_series:
                episodes_by_series[sid] = []
                names[sid] = item.get("SeriesName") or _("Series")
                order.append(sid)
            episodes_by_series[sid].append(item)
        return [{"Id": sid, "Name": names[sid], "Type": "Series",
                 "ImageTags": {},
                 "UserData": self._aggregate_userdata(episodes_by_series[sid])}
                for sid in order]

    def get_home_prefs(self, server_uuid, refresh=False):
        """Signature parity with LibrarySource — see get_home_rows below for
        why parity matters here. There is no server to ask, and the offline
        rows are not the configurable ones, so this is always the default
        layout with nothing excluded."""
        return list(home_sections.DEFAULT_LAYOUT), frozenset()

    def get_home_rows(self, server_uuid, libraries=None, sections=None,
                      layout=None, latest_excludes=None):
        """Offline home rows.

        Every keyword is accepted for signature parity with LibrarySource, and
        must stay that way: _load_home fetches in two batches, and the offline
        source is what the failure path falls back TO. A TypeError here would
        fail that fallback, which re-triggers the fallback, which fails
        again — an unbounded retry loop rather than a degraded screen.

        ``layout`` and ``latest_excludes`` are ignored rather than honoured:
        these rows are "what you downloaded", not the server's configurable
        sections, so there is nothing for a section layout to reorder. They
        still carry ascending ``slot`` values so the caller's merge-by-slot
        works the same for both sources.

        Everything is local, so there is nothing to stagger: the whole set is
        returned for the primary batch and the latest batch adds nothing.
        """
        sections = tuple(LibrarySource.HOME_SECTIONS
                         if sections is None else sections)
        if "primary" not in sections:
            return []
        snap = self._snap
        rows = []
        movies = [i for i in snap.items if i.get("Type") == "Movie"]
        if movies:
            rows.append({"title": _("Downloaded Movies"), "items": movies,
                         "collection_type": "movies"})
        videos = [i for i in snap.items if i.get("Type") == "Video"]
        if videos:
            rows.append({"title": _("Downloaded Videos"), "items": videos,
                         "collection_type": "homevideos"})
        series = self._series_list(snap)
        if series:
            rows.append({"title": _("Downloaded Shows"), "items": series,
                         "collection_type": "tvshows"})
        for slot, row in enumerate(rows):
            row["slot"] = slot
            # Not a home_sections type: these are "what you downloaded", not
            # any of the server's configurable sections. The kind only
            # namespaces the row's scroll id, and calling these latestmedia
            # made the id read "row-latestmedia-0" for a row titled
            # "Downloaded Movies".
            row["kind"] = OFFLINE_ROW_KIND
        return rows

    @staticmethod
    def _apply_filters(items, filters):
        """Offline mirror of the server-side filter params. Genres live in the
        item_json snapshot (DETAIL_FIELDS includes them); synthesized series
        DTOs have none and simply drop out of a genre-filtered view."""
        if not filters:
            return items
        out = []
        for i in items:
            data = i.get("UserData") or {}
            if filters.get("unplayed") and data.get("Played"):
                continue
            if filters.get("favorite") and not data.get("IsFavorite"):
                continue
            if filters.get("genre") and filters["genre"] not in (
                    i.get("Genres") or []):
                continue
            if filters.get("year") and i.get("ProductionYear") != filters["year"]:
                continue
            letter = filters.get("letter")
            if letter:
                first = ((i.get("Name") or "?")[:1]).upper()
                if letter == "#":
                    if first.isalpha():
                        continue
                elif first != letter:
                    continue
            out.append(i)
        return out

    def get_library_items(self, server_uuid, parent_id, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100,
                          filters=None):
        snap = self._snap
        if parent_id == "offline:movies":
            items = [i for i in snap.items if i.get("Type") == "Movie"]
        elif parent_id == "offline:videos":
            items = [i for i in snap.items if i.get("Type") == "Video"]
        elif parent_id == "offline:tv":
            items = self._series_list(snap)
        elif parent_id == "offline:playlists":
            # Playlist tiles, name-sorted; contents keep playlist order via
            # get_playlist_items and must NOT be re-sorted here.
            items = sorted(snap.playlists,
                           key=lambda i: (i.get("Name") or "").lower())
            return items[start_index:start_index + limit], len(items)
        else:
            items = []
        items = self._apply_filters(items, filters)
        items = sorted(items, key=lambda i: (i.get("Name") or "").lower())
        return items[start_index:start_index + limit], len(items)

    def get_person_items(self, server_uuid, person_id, start_index=0, limit=100,
                         sort_by="SortName", sort_order="Ascending"):
        # People aren't cached offline; the person page simply comes up empty.
        return [], 0

    def get_genres(self, server_uuid, parent_id=None):
        genres = set()
        for i in self._snap.items:
            genres.update(i.get("Genres") or [])
        return sorted(genres)

    def get_filter_values(self, server_uuid, parent_id=None):
        years = {i.get("ProductionYear") for i in self._snap.items
                 if i.get("ProductionYear")}
        return {"genres": self.get_genres(server_uuid, parent_id),
                "years": sorted(years, reverse=True)}

    def get_similar(self, server_uuid, item_id, limit=12):
        return []  # no similarity data in the offline catalog

    def get_trailers(self, server_uuid, item_id):
        return []  # trailers aren't downloaded

    def search_people(self, server_uuid, term, limit=20):
        return []  # people aren't cached offline

    def get_playlists(self, server_uuid, limit=300):
        return list(self._snap.playlists)

    def get_collections(self, server_uuid, limit=300):
        return []  # collections aren't cached offline (editing is online-only)

    def get_shuffle_ids(self, server_uuid, parent_id, limit=200):
        snap = self._snap
        if parent_id == "offline:tv":
            pool = [i for i in snap.items if i.get("Type") == "Episode"]
        elif parent_id == "offline:movies":
            pool = [i for i in snap.items if i.get("Type") == "Movie"]
        elif parent_id == "offline:videos":
            pool = [i for i in snap.items if i.get("Type") == "Video"]
        else:
            pool = []
        ids = [i["Id"] for i in pool if i.get("Id")]
        random.shuffle(ids)
        return ids[:limit]

    def chapter_image_url(self, server_uuid, item_id, chapter_index, chapter,
                          width=320):
        return None  # chapter thumbnails aren't downloaded

    def get_playlist_items(self, server_uuid, playlist_id):
        """Downloaded items of a playlist, in playlist order."""
        return list(self._snap.playlist_items.get(playlist_id, []))

    def get_seasons(self, server_uuid, series_id):
        snap = self._snap
        seen, episodes_by_key, order = {}, {}, []
        for item in snap.items:
            if item.get("Type") != "Episode" or item.get("SeriesId") != series_id:
                continue
            key = item.get("SeasonId") or ("p%s" % item.get("ParentIndexNumber"))
            if key not in seen:
                pidx = item.get("ParentIndexNumber")
                if item.get("SeasonName"):
                    name = item["SeasonName"]
                elif pidx == 0:
                    name = _("Specials")
                elif pidx is not None:
                    name = _("Season %d") % pidx
                else:
                    name = _("Episodes")
                # SeriesId is load-bearing, not decoration: opening a Season
                # tile reads it to build the season route (see app.py's
                # item-type routing), and without it the route carried
                # series_id=None — which get_episodes filters against, so
                # every episode was discarded and the season read "Nothing
                # here yet." The live source gets this for free from the
                # server's own Season DTO.
                seen[key] = {"Id": item.get("SeasonId") or key, "Name": name,
                             "Type": "Season", "ImageTags": {},
                             "SeriesId": series_id,
                             "IndexNumber": pidx}
                episodes_by_key[key] = []
                order.append(key)
            episodes_by_key[key].append(item)
        for key in order:
            seen[key]["UserData"] = self._aggregate_userdata(episodes_by_key[key])
        # Match Jellyfin's online order: by season number ascending (Specials =
        # 0 first), with any unnumbered seasons last.
        return sorted((seen[k] for k in order),
                      key=lambda s: (s.get("IndexNumber") is None,
                                     s.get("IndexNumber") or 0))

    def get_episodes(self, server_uuid, series_id, season_id):
        # Seasons without a real SeasonId get a synthetic "p<ParentIndexNumber>"
        # id in get_seasons; match those back by ParentIndexNumber (a real
        # SeasonId is a hex GUID and never starts with "p").
        synthetic = isinstance(season_id, str) and season_id.startswith("p")
        eps = []
        for i in self._snap.items:
            if i.get("Type") != "Episode" or i.get("SeriesId") != series_id:
                continue
            if synthetic:
                if ("p%s" % i.get("ParentIndexNumber")) != season_id:
                    continue
            elif i.get("SeasonId") != season_id:
                continue
            eps.append(i)
        eps.sort(key=lambda i: (i.get("ParentIndexNumber") or 0,
                                i.get("IndexNumber") or 0))
        return eps

    @staticmethod
    def _item_from_row(row):
        """Build an item DTO from a catalog row, overlaying the LIVE UserData
        (downloads.userdata_json — updated by offline playback's periodic
        position record and watched marks) onto the item_json snapshot frozen
        at download time. Without the overlay, offline resume positions and
        watched state were written but never read back — playback always
        restarted from the beginning after a relaunch."""
        try:
            item = json.loads(row["item_json"])
        except (TypeError, ValueError):
            return None
        try:
            userdata = json.loads(row.get("userdata_json") or "{}")
        except (TypeError, ValueError):
            userdata = {}
        if userdata:
            merged = dict(item.get("UserData") or {})
            merged.update(userdata)
            # PlayedPercentage is derived; the live position is the truth.
            # Recompute it (a percentage seeded from the server at download
            # time or left in the snapshot would otherwise freeze the tile
            # progress bar), and drop it entirely when there is no resume
            # point (watched items show the badge, not a partial bar).
            pos = merged.get("PlaybackPositionTicks")
            runtime = row.get("runtime_ticks") or item.get("RunTimeTicks")
            if pos and runtime:
                merged["PlayedPercentage"] = min(pos / runtime * 100, 100.0)
            else:
                merged.pop("PlayedPercentage", None)
            item["UserData"] = merged
        return item

    def get_item(self, server_uuid, item_id):
        snap = self._snap
        row = snap.rows.get(item_id)
        if row and row.get("item_json"):
            item = self._item_from_row(row)
            if item is not None:
                return item
        # Synthesize a Series DTO so the series overview page renders offline.
        if item_id in snap.series_server:
            eps = [i for i in snap.items if i.get("SeriesId") == item_id
                   and i.get("Type") == "Episode"]
            name = next((i.get("SeriesName") for i in eps), _("Series"))
            return {"Id": item_id, "Name": name, "Type": "Series",
                    "ImageTags": {},
                    "UserData": self._aggregate_userdata(eps)}
        return None

    def get_series_queue(self, server_uuid, series_id, start_item_id=None, limit=100):
        eps = [i for i in self._snap.items
               if i.get("Type") == "Episode" and i.get("SeriesId") == series_id]
        eps.sort(key=lambda i: (i.get("ParentIndexNumber") or 0,
                                i.get("IndexNumber") or 0))
        if start_item_id:
            ids = [e.get("Id") for e in eps]
            if start_item_id in ids:
                eps = eps[ids.index(start_item_id):]
        return eps[:limit]

    def get_next_up(self, server_uuid, series_id):
        eps = self.get_series_queue(server_uuid, series_id)
        for ep in eps:
            if not (ep.get("UserData") or {}).get("Played"):
                return ep
        return eps[0] if eps else None

    def search(self, server_uuid, term, limit=60):
        needle = term.lower()
        return [i for i in self._snap.items
                if needle in (i.get("Name") or "").lower()][:limit]

    # -- images (local files) ---------------------------------------------

    @staticmethod
    def _name_for(image_type):
        kind = (image_type or "").lower()
        if kind.startswith("backdrop"):
            return "backdrop.jpg"
        if kind.startswith("thumb"):
            return "thumb.jpg"
        return "poster.jpg"

    def _in_dir(self, item_dir, name):
        path = os.path.join(item_dir, name)
        if os.path.exists(path):
            return path
        fallback = os.path.join(item_dir, "poster.jpg")
        return fallback if os.path.exists(fallback) else None

    def _art_path(self, item_id, image_type, snap=None):
        """Resolve an item's local artwork file. Memoized per snapshot: this
        runs on the Tk thread from tile lazy-loading, and each uncached call
        costs several os.path.exists probes (a real stutter source when the
        download folder lives on a network share). The memo dies with its
        snapshot, so a reload invalidates it automatically."""
        snap = snap or self._snap
        cache_key = (item_id, image_type)
        try:
            return snap.art_cache[cache_key]
        except KeyError:
            pass
        path = self._art_path_uncached(item_id, image_type, snap)
        snap.art_cache[cache_key] = path
        return path

    def _art_path_uncached(self, item_id, image_type, snap):
        if not self.root or not item_id:
            return None
        name = self._name_for(image_type)
        # Downloaded item (movie/episode).
        row = snap.rows.get(item_id)
        if row and row.get("file_path"):
            return self._in_dir(os.path.join(self.root,
                                             os.path.dirname(row["file_path"])), name)
        # Series artwork (cached separately from its episodes).
        if item_id in snap.series_server:
            series_dir = os.path.join(self.root,
                                      snap.series_server[item_id] or "server",
                                      "series", item_id)
            return self._in_dir(series_dir, name)
        # Season artwork, falling back to the series image when the season has
        # no specific artwork.
        if item_id in snap.season_server:
            season_dir = os.path.join(self.root,
                                      snap.season_server[item_id] or "server",
                                      "season", item_id)
            found = self._in_dir(season_dir, name)
            if found:
                return found
            series_id = snap.season_series.get(item_id)
            if series_id:
                return self._art_path(series_id, image_type, snap)
            return None
        # A playlist's own poster, cached at download time (a playlist has
        # its own image; borrowing a member's meant one member without art
        # blanked the whole tile).
        if item_id in snap.playlist_server:
            return self._in_dir(os.path.join(
                self.root, snap.playlist_server[item_id] or "server",
                "playlist", item_id), name)
        # Synthetic library previews use a representative download.
        if item_id == "offline:movies":
            return self._representative(("Movie",), snap)
        if item_id == "offline:videos":
            return self._representative(("Video",), snap)
        if item_id == "offline:tv":
            for series_id in snap.series_server:
                path = self._art_path(series_id, "Primary", snap)
                if path:
                    return path
            return self._representative(("Episode",), snap)
        if item_id == "offline:playlists":
            for pid in snap.playlist_server:
                path = self._art_path(pid, image_type, snap)
                if path:
                    return path
            return None
        return None

    def _representative(self, types, snap):
        for row in snap.rows.values():
            if row.get("type") in types and row.get("file_path"):
                path = self._in_dir(os.path.join(
                    self.root, os.path.dirname(row["file_path"])), "poster.jpg")
                if path:
                    return path
        return None

    def image_spec(self, item, image_type="Primary", width=280):
        if self._art_path(item.get("Id"), image_type):
            return item["Id"], image_type, "offline"
        return None

    def image_url(self, server_uuid, item_id, image_type, tag, width,
                  height=None, fill=False, index=None):
        return self._art_path(item_id, image_type)

    @staticmethod
    def backdrop_spec(item):
        """Cache-key spec matching LibrarySource.backdrop_spec. The "offline"
        sentinel keeps offline header art keyed apart from the online tags, so
        source switches can't serve each other's cached bitmaps."""
        return item.get("Id"), "offline"

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        return self._art_path(item.get("Id"), "Backdrop")
