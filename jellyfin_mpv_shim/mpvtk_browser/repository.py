"""Browse-only data access for the library browser.

``LibrarySource`` is the seam the UI depends on. Today it is backed by live
Jellyfin connections; a future offline build can provide an object with the same
method surface backed by the local sync catalog without touching the views.

Every method returns plain Jellyfin item DTO dicts (or lists of them) so the
same shapes work whether they came from the server or a local cache.
"""

import datetime
import json
import logging
from urllib.parse import urlparse
import os
import random

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Optional

from jellyfin_apiclient_python import JellyfinClient

from ..books import AUDIOBOOK_TYPE, BOOK_TYPE
from ..constants import USER_APP_NAME, CLIENT_VERSION, USER_AGENT
from ..i18n import _
from ..sync.db import SyncDB, STATUS_COMPLETE
from . import home_sections
from . import live_tv
from . import user_prefs
from . import view_prefs


def _iso_utc(when):
    """An aware datetime as the ISO 8601 the Live TV endpoints require.

    With the ``Z``, always: the server binds these dates with
    ``AdjustToUniversal``, which accepts an offset-less string *without*
    shifting it — so a bare ``str(datetime)`` queries a window that is out by
    the local UTC offset and answers successfully with the wrong programmes.
    """
    return when.astimezone(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ")

log = logging.getLogger("mpvtk_browser.repository")

# Fields requested for grids/rows. Kept lean for speed.
#
# Only names in the server's ItemFields enum belong here. Everything a tile
# actually renders beyond these — ProductionYear, Artists, Album, RunTimeTicks,
# the ratings — is an unconditional BaseItemDto property that DtoService sets
# whether or not it was asked for, so listing it achieves nothing. The comma
# binder drops names it cannot parse instead of rejecting the request, which is
# why doing so was invisible; a stricter server would 400 the whole query.
#
# MediaSourceCount is the exception that has to be asked for, and it is the
# whole of the multi-version indicator: a Video's DTO carries the count only
# under this field, and the server omits it entirely when it is 1 -- so an
# absent value means "one version", not "not asked for". jellyfin-web puts it
# on every grid and row query for the same reason, and it costs nothing extra
# to answer (it is the length of the item's own alternate-version lists, not a
# media-source resolution -- that is MediaSources, which DETAIL_FIELDS pays
# for and a browse query must not).
LIST_FIELDS = "PrimaryImageAspectRatio,Overview,MediaSourceCount"

#: An item's own artwork counts as landscape at or above this, which is what
#: lets `backdrop_spec` use a home video's extracted still for its header and
#: refuse a film's poster. Square is the boundary: anything taller is key art
#: and a banner crop through the middle of it is worse than no banner at all.
#: Same threshold as TileRenderer.SQUARE_RATIO, kept here because the source
#: cannot import the renderer.
_LANDSCAPE_ART = 0.8

# Fields for a library GRID, which is the one place a hundred items arrive at
# once. Overview is a third of that response body (154KB -> 108KB for a
# hundred series here) and a tile draws a name, a year and a runtime -- none
# of them fields. Rows keep LIST_FIELDS: they are twelve to twenty items and
# a clicked one seeds a page that shows the text while the real DTO loads.
# jellyfin-web's grid asks for the aspect ratio only when the view is Primary;
# MediaSourceCount it asks for everywhere, and so do we -- see LIST_FIELDS.
GRID_FIELDS = "PrimaryImageAspectRatio,MediaSourceCount"

#: ...and what a BOOKS grid asks for on top.
#:
#: Overview is back, for the one library where the argument above does not
#: apply: a books folder is a book -- three parts, or eight chapters -- not
#: a hundred series, and its description is drawn *on that screen* rather
#: than on a page you click through to. An audiobook's description lives in
#: the file's tags, so it is on the chapter items and on nothing else; the
#: folder does not have it unless someone wrote an .nfo. Without this the
#: only place a book's description could come from is a second request per
#: folder open.
BOOKS_GRID_FIELDS = GRID_FIELDS + ",Overview"

# What a library's grid lists, by collection type -- jellyfin-web's default
# tab for that view (LibraryTab.Movies -> Movie, and so on).
#
# Naming the type is a performance fix as much as a parity one. Without it the
# query is answered with the library's own folders too, and the server builds
# UserData for a Folder by walking everything under it: 8.0s for one page of a
# 1334-film library against a real server here, 0.3s once the folders are out
# of it. The recursion is what keeps the *contents* the same afterwards --
# films inside those folders are still listed, just as films.
#
# Absent means "list this folder exactly as it is": Home Videos, photos and
# mixed libraries are browsed BY folder, and flattening them would destroy
# the only structure they have. Web does the same (its Folders tab passes
# recursive: false and no type filter). Only a library ROOT carries a
# collection type -- a folder inside one has none, see MpvtkBrowser.open_item
# -- so this can never flatten a folder the user opened.
LIBRARY_ITEM_TYPES = {
    "movies": "Movie",
    "tvshows": "Series",
    "music": "MusicAlbum",
    "musicvideos": "MusicVideo",
    "boxsets": "BoxSet",
}

# Image types every browse query asks for. Primary is the artwork itself,
# Thumb and Backdrop are what the landscape shapes fall back through
# (image_spec's preferThumb ladder), and a view asking for one of the other
# kinds gets it added -- see GRID_IMAGE_TYPES.
BROWSE_IMAGE_TYPES = "Primary,Thumb,Backdrop"

# Artwork a library view can be *set* to, which the server only sends when the
# query asks for it. This is the whole of the Banner bug: Banner, Logo and Disc
# are absent from BROWSE_IMAGE_TYPES, so ImageTags never carried one, so
# image_spec fell through to the item's thumbnail and drew it letterboxed in a
# 5.4:1 frame. jellyfin-web asks for exactly the type the view is set to
# (useFetchItems.ts: `enableImageTypes: [ImageType, Backdrop]`).
GRID_IMAGE_TYPES = ("Banner", "Logo", "Disc")

# What each of those tries before giving up and taking the poster. A banner
# and a logo are the same thing at different margins -- the show's name, drawn
# to be read on a bar -- so they stand in for each other. A disc has no
# stand-in: nothing else in a library is a round label.
_WORDMARK_CHAIN = {
    "Banner": ("Banner", "Logo"),
    "Logo": ("Logo", "Banner"),
    "Disc": ("Disc",),
}


def browse_image_types(image_type=None):
    """``EnableImageTypes`` for a grid drawn with ``image_type``.

    The base three plus everything that view can *resolve* to -- the whole
    wordmark chain, not just the name asked for, or the Banner fallback to a
    logo would be looking for a tag the query told the server to leave out.
    The base set stays because our fallback chains are wider than web's: a
    view set to Banner still draws the item's Primary where it has neither.
    """
    extra = (image_type or "").strip().capitalize()
    chain = _WORDMARK_CHAIN.get(extra)
    if not chain:
        return BROWSE_IMAGE_TYPES
    return BROWSE_IMAGE_TYPES + "," + ",".join(chain)

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

# Fields for music browse (albums/artists/tracks). ItemCounts is what fills in
# the track/album totals on artist tiles. The artist/album labels and track
# runtimes these views also draw need no field at all — see LIST_FIELDS.
MUSIC_FIELDS = "PrimaryImageAspectRatio,ItemCounts"

# Fields requested for the detail view. Intentionally a superset (MediaSources,
# MediaStreams, People, ...) so cached DTOs are already complete for the eventual
# offline-sync feature. The ratings, premiere date and production year the view
# also shows are unconditional properties and need no field — see LIST_FIELDS.
DETAIL_FIELDS = (
    "Path,Overview,Genres,Studios,People,Taglines,SortName,"
    "MediaSources,MediaStreams,Chapters,ProviderIds,"
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
# "livetv" is no longer excluded either: it is a real destination now (the
# tabbed Live TV screen), reached by opening its library tile. It is still
# special-cased in two places — it never gets a "Latest" row (a tuner has no
# recently-added anything) and clicking it routes to the Live TV page rather
# than to a grid of its children.
# "books" is no longer excluded: an AudioBook is an ordinary audio item and
# plays like any other, and a Book is reachable as a download-and-open target
# (see ``books.py`` for why that is as far as it can go).
EXCLUDED_COLLECTION_TYPES: set = set()

#: CollectionType of a books library. Holds two unrelated entity types --
#: ``Book`` and ``AudioBook`` -- which is why so much book handling asks
#: about the *item* type rather than the library's.
BOOKS_COLLECTION = "books"

#: CollectionType of the Live TV view. Its own constant because three modules
#: test for it and a bare string in each is how one of them ends up spelled
#: "liveTv".
LIVE_TV_COLLECTION = "livetv"

# Item types that open the detail/play view rather than drilling deeper.
# "Recording" is one of them: a finished recording is an ordinary file on the
# server and plays like any other item. Servers before 10.7 label them that
# way; newer ones hand back a Movie/Episode/Video, which was already covered.
PLAYABLE_TYPES = {"Movie", "Episode", "Video", "MusicVideo", "Recording"}

#: Photos play, but they are deliberately NOT in PLAYABLE_TYPES: that set
#: also drives the tile context menu, and Download / Add to Playlist /
#: Mark Watched are all either meaningless or broken for a still image.
#: A photo click is routed on its own in ``app._open_item``.
PHOTO_TYPE = "Photo"
# Item types that drill into a series view.
SERIES_TYPES = {"Series"}
# Live TV entries, which play immediately rather than opening a detail view.
# A Program plays its ChannelId, not its own id.
LIVE_TYPES = {"Program", "TvChannel"}
# Item types that drill into another grid (by ParentId).
# "PhotoAlbum" is a folder that Jellyfin gives its own type: a Home Videos
# directory holding both clips and images comes back as PhotoAlbum with
# IsFolder true. Without it here those folders dead-ended at _open_item's
# else branch -- the single reason a mixed home-video library had holes
# in it.
FOLDER_TYPES = {"CollectionFolder", "Folder", "BoxSet", "Season", "UserView",
                "PhotoAlbum"}
# Item types shown inside a playlist. A playlist can mix in music/other entries;
# only these are surfaced (and downloaded). Audio is included so music
# playlists play, queue, and download (the now-playing bar drives them).
# AudioBook alongside Audio: the tile menu offers "Add to Playlist" on one
# now, and a playlist that accepted an entry it would then refuse to SHOW
# is worse than not offering it -- the track would vanish from the playlist
# on the next visit with nothing to say why.
PLAYLIST_SUPPORTED_TYPES = {"Movie", "Episode", "Video", "Audio", "AudioBook"}

#: What Play All and Shuffle will put in a queue.
#:
#: **MediaType, not Type.** Type is the concrete entity the library scanner
#: resolved to, so it depends on which resolver ran: the same clip is a
#: ``Video`` under a Home Videos library and a ``Movie`` under a movies one
#: (MovieResolver.cs:158-215). MediaType is the question actually being
#: asked -- is this a stream, or a container? -- and it is also the axis
#: jellyfin-web's playbackManager filters on.
#:
#: Photos are in. "Play all" in an album of pictures means the slideshow,
#: and leaving them out made that button dead in exactly the folders it was
#: added for. What keeps it from being a queue that stalls on every still is
#: that the pause-on-open rule is a property of the *request* -- clicking one
#: picture opens a viewer, Play All starts a slideshow (see
#: PlayerManager.play's pause_stills).
QUEUEABLE_MEDIA = frozenset({"Video", "Audio", "Photo"})

#: The same set as a ``MediaTypes`` query parameter. Sorted so the URL is
#: stable, which matters only for reading logs.
QUEUEABLE_MEDIA_PARAM = ",".join(sorted(QUEUEABLE_MEDIA))

#: How many items Play All / Shuffle queue from a library.
#:
#: The number is jellyfin-web's: ``getItemsForPlayback`` caps every playback
#: query at 300 unless the caller passes its explicit "unlimited" sentinel,
#: which only a photo album does. Matching it means the same button builds
#: the same queue in both clients -- ours was 200 for no reason beyond when
#: it was written.
#:
#: A cap at all, rather than the whole library, because this is a queue to
#: play and not a list to browse: 300 is more than anyone watches in a
#: sitting, and the alternative is asking a server for forty thousand ids to
#: use the first few.
QUEUE_LIMIT = 300

#: What a search asks the server for, in total, across every type it draws
#: a row for. jellyfin-web's number (its search asks the same endpoint for
#: 800 and splits the answer client-side, as the search screen does).
#:
#: It is one budget for all the rows, so it has to be generous: at 60 a
#: term that matched a lot of episodes spent the whole allowance on them
#: and the Movies row never appeared at all (#641). Sorting is the server's,
#: and it does not interleave by type, so the shortfall is not spread
#: evenly -- it takes whole rows off the bottom.
SEARCH_LIMIT = 800

#: What the item half of a search covers. Artists are NOT here: they have
#: their own endpoint and their own request, exactly as in web, because
#: /Items does not reliably answer with them. On the server this was
#: developed against the item query returns fewer artists than /Artists (9
#: against 13 for one term -- /Artists includes track-level and featured
#: artists that are not library items), and on at least one real server it
#: returns none at all, which is what "there is no Artists row" looked like
#: from the outside.
SEARCH_TYPES = ("Movie,Series,Episode,Video,MusicAlbum,Audio,"
                "AudioBook,Book")

#: People and artists are separate endpoints with separate budgets, so
#: these are not shared with anything. 20 was low enough that a common first
#: name could fill it with people the user did not mean; web asks for 100.
PEOPLE_SEARCH_LIMIT = 100
ARTIST_SEARCH_LIMIT = 100

#: Fields a list of guide entries needs on top of ``LIST_FIELDS``.
#:
#: **Two fields, not one.** ``ChannelInfo`` alone gets ``ChannelName`` and
#: ``ChannelNumber``; the channel's *logo* is gated separately on
#: ``ChannelImage`` (``LiveTvManager.AddInfoToProgramDto`` sets
#: ``ChannelPrimaryImageTag`` only under ``hasChannelImage``). Most guide
#: data carries no artwork of its own and the channel logo is the whole
#: fallback, so asking for only the first turns every listing row into a
#: wall of letter glyphs. jellyfin-web asks for the pair together.
PROGRAM_FIELDS = ",ChannelInfo,ChannelImage"

#: Channels per guide page. The guide asks for programmes with the channel
#: ids in the query string, and past roughly 150-200 GUIDs that overflows the
#: request-line limit in Kestrel and common reverse proxies (414/431) — so
#: this bounds the guide fetch as much as it bounds the list. jellyfin-web
#: pages at 500 and gets away with it only because it splits nothing.
CHANNEL_PAGE = 100

#: Programmes the channel page asks for. jellyfin-web asks for the whole
#: guide with no limit at all, which it can afford because a browser drops
#: off-screen rows itself; this was 200 while the page built every row into
#: one scene. It windows now (``ChannelPage``), so the ceiling is about the
#: response rather than the render: a fortnight of half-hour listings is
#: ~670 rows and two weeks is as deep as guide data usually goes. A backstop
#: rather than a budget -- the page still says so when it bites, so a
#: provider with more does not look like it has less.
CHANNEL_LISTING = 1000


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
        self._home_prefs: dict[str, Any] = {}
        # uuid -> the resolved guide preference dict. Same document as
        # _home_prefs, cached separately: the home screen reads on startup
        # and the guide may never be opened at all.
        self._live_tv_prefs: dict[str, Any] = {}
        self._user_prefs: dict[str, Any] = {}
        # The whole CustomPrefs blob, shared by every consumer of it.
        self._custom_prefs: dict[str, Any] = {}
        # uuid -> whether this server offers Live TV to this user. Derived for
        # free from the /Views response get_libraries already fetches; see
        # has_live_tv for why that answer is authoritative.
        self._has_live_tv: dict[str, bool] = {}
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

    def auth_origins(self):
        """``{(scheme, host, port): authorization-header}`` for every server
        this source is connected to.

        For the thumbnail store, which fetches image URLs on its own session
        and would otherwise have to carry the token in the query string. The
        header is the apiclient's own -- the non-legacy MediaBrowser scheme
        -- so there is one spelling of it in the app.
        """
        out = {}
        for conn in self._conns.values():
            try:
                client = conn.client
                base = client.config.data.get("auth.server") or ""
                header = client.http._get_authenication_header()
            except Exception:
                log.debug("no auth header for a browse connection",
                          exc_info=True)
                continue
            if not header or "Token=" not in header:
                continue
            parts = urlparse(base)
            if parts.hostname:
                out[(parts.scheme, parts.hostname, parts.port)] = header
        return out

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
                continue
            # Noted on the way past rather than fetched separately: the
            # server adds this view only when the user may use Live TV AND
            # a tuner is configured (UserViewManager consults
            # LiveTvManager.GetEnabledUsers, which is
            # EnableLiveTvAccess && tuner hosts exist). So its presence
            # here is the whole gate, at no extra request — which matters
            # because Live TV sits in the stock home layout, and without a
            # gate every user without a tuner would pay for a row that can
            # never have anything in it.
            has_live_tv = (has_live_tv
                           or item.get("CollectionType") == LIVE_TV_COLLECTION)
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

    # -- what this user is allowed to do (see user_policy.py) --------------
    #
    # Thin, and deliberately so: the answer is cached on the client object,
    # not here, because the player side reaches the same clients through
    # clientManager and both have to get the same answer. These exist so the
    # browser never has to reach for a client itself.

    def syncplay_access(self, server_uuid):
        from ..user_policy import syncplay_access

        try:
            return syncplay_access(self._conn(server_uuid).client)
        except Exception:
            from ..user_policy import CREATE_AND_JOIN

            return CREATE_AND_JOIN      # fails open; see user_policy

    def can_manage_live_tv(self, server_uuid):
        from ..user_policy import may_manage_live_tv

        try:
            return may_manage_live_tv(self._conn(server_uuid).client)
        except Exception:
            return True                 # fails open; see user_policy

    def can_manage_collections(self, server_uuid):
        from ..user_policy import may_manage_collections

        try:
            return may_manage_collections(self._conn(server_uuid).client)
        except Exception:
            return True                 # fails open; see user_policy

    def can_download(self, server_uuid):
        """`EnableContentDownloading`.

        Read here for books in particular. Every other download is an
        offline convenience -- without the permission you simply watch the
        thing online -- but ``/Items/{id}/Download`` is the *only* path to a
        book's bytes, so a user without it cannot open a book at all. That
        deserves to be said, rather than surfacing as a download that always
        fails. Fails open like its siblings: only an answer the server gave
        closes the gate.
        """
        from ..user_policy import may_download

        try:
            return may_download(self._conn(server_uuid).client)
        except Exception:
            return True                 # fails open; see user_policy

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
        # client name, which must match what jellyfin-web uses or we write a
        # preference set only this client can see.
        api.update_user_settings(dto,
                                 client=home_sections.DISPLAY_PREFS_CLIENT)
        excludes = self._home_prefs.get(server_uuid, (None, frozenset()))[1]
        self._home_prefs[server_uuid] = (list(layout), excludes)

    def get_user_prefs(self, server_uuid, refresh=False):
        """Per-user display preferences, cached. See ``user_prefs``.

        Shares the DisplayPreferences document the home layout and the guide
        settings live in, cached separately for the same reason they are:
        each screen wants its own without paying for the others.

        Never raises: an unreachable or ancient server gets jellyfin-web's
        defaults, which is the behaviour we had before the setting existed.
        """
        if not refresh and server_uuid in self._user_prefs:
            return self._user_prefs[server_uuid]
        api = self._conn(server_uuid).api
        try:
            custom = self._display_prefs_dto(api).get("CustomPrefs") or {}
        except Exception:
            log.warning("Failed to read display preferences; using defaults",
                        exc_info=True)
            custom = {}
        prefs = user_prefs.resolve_prefs(custom)
        self._user_prefs[server_uuid] = prefs
        return prefs

    def save_user_prefs(self, server_uuid, prefs):
        """Persist the display preferences. Raises on failure.

        Read-modify-write of the whole DTO, for the same reason
        ``save_home_layout`` and ``save_live_tv_prefs`` do it: there is no
        partial-update path on this API, so posting only our keys would drop
        jellyfin-web's home layout, guide settings and landing screens.

        The cache is adopted before the write and rolled back if it fails --
        the alternative is a cache that disagrees with the server for the
        rest of the session.
        """
        api = self._conn(server_uuid).api
        previous = self._user_prefs.get(server_uuid)
        self._user_prefs[server_uuid] = dict(prefs)
        try:
            dto = self._display_prefs_dto(api)
            custom = dict(dto.get("CustomPrefs") or {})
            custom.update(user_prefs.prefs_to_custom(prefs))
            dto["CustomPrefs"] = custom
            api.update_user_settings(dto,
                                     client=home_sections.DISPLAY_PREFS_CLIENT)
        except Exception:
            if previous is None:
                self._user_prefs.pop(server_uuid, None)
            else:
                self._user_prefs[server_uuid] = previous
            raise

    def get_view_settings(self, server_uuid, parent_id, collection_type):
        """``{setting: (value, key)}`` for a library's saved view settings.

        All four in one read, because they live in one document -- and the
        key each came from rides along so a save lands where the user's web
        client will look for it (see ``view_prefs``).
        """
        try:
            custom = self._display_prefs_custom(server_uuid)
        except Exception:
            log.debug("could not read view preferences", exc_info=True)
            custom = {}
        out = {
            "imageType": view_prefs.resolve_image_type(
                custom, parent_id, collection_type),
            "viewType": view_prefs.resolve_view_type(
                custom, parent_id, collection_type),
        }
        for setting in view_prefs.BOOL_SETTINGS:
            out[setting] = view_prefs.resolve_bool(
                custom, parent_id, collection_type, setting)
        return out

    def _display_prefs_custom(self, server_uuid, refresh=False):
        """The raw CustomPrefs blob, cached.

        One cache for the whole document rather than one per consumer: the
        home layout, the guide settings, the display prefs and the per-view
        settings are all in it, and re-fetching it per screen would be four
        round trips for one document.
        """
        if not refresh and server_uuid in self._custom_prefs:
            return self._custom_prefs[server_uuid]
        api = self._conn(server_uuid).api
        custom = self._display_prefs_dto(api).get("CustomPrefs") or {}
        self._custom_prefs[server_uuid] = custom
        return custom

    def save_view_setting(self, server_uuid, parent_id, collection_type,
                          setting, value, key=None):
        """Persist one of a library's view settings. Raises on failure.

        ``key`` is the one it was read from, so a change lands where the
        user's web client will look for it; with none stored yet the first
        candidate is used. Read-modify-write of the whole DTO, as every
        other writer here does.

        Booleans go out as ``"true"``/``"false"`` strings for the reason
        every other boolean in this document does: web compares them as
        strings, so a JSON boolean reads there as false.
        """
        api = self._conn(server_uuid).api
        if not key:
            candidates = view_prefs.keys_for(parent_id, collection_type,
                                             setting)
            if not candidates:
                return
            key = candidates[0]
        if isinstance(value, bool):
            value = "true" if value else "false"
        dto = self._display_prefs_dto(api)
        custom = dict(dto.get("CustomPrefs") or {})
        custom[key] = value
        dto["CustomPrefs"] = custom
        api.update_user_settings(dto,
                                 client=home_sections.DISPLAY_PREFS_CLIENT)
        self._custom_prefs[server_uuid] = custom

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
                # Deliberately no parent_id: that is what lets the server apply
                # the user's "Display in home screen sections" exclusions for
                # us. Scoping this by library would silently bypass them.
                resume = api.get_resume_items(
                    limit=20,
                    fields=LIST_FIELDS,
                    enable_image_types="Primary,Thumb,Backdrop",
                    # One tag per image type: without it every backdrop tag
                    # comes back, and items routinely carry five to ten.
                    image_type_limit=1,
                    # The row is capped at 20 anyway, so the server's separate
                    # COUNT(*) over the whole library is pure waste
                    # (jellyfin-web passes this on all three home queries).
                    enable_total_record_count=False,
                    **extra) or {}
                return (title, resume.get("Items", []), collection_type, None)
            return fetch

        def video_resume_row():
            # No CollectionType: these mixed rows keep the item-type heuristic.
            return resume_row(_("Continue Watching"),
                              include_item_types="Movie,Episode,Video")()

        def audio_resume_row():
            # media_types rather than include_item_types, matching
            # jellyfin-web: it catches Audio and AudioBook without enumerating
            # types. The music collection_type gives the row square art.
            #
            # Square is a DELIBERATE divergence, not a side effect of that
            # tag. jellyfin-web shapes every resume row 16:9 except Book
            # (homesections/sections/resume.ts:557) -- audio included -- which
            # crops a square cover on both sides for no gain. Album art is
            # square; the row that lists it should be too.
            return resume_row(_("Continue Listening"), collection_type="music",
                              media_types="Audio")()

        def book_resume_row():
            # media_types="Book", which is what jellyfin-web's Continue
            # Reading section asks for -- and the only thing that works:
            # the two book entity types are unrelated (an AudioBook is an
            # Audio and would land in Continue Listening), so a type filter
            # would have to name them and get the split wrong.
            #
            # Portrait art, which is the ONE resume row web does not shape
            # 16:9 (homesections/sections/resume.ts: `mediaType === 'Book'`
            # takes getPortraitShape). A cover is a cover. The books
            # collection_type is what carries that through to the tiles.
            return resume_row(_("Continue Reading"),
                              collection_type=BOOKS_COLLECTION,
                              media_types="Book")()

        def next_up_row():
            nextup = api.get_next(
                limit=20, fields=LIST_FIELDS,
                enable_image_types="Primary,Thumb,Backdrop",
                # Every other home query caps this; Next Up was the one that
                # did not, so a series with twenty backdrops sent twenty tags
                # per card for the one the tile draws.
                image_type_limit=1) or {}
            return (_("Next Up"), nextup.get("Items", []), None, None)

        def live_tv_row():
            # jellyfin-web's Live TV home section is a row of nav buttons plus
            # an "On Now" strip; the strip is the part that lists anything, so
            # it is the part reproduced here.
            #
            # PROGRAM_FIELDS is what adds the channel name and its logo to
            # each program, which is the only art most guide data carries.
            #
            # Not jellyfin-web's separate limit=1 probe: an empty row is
            # already dropped by the comprehension below, so probing first
            # would only add a round trip to reach the same result.
            # User data is deliberately NOT disabled, unlike the other home
            # rows — jellyfin-web's query does not disable it either, and an
            # On Now tile shows a watched tick and a favourite heart like
            # any other. It has no bearing on the recording state: TimerId,
            # SeriesTimerId and Status are attached by LiveTvManager's
            # AddRecordingInfo regardless of EnableUserData, which only ever
            # gates the DTO's UserData block.
            onnow = api.get_recommended_programs(
                is_airing=True,
                limit=24,
                fields=LIST_FIELDS + PROGRAM_FIELDS,
                enable_image_types="Primary,Thumb,Backdrop",
                image_type_limit=1,
                enable_total_record_count=False,
            ) or {}
            return (_("On Now"), onnow.get("Items", []), "livetv", None)

        def latest_row(lib):
            def fetch():
                # fields is passed explicitly: the default is info(), a
                # 28-field payload including MediaSources, People, Studios and
                # RecursiveItemCount. MediaSources forces per-item media-source
                # resolution and the rest add joins — for 16 items times every
                # library, none of which this row renders. LIST_FIELDS is what
                # the other browse calls use.
                latest = api.get_recently_added(
                    parent_id=lib.get("Id"),
                    limit=16,
                    fields=LIST_FIELDS,
                    enable_image_types="Primary,Thumb,Backdrop",
                    image_type_limit=1,
                    enable_total_record_count=False,
                )
                # /Latest answers with a bare list, not an Items dict.
                items = (latest.get("Items", []) if isinstance(latest, dict)
                         else (latest or []))
                # The library id rides along so the row's heading can link
                # to it. Nothing else in the tuple identifies which library
                # a Latest row came from -- the title is a translated
                # string and the collection type is shared.
                return (_("Latest %s") % lib.get("Name", ""), items,
                        lib.get("CollectionType"), lib.get("Id"))
            return fetch

        def active_recordings_row():
            return (_("Active Recordings"),
                    self.get_recordings(server_uuid, limit=12,
                                        is_in_progress=True),
                    "livetv", None)

        builders = {
            home_sections.RESUME: video_resume_row,
            home_sections.RESUME_AUDIO: audio_resume_row,
            home_sections.RESUME_BOOK: book_resume_row,
            home_sections.NEXT_UP: next_up_row,
            home_sections.LIVE_TV: live_tv_row,
            home_sections.ACTIVE_RECORDINGS: active_recordings_row,
        }

        #: Sections that only mean anything on a server with a tuner. Both are
        #: in reach of the stock layout, and a server without Live TV can only
        #: ever answer them empty — so the gate is what keeps them free for
        #: the large majority of users who have no tuner.
        live_tv_sections = (home_sections.LIVE_TV,
                            home_sections.ACTIVE_RECORDINGS)

        # (slot, kind, callable). The slot travels with the row so the caller
        # can restore the user's order after merging the two fetch batches.
        # Sections we cannot draw simply have no entry in STAGE and
        # contribute no work.
        tasks = []
        for slot, kind in enumerate(layout):
            stage = home_sections.STAGE.get(kind)
            if stage is None or stage == "local" or stage not in sections:
                continue
            if kind in live_tv_sections and not self.has_live_tv(server_uuid):
                continue
            if kind == home_sections.LATEST:
                # One request per library, so this is where the user's
                # exclusions have to be honoured — the ParentId these carry
                # stops the server from doing it.
                tasks += [(slot, kind, latest_row(lib)) for lib in libraries
                          if lib.get("CollectionType") not in (
                              "playlists", LIVE_TV_COLLECTION)
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
                 "slot": slot, "kind": kind, "parent_id": pid}
                for slot, kind, t, i, c, pid in rows if i]

    # -- Live TV -----------------------------------------------------------
    #
    # Reads only. Creating and cancelling recordings is a mutation and goes
    # through the gateway (gateway/livetv.py), exactly as playlist editing
    # does — this class is the browse seam an offline source has to be able
    # to stand in for, and an offline source cannot schedule a recording.

    def get_live_tv_prefs(self, server_uuid, refresh=False):
        """The guide preference dict (see ``live_tv.resolve_prefs``), cached.

        Shares the DisplayPreferences document the home layout lives in, so
        one read serves both — but they are cached separately because the
        home screen loads on startup and the guide may never be opened.

        Never raises: an unreachable or ancient server gets jellyfin-web's
        defaults, which is a working guide rather than no guide.
        """
        if not refresh and server_uuid in self._live_tv_prefs:
            return self._live_tv_prefs[server_uuid]
        api = self._conn(server_uuid).api
        try:
            custom = self._display_prefs_dto(api).get("CustomPrefs") or {}
        except Exception:
            log.warning("Failed to read Live TV preferences; using defaults",
                        exc_info=True)
            custom = {}
        prefs = live_tv.resolve_prefs(custom)
        self._live_tv_prefs[server_uuid] = prefs
        return prefs

    def cache_live_tv_prefs(self, server_uuid, prefs):
        """Adopt ``prefs`` as the cached answer without writing anything.

        **Called on the loop thread, before the save is submitted.** Saving
        the guide settings repaints the guide, and repainting it means
        re-fetching it — a pool job whose first act is ``get_live_tv_prefs``.
        That job is submitted *first*, so no amount of care inside the save
        worker wins the race: by the time the save runs, the reload has
        already read the old cache and the guide comes back drawn with the
        settings the user just changed away from.

        So the cache moves on the thread that ordered both, where there is
        no race to lose. A dict assignment, which is why that is safe.
        """
        self._live_tv_prefs[server_uuid] = dict(prefs)

    def save_live_tv_prefs(self, server_uuid, prefs):
        """Persist the guide preferences. Raises on failure.

        Read-modify-write of the whole DTO, for the same reason
        ``save_home_layout`` does it: there is no partial-update path on this
        API, so posting only our keys would drop jellyfin-web's home layout,
        landing screens and everything else the same document holds.

        The cache is adopted before the write and rolled back if it fails —
        the alternative is a cache that disagrees with the server for the
        rest of the session. Callers that also repaint must additionally
        call :meth:`cache_live_tv_prefs` on the loop thread; see there.
        """
        api = self._conn(server_uuid).api
        previous = self._live_tv_prefs.get(server_uuid)
        self._live_tv_prefs[server_uuid] = dict(prefs)
        try:
            dto = self._display_prefs_dto(api)
            custom = dict(dto.get("CustomPrefs") or {})
            custom.update(live_tv.prefs_to_custom(prefs))
            dto["CustomPrefs"] = custom
            api.update_user_settings(dto,
                                     client=home_sections.DISPLAY_PREFS_CLIENT)
        except Exception:
            if previous is None:
                self._live_tv_prefs.pop(server_uuid, None)
            else:
                self._live_tv_prefs[server_uuid] = previous
            raise

    def get_channels(self, server_uuid, start_index=0, limit=CHANNEL_PAGE,
                     prefs=None, categories=(), add_current_program=True,
                     favorites_only=False):
        """Live TV channels as ``(items, total)``.

        Paged, because an IPTV line-up runs to thousands of channels and this
        endpoint has no way to skip the total record count — the unbounded
        call returns every one of them with artwork and user data attached.

        ``add_current_program`` is what puts "what is on now" on each channel
        tile; the guide turns it off because it fetches the programmes for
        the whole window anyway.
        """
        api = self._conn(server_uuid).api
        kwargs = dict(live_tv.channel_sort_kwargs(prefs or {}))
        kwargs.update(live_tv.category_kwargs(categories))
        if favorites_only:
            kwargs["is_favorite"] = True
        result = api.get_channels(
            start_index=start_index, limit=limit,
            fields=LIST_FIELDS,
            image_type_limit=1,
            enable_image_types="Primary,Thumb,Backdrop",
            add_current_program=add_current_program,
            **kwargs) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_guide_info(self, server_uuid):
        """The provider's guide date range, for the date picker. ``{}`` when
        the server cannot answer — the picker then imposes no limit."""
        api = self._conn(server_uuid).api
        try:
            return api.get_live_tv_guide_info() or {}
        except Exception:
            log.debug("GuideInfo unavailable", exc_info=True)
            return {}

    def get_guide(self, server_uuid, channel_ids, start, end, want_hd=False):
        """Guide entries for ``channel_ids`` overlapping [start, end).

        ``start``/``end`` are aware datetimes; they go out as UTC ISO 8601
        **with the Z**, because the server binds these with
        ``AdjustToUniversal`` and silently accepts an offset-less string
        without shifting it — i.e. quietly answers for the wrong window.

        The bounds are the pair jellyfin-web uses and they are not symmetric:
        ``MaxStartDate`` is the window end and ``MinEndDate`` its start, which
        is what includes a programme that began before the window opened.

        **No category filter.** The guide's categories are applied by
        drawing (``live_tv.program_displayed``), as jellyfin-web does, and
        not here. Two reasons, one of which is a bug this used to have: the
        server's ``IsMovie`` is a column predicate while the other three are
        a tag filter, so two categories AND together and the guide came back
        empty; and dropping the rows entirely turns a filtered guide into a
        field of dead air rather than a grid with quiet cells.
        """
        api = self._conn(server_uuid).api
        ids = [c for c in (channel_ids or []) if c]
        if not ids:
            return []
        result = api.get_programs(
            channel_ids=ids,
            # +/- a second, as jellyfin-web does, so a programme that ends
            # exactly as the window opens (or starts exactly as it closes)
            # does not occupy a zero-width cell at the edge.
            max_start_date=_iso_utc(end - datetime.timedelta(seconds=1)),
            min_end_date=_iso_utc(start + datetime.timedelta(seconds=1)),
            sort_by="StartDate",
            # ChannelInfo without ChannelImage, unlike every other program
            # query: a guide cell is text, so it wants ChannelName for its
            # second line and has nowhere to put a logo. Asking for the
            # image tag would cost a channel lookup per programme across the
            # whole window for something never drawn.
            fields="ChannelInfo" + (",IsHD" if want_hd else ""),
            enable_user_data=False,
            enable_total_record_count=False,
            enable_image_types="Primary",
            image_type_limit=1) or {}
        return result.get("Items", [])

    def get_channel_listing(self, server_uuid, channel_id,
                            limit=CHANNEL_LISTING):
        """A channel and everything still to come on it, for the channel page.

        jellyfin-web's ``renderChannelGuide``: ``HasAired=False`` sorted by
        start, which is "has not finished yet" rather than "has not started"
        — so the first entry is what is on right now, not what is on next.

        Returns ``{"channel": dto-or-None, "programs": [...], "capped":
        bool}``. Both halves in one call because they are one screen and the
        page would otherwise draw twice, half-empty.

        One departure from jellyfin-web: ``limit``, a backstop rather than a
        budget now that the page windows its rows (``capped`` says so, so a
        provider with a deeper guide does not look like it has a shallower
        one).

        **No image fields**, which is jellyfin-web's call here too and the
        same one ``get_guide`` makes for the same reason: these rows are
        text. ``ChannelImage`` in particular costs a channel lookup per
        programme -- across a thousand of them -- for a tag nothing on this
        screen draws. A row that is clicked still seeds the program page,
        which re-reads the authoritative DTO anyway and already draws its
        heading as text while artwork is absent.

        Web also sends ``EnableImages: false``, which suppresses the tags
        themselves (~12% of the body here). We cannot: ``get_programs`` has
        no such argument, and passing one raised a TypeError that took the
        whole channel page down. Leaving the tags in is the cheap half of
        the wrong answer; the fields above are the expensive half, and they
        are gone.
        """
        api = self._conn(server_uuid).api
        channel = None
        try:
            channel = api.get_item(channel_id, fields=DETAIL_FIELDS)
        except Exception:
            # Not fatal: the tile that linked here seeded the header, and
            # what the page is actually for is the listing below it.
            log.debug("channel %s unavailable", channel_id, exc_info=True)
        result = api.get_programs(
            channel_ids=[channel_id],
            has_aired=False,
            sort_by="StartDate",
            limit=limit,
            fields=LIST_FIELDS + ",ChannelInfo",
            enable_user_data=False,
            enable_total_record_count=False) or {}
        programs = result.get("Items", [])
        return {"channel": channel, "programs": programs,
                "capped": len(programs) >= limit}

    def programs_page(self, server_uuid, start_index=0, limit=24,
                      with_total=False, **filters):
        """``(items, total)`` from ``LiveTv/Programs``.

        The paged form, for the "see all" list route. ``get_programs``
        below is the same query without the paging — the twelve-item rows
        want a plain list and would pay for a count they never draw.

        ``with_total`` is what buys a real ``TotalRecordCount``: the rows
        turn it off deliberately (it is a second, wider query server-side),
        but a paginated screen cannot page without it — falling back to
        ``len(items)`` tells the paginator the first page is the whole
        list, which is how it came to re-serve page 1 as page 2.
        """
        api = self._conn(server_uuid).api
        result = api.get_programs(
            start_index=start_index,
            limit=limit,
            fields=LIST_FIELDS + PROGRAM_FIELDS,
            image_type_limit=1,
            enable_image_types="Primary,Thumb,Backdrop",
            enable_total_record_count=with_total,
            **filters) or {}
        items = result.get("Items", [])
        # No count asked for: report the page, not a total we do not have.
        total = (result.get("TotalRecordCount", len(items)) if with_total
                 else len(items))
        return items, total

    def get_programs(self, server_uuid, limit=24, **filters):
        """Upcoming guide entries for the Programs screen's category rows.

        ``filters`` are the ``is_movie``/``is_sports``/… flags plus
        ``has_aired``; they go straight through to ``LiveTv/Programs``.
        """
        return self.programs_page(server_uuid, limit=limit, **filters)[0]

    def get_recommended_programs(self, server_uuid, limit=24, **filters):
        """The server's own recommendations — the "On Now" strip."""
        api = self._conn(server_uuid).api
        result = api.get_recommended_programs(
            limit=limit,
            fields=LIST_FIELDS + PROGRAM_FIELDS,
            image_type_limit=1,
            enable_image_types="Primary,Thumb,Backdrop",
            enable_total_record_count=False,
            **filters) or {}
        return result.get("Items", [])

    #: The Programs screen's rows: (key, title-factory, query). Mirrors
    #: jellyfin-web's livetvsuggested, including the shape of the "Upcoming
    #: Episodes" query — its four ``False`` flags are what stop a sports
    #: fixture or a kids' show turning up in it, and they have to be sent as
    #: explicit falses rather than omitted.
    PROGRAM_SECTIONS = (
        ("onnow", lambda: _("On Now"), {"is_airing": True}),
        ("episodes", lambda: _("Upcoming Episodes"),
         {"has_aired": False, "is_series": True, "is_movie": False,
          "is_sports": False, "is_kids": False, "is_news": False}),
        ("movies", lambda: _("Upcoming Movies"),
         {"has_aired": False, "is_movie": True}),
        ("sports", lambda: _("Upcoming Sports"),
         {"has_aired": False, "is_sports": True}),
        ("kids", lambda: _("Upcoming Kids"),
         {"has_aired": False, "is_kids": True}),
        ("news", lambda: _("Upcoming News"),
         {"has_aired": False, "is_news": True}),
    )

    def get_program_sections(self, server_uuid, limit=12):
        """The Programs screen's rows as ``[{"key", "title", "items"}]``.

        Fanned out, like the home screen: six independent requests walked
        serially is six round trips before the screen can draw anything, and
        guide queries are not fast. Order is preserved by collecting in
        submit order, and one failed row costs only that row.

        Empty rows are dropped, so a provider with no sports data simply has
        no Sports row rather than an empty heading.
        """
        def fetch(key, title, query):
            def work():
                if key == "onnow":
                    # The server's own recommendations, which is what the
                    # official clients show — not a plain is_airing query.
                    items = self.get_recommended_programs(
                        server_uuid, limit=limit * 2, **query)
                else:
                    items = self.get_programs(server_uuid, limit=limit,
                                              **query)
                # The query rides along: the row's own predicate is what a
                # "see all" listing re-runs without the limit, and keeping
                # it here means the destination cannot drift from the row.
                return {"key": key, "title": title(), "items": items,
                        "filters": dict(query)}
            return work

        tasks = [fetch(key, title, query)
                 for key, title, query in self.PROGRAM_SECTIONS]
        rows = []
        with ThreadPoolExecutor(max_workers=min(HOME_FANOUT, len(tasks)),
                                thread_name_prefix="livetv") as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    row = future.result()
                except Exception:
                    log.warning("a Live TV programs row failed to load",
                                exc_info=True)
                    continue
                if row["items"]:
                    rows.append(row)
        return rows

    def search_live_tv(self, server_uuid, term, limit=24):
        """Channels and guide entries matching ``term``.

        Two requests, not jellyfin-web's seven: it splits programmes into
        movies/episodes/sports/kids/news/other so each row can have its own
        card shape, and then draws them all the same. One Programs row is
        the same information for a fifth of the traffic.

        Never raises — a search that half-worked is better than a search
        screen that reports failure because the tuner was busy.
        """
        api = self._conn(server_uuid).api

        def find(item_type, fields):
            try:
                result = api.get_user_items(
                    include_item_types=item_type, search_term=term,
                    recursive=True, limit=limit, fields=fields,
                    image_type_limit=1,
                    enable_image_types="Primary,Thumb") or {}
            except Exception:
                log.debug("Live TV search for %s failed", item_type,
                          exc_info=True)
                return []
            return result.get("Items", [])

        return {"channels": find("TvChannel", LIST_FIELDS),
                "programs": find("LiveTvProgram",
                                 LIST_FIELDS + PROGRAM_FIELDS)}

    def get_live_program(self, server_uuid, program_id):
        """One guide entry with its live recording state, for the program
        page. ``None`` when the entry has expired out of the guide."""
        api = self._conn(server_uuid).api
        try:
            return api.get_live_tv_program(program_id)
        except Exception:
            log.debug("program %s unavailable", program_id, exc_info=True)
            return None

    def recordings_page(self, server_uuid, start_index=0, limit=60,
                        with_total=False, is_in_progress=None,
                        series_timer_id=None):
        """``(items, total)`` of recordings, newest first. In-progress ones
        with ``is_in_progress=True``; everything else is the recordings
        library.

        An in-progress result is stamped ``_recording`` — a recording DTO
        carries no timer state, so the *query* is the only thing that knows
        it is still being written, and the tile needs to (it draws the red
        dot and a broadcast-progress bar rather than a resume bar). Stamped
        here rather than at each of the three call sites so none of them can
        forget.

        See ``programs_page`` for why paging and ``with_total`` go together.
        """
        api = self._conn(server_uuid).api
        result = api.get_live_tv_recordings(
            start_index=start_index,
            limit=limit,
            is_in_progress=is_in_progress,
            series_timer_id=series_timer_id,
            fields=LIST_FIELDS + ",CanDelete",
            image_type_limit=1,
            enable_image_types="Primary,Thumb,Backdrop",
            enable_total_record_count=with_total) or {}
        items = result.get("Items", [])
        if is_in_progress:
            for item in items:
                item["_recording"] = True
        total = (result.get("TotalRecordCount", len(items)) if with_total
                 else len(items))
        return items, total

    def get_recordings(self, server_uuid, limit=60, is_in_progress=None,
                       series_timer_id=None):
        """Recordings for a row: the list alone, unpaged."""
        return self.recordings_page(
            server_uuid, limit=limit, is_in_progress=is_in_progress,
            series_timer_id=series_timer_id)[0]

    def get_recording_folders(self, server_uuid):
        """The virtual folders recordings are filed under. These are ordinary
        folder items, so opening one lands in the normal grid."""
        api = self._conn(server_uuid).api
        try:
            result = api.get_recording_folders() or {}
        except Exception:
            log.debug("recording folders unavailable", exc_info=True)
            return []
        return result.get("Items", [])

    def get_timers(self, server_uuid, is_active=None, is_scheduled=None,
                   series_timer_id=None):
        """Single recording timers, in start order.

        Sorted here rather than by the server: ``LiveTv/Timers`` takes no
        sort arguments, and the screen groups them by day — which only reads
        as a schedule if they arrive in time order.
        """
        api = self._conn(server_uuid).api
        result = api.get_live_tv_timers(is_active=is_active,
                                        is_scheduled=is_scheduled,
                                        series_timer_id=series_timer_id) or {}
        items = result.get("Items", [])
        return sorted(items, key=lambda t: t.get("StartDate") or "")

    def get_timer(self, server_uuid, timer_id):
        api = self._conn(server_uuid).api
        return api.get_live_tv_timer(timer_id)

    def get_series_timers(self, server_uuid):
        api = self._conn(server_uuid).api
        result = api.get_live_tv_series_timers(
            sort_by="SortName", sort_order="Ascending") or {}
        return result.get("Items", [])

    def get_series_timer(self, server_uuid, timer_id):
        api = self._conn(server_uuid).api
        return api.get_live_tv_series_timer(timer_id)

    @staticmethod
    def _filter_kwargs(filters):
        """Translate the UI's filter dict into ``get_user_items`` arguments."""
        kwargs: dict[str, str] = {}
        if not filters:
            return kwargs
        active = []
        if filters.get("unplayed"):
            active.append("IsUnplayed")
        if active:
            kwargs["filters"] = ",".join(active)
        if filters.get("favorite"):
            kwargs["is_favorite"] = "true"
        if filters.get("genre"):
            kwargs["genres"] = filters["genre"]
        if filters.get("year"):
            kwargs["years"] = str(filters["year"])
        letter = filters.get("letter")
        if letter == "#":
            kwargs["name_less_than"] = "A"
        elif letter:
            kwargs["name_starts_with"] = letter
        return kwargs

    #: The by-name screens' rows: (key, title-factory, item types).
    #: jellyfin-web's ``itemsByName.js``, in its order. A genre, a studio or
    #: a person spans several kinds of thing, and one flat grid of all of
    #: them sorted by name is a worse answer than a row each.
    BY_NAME_SECTIONS = (
        ("movies", lambda: _("Movies"), "Movie"),
        ("shows", lambda: _("Shows"), "Series"),
        ("episodes", lambda: _("Episodes"), "Episode"),
        ("videos", lambda: _("Videos"), "Video,MusicVideo"),
        ("trailers", lambda: _("Trailers"), "Trailer"),
        ("albums", lambda: _("Albums"), "MusicAlbum"),
        ("songs", lambda: _("Songs"), "Audio"),
        # Books are genre-tagged like everything else (a books library has
        # its own Genres tab in jellyfin-web), so a genre that turns up
        # novels as well as films should say so. Last, as the search order
        # puts them.
        ("audiobooks", lambda: _("Audiobooks"), AUDIOBOOK_TYPE),
        ("books", lambda: _("Books"), BOOK_TYPE),
    )

    def get_by_name_sections(self, server_uuid, spec, limit=20):
        """``[{"key", "title", "items", "types", "total"}]`` for a by-name
        screen -- everything a genre, studio or person is attached to,
        grouped by what kind of thing it is.

        ``spec`` is a :meth:`get_list` ``items`` spec minus the item types;
        each row adds its own. Fanned out, empty rows dropped, and ``total``
        rides along so a row only offers "see all" when there is more behind
        it than it is showing -- which is jellyfin-web's rule for its own
        More button (``itemsByName.js:96``).
        """
        def fetch(key, title, types):
            def work():
                row_spec = dict(spec or {})
                row_spec["include_item_types"] = types
                items, total = self._list_items(
                    server_uuid, row_spec, "SortName", "Ascending", 0,
                    limit, None)
                return {"key": key, "title": title(), "items": items,
                        "types": types, "total": total}
            return work

        rows = []
        tasks = [fetch(*section) for section in self.BY_NAME_SECTIONS]
        with ThreadPoolExecutor(
                max_workers=min(HOME_FANOUT, max(1, len(tasks))),
                thread_name_prefix="byname") as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    row = future.result()
                except Exception:
                    log.warning("Failed to load a by-name row", exc_info=True)
                    continue
                if row["items"]:
                    rows.append(row)
        return rows

    #: Item type a genre row lists, by the library's collection type.
    #: jellyfin-web's per-collection view definitions (``views/movies.ts``,
    #: ``views/tvshows.ts``): a movies library's genres are rows of films, a
    #: TV library's are rows of *shows* rather than episodes.
    GENRE_ITEM_TYPES = {"movies": "Movie", "tvshows": "Series"}

    def get_genre_sections(self, server_uuid, parent_id, collection_type,
                           limit=10, max_genres=40):
        """``[{"key", "title", "items", "types"}]`` -- one row per genre.

        jellyfin-web's ``moviegenres`` / ``tvgenres``: a heading per genre
        over a random sample of that genre's items, with the heading linking
        to the unbounded listing. ``SortBy: Random`` is theirs too, and it is
        the point -- a fixed sample of ten would make the screen the same
        ten films forever.

        ``max_genres`` bounds the fan-out. A library can have a hundred
        genres and each row is a request; web lazy-loads them on scroll
        (IntersectionObserver), which we have no equivalent for, so a cap is
        the honest version. It is logged when it bites rather than silently
        truncating.
        """
        types = self.GENRE_ITEM_TYPES.get(collection_type)
        if not types:
            return []
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id, include_item_types=types) or {}
        genres = [g for g in result.get("Items", []) if g.get("Id")]
        if len(genres) > max_genres:
            log.info("Showing %d of %d genres; the rest need the search or "
                     "the genre filter.", max_genres, len(genres))
            genres = genres[:max_genres]

        def fetch(genre):
            def work():
                items, _total = self._list_items(
                    server_uuid,
                    {"type": "items", "genre_ids": genre["Id"],
                     "include_item_types": types, "parent_id": parent_id},
                    "Random", "Ascending", 0, limit, None)
                return {"key": genre["Id"], "title": genre.get("Name") or "",
                        "items": items, "types": types}
            return work

        rows = []
        if not genres:
            return rows
        with ThreadPoolExecutor(
                max_workers=min(HOME_FANOUT, len(genres)),
                thread_name_prefix="genres") as pool:
            for future in [pool.submit(fetch(g)) for g in genres]:
                try:
                    row = future.result()
                except Exception:
                    log.warning("Failed to load a genre row", exc_info=True)
                    continue
                if row["items"]:
                    rows.append(row)
        return rows

    #: The Favorites screen's rows: (key, title-factory, item types).
    #: jellyfin-web's favoriteitems.js, in its order -- which is roughly
    #: "biggest thing first" and worth keeping, because a favourites screen
    #: is read top-down rather than searched.
    FAVORITE_SECTIONS = (
        ("movies", lambda: _("Movies"), "Movie"),
        ("shows", lambda: _("Shows"), "Series"),
        ("episodes", lambda: _("Episodes"), "Episode"),
        ("videos", lambda: _("Videos"), "Video,MusicVideo"),
        ("artists", lambda: _("Artists"), "MusicArtist"),
        ("albums", lambda: _("Albums"), "MusicAlbum"),
        ("songs", lambda: _("Songs"), "Audio"),
        # jellyfin-web's global Favorites screen has no book rows -- it
        # reaches a favourited book through the books LIBRARY's Favorites
        # tab (constants/views/books.ts, slot 6), which is a per-library
        # tab strip this browser does not have. Without these two, marking
        # a book as a favourite would be an action with nowhere to see the
        # result. Same predicate, same shape rules; only the door differs.
        ("audiobooks", lambda: _("Audiobooks"), AUDIOBOOK_TYPE),
        ("books", lambda: _("Books"), BOOK_TYPE),
    )

    def get_favorite_sections(self, server_uuid, limit=24):
        """The Favorites screen's rows as ``[{"key", "title", "items",
        "types"}]``.

        Fanned out for the same reason the Programs screen is: seven
        independent queries walked serially is seven round trips before
        anything draws. Empty rows are dropped, so a user with no favourite
        albums has no Albums heading rather than an empty one.

        ``types`` rides along so the row's heading can link to the unbounded
        listing, exactly as the Live TV rows carry their filters.
        """
        def fetch(key, title, types):
            def work():
                items, _total = self._list_items(
                    server_uuid, {"type": "items", "include_item_types": types,
                                  "is_favorite": True},
                    "SortName", "Ascending", 0, limit, None)
                return {"key": key, "title": title(), "items": items,
                        "types": types}
            return work

        rows = []
        tasks = [fetch(*section) for section in self.FAVORITE_SECTIONS]
        with ThreadPoolExecutor(
                max_workers=min(HOME_FANOUT, max(1, len(tasks))),
                thread_name_prefix="favorites") as pool:
            for future in [pool.submit(task) for task in tasks]:
                try:
                    row = future.result()
                except Exception:
                    # One dead row must not cost the whole screen.
                    log.warning("Failed to load a favourites row",
                                exc_info=True)
                    continue
                if row["items"]:
                    rows.append(row)
        return rows

    def get_list(self, server_uuid, spec, sort_by="SortName",
                 sort_order="Ascending", start_index=0, limit=100,
                 filters=None):
        """``(items, total)`` for a generic list route -- jellyfin-web's
        ``#/list?type=…``.

        One entry point for every "see all" destination, because they are the
        same screen with different `where`: Next Up in full, a Live TV
        category beyond the twelve a row shows, everything in a genre, a
        studio's catalogue, the favourites.

        ``spec`` is a plain dict so it can live in a route and survive
        back-navigation. ``spec["type"]`` selects the query; everything else
        in it is that query's arguments. Unknown types raise rather than
        quietly returning an empty list -- a typo in a route should not look
        like an empty library.
        """
        kind = (spec or {}).get("type")
        if kind == "nextup":
            api = self._conn(server_uuid).api
            result = api.get_next(
                index=start_index, limit=limit, fields=LIST_FIELDS,
                enable_image_types="Primary,Thumb,Backdrop",
                image_type_limit=1) or {}
            return (result.get("Items", []),
                    result.get("TotalRecordCount", 0))
        if kind == "programs":
            # The six Programs rows cap at twelve with nothing behind them;
            # this is that nothing. The flags ride in the spec exactly as
            # PROGRAM_SECTIONS holds them.
            #
            # start_index and with_total are both load-bearing here, and
            # were both missing: the row form of this query takes neither,
            # so page 2 re-fetched page 1 and then reported len(items) as
            # the total, which shrank the page count back to one. A
            # 40-programme listing rendered as its first twelve, labelled
            # "1 / 1".
            return self.programs_page(
                server_uuid, start_index=start_index, limit=limit,
                with_total=True, **(spec.get("filters") or {}))
        if kind == "recordings":
            return self.recordings_page(
                server_uuid, start_index=start_index, limit=limit,
                with_total=True)
        if kind == "studios":
            # A flat list of Studio items rather than media -- the by-name
            # counterpart to genres. They render in the same grid because
            # they are tiles like any other; only the query differs.
            api = self._conn(server_uuid).api
            result = api.get_studios(
                parent_id=spec.get("parent_id"),
                include_item_types=spec.get("include_item_types"),
                start_index=start_index, limit=limit) or {}
            return (result.get("Items", []),
                    result.get("TotalRecordCount", 0))
        if kind == "items":
            return self._list_items(server_uuid, spec, sort_by, sort_order,
                                    start_index, limit, filters)
        raise ValueError("unknown list type %r" % (kind,))

    def _list_items(self, server_uuid, spec, sort_by, sort_order,
                    start_index, limit, filters):
        """The ``Users/{id}/Items`` half of :meth:`get_list`.

        Genre, studio, person and favourites are all one query with a
        different predicate, which is also how jellyfin-web routes them
        (``#/list?genreId=…``, ``&IsFavorite=true``).
        """
        api = self._conn(server_uuid).api
        kwargs = dict(self._filter_kwargs(filters))
        for key in ("genre_ids", "person_ids", "artist_ids",
                    "include_item_types", "parent_id"):
            if spec.get(key) is not None:
                kwargs[key] = spec[key]
        if spec.get("is_favorite"):
            kwargs["is_favorite"] = "true"
        # StudioIds has no named argument on get_user_items; params is the
        # documented way through for query parameters the signature does not
        # spell out, and it merges last.
        if spec.get("studio_ids") is not None:
            kwargs["params"] = {"StudioIds": spec["studio_ids"]}
        result = api.get_user_items(
            sort_by=sort_by,
            sort_order=sort_order,
            start_index=start_index,
            limit=limit,
            # Recursive: these predicates are about the whole library, not
            # one folder's direct children -- a genre listing that stopped at
            # the top level would be empty on any library with folders in it.
            recursive=True,
            fields=LIST_FIELDS,
            image_type_limit=1,
            enable_image_types="Primary,Thumb,Backdrop",
            **kwargs) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_library_items(self, server_uuid, parent_id, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100,
                          filters=None, image_type=None, collection_type=None):
        """One page of a library grid.

        ``image_type`` is the artwork the view is set to draw (see
        :func:`browse_image_types`); without it a library set to Banner gets
        no banner tags back and falls through to its thumbnails.

        ``collection_type`` says what kind of library this is, which decides
        whether the query is typed and recursive -- see
        :data:`LIBRARY_ITEM_TYPES`. A folder inside a library has none and is
        listed as it stands.
        """
        api = self._conn(server_uuid).api
        include = LIBRARY_ITEM_TYPES.get(collection_type or "")
        fields = (BOOKS_GRID_FIELDS if collection_type == BOOKS_COLLECTION
                  else GRID_FIELDS)
        # Built conditionally rather than passed as None: a folder listing
        # must send neither, and "the grid's own query" is a claim other code
        # relies on literally (get_play_all_ids, and the test that pins it).
        # Recursive only ever travels WITH the type filter -- on its own it is
        # how a TV library answers with 43,000 episodes.
        typed = ({"include_item_types": include, "recursive": True}
                 if include else {})
        result = api.get_user_items(
            parent_id=parent_id,
            **typed,
            sort_by=sort_by,
            sort_order=sort_order,
            start_index=start_index,
            limit=limit,
            fields=fields,
            image_type_limit=1,
            enable_image_types=browse_image_types(image_type),
            **self._filter_kwargs(filters)) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_movie_collections(self, server_uuid, sort_by="SortName",
                              sort_order="Ascending", start_index=0, limit=100,
                              filters=None, image_type=None):
        """Collections (BoxSets) as a paged grid, for the Movies-library
        Collections toggle. Server-wide and recursive (a BoxSet can gather
        items from several libraries), mirroring jellyfin-web's Collections
        view. Returns ``(items, total)`` like ``get_library_items``."""
        api = self._conn(server_uuid).api
        # get_user_items rather than the apiclient's get_collections: this grid
        # carries the same filter row as any other, and the collections helper
        # has no filter arguments.
        result = api.get_user_items(
            include_item_types="BoxSet",
            recursive=True,
            sort_by=sort_by,
            sort_order=sort_order,
            start_index=start_index,
            limit=limit,
            fields=GRID_FIELDS,
            image_type_limit=1,
            enable_image_types=browse_image_types(image_type),
            **self._filter_kwargs(filters)) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_person_items(self, server_uuid, person_id, start_index=0, limit=100,
                         sort_by="SortName", sort_order="Ascending"):
        """A person's filmography (movies + series they appear in)."""
        api = self._conn(server_uuid).api
        result = api.get_items_by_person(
            person_id,
            sort_by=sort_by,
            sort_order=sort_order,
            start_index=start_index,
            limit=limit,
            fields=GRID_FIELDS,
            image_type_limit=1,
            enable_image_types=browse_image_types()) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    # -- music browse ------------------------------------------------------

    def _music_items(self, server_uuid, include, parent_id, sort_by,
                     sort_order, start_index, limit, filters=None,
                     extra=None):
        api = self._conn(server_uuid).api
        kwargs = {
            "parent_id": parent_id,
            "include_item_types": include,
            "recursive": True,
            "sort_by": sort_by,
            "sort_order": sort_order,
            "start_index": start_index,
            "limit": limit,
            "fields": MUSIC_FIELDS,
            "image_type_limit": 1,
            "enable_image_types": "Primary",
        }
        if extra:
            kwargs.update(extra)
        kwargs.update(self._filter_kwargs(filters))
        result = api.get_user_items(**kwargs) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

    def get_music_albums(self, server_uuid, parent_id, sort_by="SortName",
                         sort_order="Ascending", start_index=0, limit=100,
                         filters=None):
        return self._music_items(server_uuid, "MusicAlbum", parent_id, sort_by,
                                 sort_order, start_index, limit, filters)

    def get_songs(self, server_uuid, parent_id, sort_by="Name",
                  sort_order="Ascending", start_index=0, limit=100,
                  filters=None):
        """A music library's tracks, A-Z by title.

        **Name, not SortName** -- the one place in this file where those two
        differ. A track's SortName is not its name: the server builds it from
        the disc and track numbers with the title only as a tie-break, which
        is what makes an album's own listing come out in play order. Ask a
        whole library for it and the ordering is by track number *across
        albums* -- every album's track 1, then every album's track 2 -- with
        the titles scattered through it. jellyfin-web's Songs tab has the same
        two entries in its sort menu and spells its "Track Name" one `Name`
        for this reason; the rest of its options end in SortName because they
        are all album-grouped first.
        """
        return self._music_items(server_uuid, "Audio", parent_id, sort_by,
                                 sort_order, start_index, limit, filters)

    def get_genre_albums(self, server_uuid, parent_id, genre_id,
                         sort_by="SortName", sort_order="Ascending",
                         start_index=0, limit=100, filters=None):
        return self._music_items(server_uuid, "MusicAlbum", parent_id, sort_by,
                                 sort_order, start_index, limit, filters,
                                 extra={"genre_ids": genre_id})

    def _artist_list(self, server_uuid, method_name, parent_id, sort_by,
                     sort_order, start_index, limit):
        api = self._conn(server_uuid).api
        result = getattr(api, method_name)(
            parent_id=parent_id,
            sort_by=sort_by,
            sort_order=sort_order,
            start_index=start_index,
            limit=limit,
            fields=MUSIC_FIELDS,
            image_type_limit=1,
            enable_image_types="Primary") or {}
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
        result = api.get_genres(parent_id,
                                include_item_types="MusicAlbum") or {}
        return result.get("Items", [])

    def get_album_tracks(self, server_uuid, album_id):
        """An album's tracks in disc/track order (children of the album)."""
        api = self._conn(server_uuid).api
        result = api.get_album_tracks(album_id, fields=MUSIC_FIELDS) or {}
        return result.get("Items", [])

    def get_artist_albums(self, server_uuid, artist_id):
        api = self._conn(server_uuid).api
        result = api.get_artist_albums(
            artist_id, fields=MUSIC_FIELDS, image_type_limit=1,
            enable_image_types="Primary") or {}
        return result.get("Items", [])

    def get_items_by_ids(self, server_uuid, ids):
        """Fetch DTOs for a list of ids, returned in the requested order (the
        server's Ids query does not preserve order). For the queue display.

        Batched: a big queue's ids as one ``Ids=`` param overflows the server's
        request-URI limit (HTTP 414). A partial (failed) batch just leaves those
        rows without metadata rather than losing the whole list.

        No fields: the only caller is the queue table, whose columns (index,
        name, artist, album, runtime) are all unconditional BaseItemDto
        properties — see LIST_FIELDS. It draws no artwork (``art=False``), so
        neither ``PrimaryImageAspectRatio`` nor ``ItemCounts`` reaches a pixel,
        and a field nothing reads is pure server work on a request that is
        already 100 ids wide.
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
                result = api.get_items(chunk, fields="") or {}
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
        result = api.get_artist_songs(artist_id, limit=limit,
                                      fields=MUSIC_FIELDS) or {}
        return result.get("Items", [])

    def get_genre_songs(self, server_uuid, parent_id, genre_id, limit=500):
        """All audio tracks in a genre. parent_id may be None (server-wide)."""
        api = self._conn(server_uuid).api
        result = api.get_genre_songs(genre_id, parent_id=parent_id or None,
                                     limit=limit, fields=MUSIC_FIELDS) or {}
        return result.get("Items", [])

    def get_instant_mix(self, server_uuid, item_id, limit=200):
        """A radio-style queue seeded from an album/artist/genre/song.

        Asks for **no** fields, as jellyfin-web does. The caller only wants the
        ids — it hands them to the player, which fetches the one item it is
        about to start. The apiclient's default here is the ``music_info()``
        set, and ``MediaStreams``/``People``/``ItemCounts`` are per-item
        lookups the server repeats for every one of the 200 results: it turned
        a single query into hundreds and took ~25s on a spinning-disk server.
        """
        api = self._conn(server_uuid).api
        try:
            result = api.get_instant_mix(item_id, limit=limit, fields="") or {}
        except Exception:
            return []
        return result.get("Items", [])

    def get_genres(self, server_uuid, parent_id=None):
        """Genre names available under a library (for the filter picker)."""
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id) or {}
        return [g.get("Name") for g in result.get("Items", []) if g.get("Name")]

    def get_filter_values(self, server_uuid, parent_id=None,
                          collection_type=None):
        """Filter-picker values: {"genres": [...], "years": [...]}.

        Years come from Items/Filters; where that is unavailable the year
        picker is simply empty and only the genre list is offered.

        ``collection_type`` scopes the scan to the type the grid lists, which
        is what web's filter menu does. Untyped, the endpoint walks every
        episode of every series to collect the genres of their shows: 3.7s
        against a real 950-series library here, 0.4s typed.
        """
        api = self._conn(server_uuid).api
        try:
            result = api.get_filters(
                parent_id,
                include_item_types=LIBRARY_ITEM_TYPES.get(
                    collection_type or "")) or {}
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
        """"More Like This" items."""
        api = self._conn(server_uuid).api
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

    def search_people(self, server_uuid, term, limit=PEOPLE_SEARCH_LIMIT):
        """People matching a search term.

        Its own query, and its own budget: /Persons is a different endpoint
        from the item search, so people can never be crowded out by episodes
        the way rows sharing SEARCH_LIMIT can.
        """
        api = self._conn(server_uuid).api
        result = api.get_persons(search_term=term, limit=limit) or {}
        return result.get("Items", [])

    def search_artists(self, server_uuid, term, limit=ARTIST_SEARCH_LIMIT):
        """Artists matching a search term.

        /Artists rather than the item query, which is what web does and what
        the item query cannot be relied on for: it answers with fewer
        artists on the server this was written against, and with none at all
        on at least one real one. This endpoint also covers track-level and
        featured artists, who have no MusicArtist item to be found as.
        """
        api = self._conn(server_uuid).api
        result = api.get_artists(search_term=term, limit=limit) or {}
        return result.get("Items", [])

    def get_playlists(self, server_uuid, limit=300):
        """All playlists, for the add-to-playlist picker."""
        api = self._conn(server_uuid).api
        result = api.get_playlists(limit=limit) or {}
        return result.get("Items", [])

    def get_collections(self, server_uuid, limit=300):
        """All user collections (BoxSets), for the add-to-collection picker."""
        api = self._conn(server_uuid).api
        result = api.get_collections(limit=limit, sort_by="SortName") or {}
        return result.get("Items", [])

    def get_shuffle_ids(self, server_uuid, parent_id,
                        limit=QUEUE_LIMIT):
        """Random playable item ids under a library, for shuffle play. The
        server does the shuffling so the sample spans the whole library, not
        just the loaded pages."""
        api = self._conn(server_uuid).api
        result = api.get_random_items(
            parent_id=parent_id,
            media_types=QUEUEABLE_MEDIA_PARAM,
            limit=limit,
            enable_images=False) or {}
        return [i["Id"] for i in result.get("Items", []) if i.get("Id")]

    def get_play_all_ids(self, server_uuid, parent_id, sort_by="SortName",
                         sort_order="Ascending", filters=None,
                         limit=QUEUE_LIMIT,
                         collection_type=None):
        """Ids Play All should queue, in the grid's own order.

        **Deliberately the grid's own query.** This used to ask a different
        one -- ``Recursive`` plus ``IncludeItemTypes=Movie,Episode,Video`` --
        and on a Home Videos album holding photos and clips it came back
        empty, so a folder whose videos were on screen reported nothing to
        play. Asking the same question the grid asks makes that class of
        disagreement unrepresentable: whatever is drawn is what is queued,
        and the answer cannot depend on a second query's filters agreeing
        with the first's.

        The filter is then :data:`QUEUEABLE_MEDIA`, on **MediaType**, not on
        ``Type``. Type is the concrete entity the scanner chose, which
        depends on which resolver ran -- the same clip is a ``Video`` in a
        Home Videos library and a ``Movie`` in a movies one -- whereas
        MediaType is the question actually being asked: is this a stream or
        a container?

        The recursive query survives only as the fallback for a level that
        is *all folders*, which is the top of a Home Videos library and the
        one case a flat listing genuinely cannot answer.

        Capped at ``limit``; a queue is not a library export.
        """
        items, _total = self.get_library_items(
            server_uuid, parent_id, sort_by=sort_by, sort_order=sort_order,
            limit=limit, filters=filters, collection_type=collection_type)
        ids = [i["Id"] for i in items
               if i.get("Id") and i.get("MediaType") in QUEUEABLE_MEDIA]
        if ids or not any(i.get("IsFolder") for i in items):
            return ids
        api = self._conn(server_uuid).api
        result = api.get_user_items(
            parent_id=parent_id,
            recursive=True,
            media_types=QUEUEABLE_MEDIA_PARAM,
            sort_by=sort_by,
            sort_order=sort_order,
            limit=limit,
            enable_images=False,
            **self._filter_kwargs(filters)) or {}
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
        editor's Public/Private control. Returns {} on servers too old to
        expose it."""
        api = self._conn(server_uuid).api
        try:
            return api.get_playlist(playlist_id) or {}
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
                              enable_image_types="Primary,Thumb,Backdrop",
                              image_type_limit=1) or {}
        items = result.get("Items", [])
        return items[0] if items else None

    def search(self, server_uuid, term, limit=SEARCH_LIMIT):
        """Everything matching ``term``, for the search screen to group.

        **The limit is shared across every type, which is why it is large.**
        The screen splits the answer into a row per type, so a budget of 60
        was not "60 of each" -- it was 60 between them, handed out in
        whatever order the server sorted them. One series with a matching
        name brings its episodes along, and a term that hits an episode
        title spends the lot: the movie the user was looking for never
        arrived, and the row for it simply did not appear (#641).

        jellyfin-web asks the same endpoint for 800 and splits client-side
        exactly as we do, so this is its number. It also turns the total
        count off, which we now do too -- nothing here shows a total, and
        counting matches is work the server can skip.
        """
        api = self._conn(server_uuid).api
        result = api.get_user_items(
            search_term=term,
            include_item_types=SEARCH_TYPES,
            recursive=True,
            limit=limit,
            enable_total_record_count=False,
        ) or {}
        return result.get("Items", [])

    # -- images ------------------------------------------------------------

    def image_spec(self, item, image_type="Primary", width=280, inherit=True):
        """Resolve which (item_id, type, tag) actually carries the image.

        Falls back from an item's own image to its series/parent image so
        episodes and seasons still show art. Returns ``None`` when there is no
        usable image (caller shows a placeholder).

        ``image_type`` is a *request*, not a promise: ``"Thumb"`` means "this
        is a landscape tile, find me something landscape-shaped", and is
        jellyfin-web's ``preferThumb: true`` (``cardbuilder/utils/url.ts``).
        ``"Primary"`` means a poster or square one. The type actually returned
        is whatever the chain found.

        ``inherit`` is web's ``inheritThumb``: with it off, a Thumb request
        stops borrowing from the series and the season and resolves against
        the item alone. Default on, matching web's default (its
        ``useEpisodeImagesInNextUpAndResume`` setting defaults to *false*,
        which reads as ``inheritThumb: true``).

        **The Thumb chain used to reach the item's own Primary second**, so a
        recorded episode -- which typically carries only a Primary while its
        series carries Thumb and Backdrop -- put a 2:3 poster in a 16:9 tile.
        Four landscape-shaped candidates come first now, which is web's order.

        **The Primary chain is deliberately not web's.** Ours ends with the
        channel logo (see below), which web has no equivalent for, and unlike
        web it does not fall back to a Backdrop -- that would put 16:9 art in
        a 2:3 tile, the same defect in the other direction.
        """
        tags = item.get("ImageTags") or {}
        backdrops = item.get("BackdropImageTags") or []
        if image_type in tags:
            return item["Id"], image_type, tags[image_type]

        if image_type in GRID_IMAGE_TYPES:
            # Banner / Logo / Disc are asked for by name, and about half a TV
            # library carries no banner (measured: 107 of 200 on one server,
            # 199 of 200 on another). jellyfin-web drops straight into the
            # Primary chain for those (url.ts:42-52), which puts a poster in a
            # 5.4:1 frame; ours goes through the OTHER wordmark first.
            #
            # A banner and a logo are the same artwork wearing different
            # margins -- the show's name, drawn to be read on a bar -- so each
            # stands in for the other far better than the poster does, and a
            # mixed library comes out as one row instead of half title cards
            # and half poster slices. That is the whole divergence from web
            # here, and it is deliberate.
            for candidate in _WORDMARK_CHAIN.get(image_type, ()):
                if candidate in tags:
                    return item["Id"], candidate, tags[candidate]
                if candidate == "Logo" and item.get("ParentLogoItemId") \
                        and item.get("ParentLogoImageTag"):
                    # web's url.ts:51 -- an episode has no logo of its own
                    # and its series does.
                    return (item["ParentLogoItemId"], "Logo",
                            item["ParentLogoImageTag"])
            image_type = "Primary"
            if image_type in tags:
                return item["Id"], image_type, tags[image_type]

        if item.get("Type") == "Playlist":
            # A playlist has its own (square) Primary image — ask the server
            # for it directly rather than borrowing a member's poster. Asked
            # for even with no tag in the DTO, since the server generates
            # playlist art; a genuine miss is a 404 that the thumbnail store
            # records and stops retrying.
            return item["Id"], "Primary", tags.get("Primary") or "playlist"

        if image_type == "ParentPrimary":
            # Draw the item as its show: the *series'* poster, which is what
            # /Items/{ParentBackdropItemId}/Images/Primary resolves to — that
            # field is the series' id, not a request for its backdrop. Not
            # reachable through the chain below, because an episode's own
            # Primary (its still) is matched first and wins. Falls through
            # when there is no parent, so a stray item still gets its own art.
            owner = item.get("SeriesId") or item.get("ParentBackdropItemId")
            if owner:
                return owner, "Primary", item.get("SeriesPrimaryImageTag")

        if image_type == "Thumb":
            # jellyfin-web's preferThumb ladder, url.ts:55-82. Everything
            # landscape-shaped is tried before the item's own poster: a
            # recorded Episode usually has only a Primary of its own while
            # the Series carries the Thumb and the Backdrop, which is why
            # recorded TV showed this worst.
            if inherit and item.get("SeriesId") \
                    and item.get("SeriesThumbImageTag"):
                return item["SeriesId"], "Thumb", item["SeriesThumbImageTag"]

            if (inherit and item.get("ParentThumbItemId")
                    and item.get("ParentThumbImageTag")
                    # web excludes photos here (url.ts:59): a photo's parent
                    # is its album, whose thumb is some other photo.
                    and item.get("MediaType") != "Photo"):
                return (item["ParentThumbItemId"], "Thumb",
                        item["ParentThumbImageTag"])

            if backdrops:
                return item["Id"], "Backdrop", backdrops[0]

            # Episodes only, exactly as web has it (url.ts:67-70). The reason
            # is not obvious -- a season's backdrop is a fine landscape image
            # for anything -- but the gate is cheap and being bug-compatible
            # here is worth more than being clever.
            parent_backdrops = item.get("ParentBackdropImageTags") or []
            if (inherit and item.get("Type") == "Episode"
                    and item.get("ParentBackdropItemId") and parent_backdrops):
                return (item["ParentBackdropItemId"], "Backdrop",
                        parent_backdrops[0])

            if "Primary" in tags:
                return item["Id"], "Primary", tags["Primary"]

        if item.get("PrimaryImageTag"):
            # People entries carry a bare PrimaryImageTag instead of ImageTags.
            return item["Id"], "Primary", item["PrimaryImageTag"]

        if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
            return item["SeriesId"], "Primary", item["SeriesPrimaryImageTag"]

        if (image_type != "Thumb" and item.get("ParentPrimaryImageItemId")
                and item.get("ParentPrimaryImageTag")):
            # How a SeriesTimer carries its artwork: the DTO is not an item,
            # so it has no ImageTags of its own — the poster belongs to the
            # *series* the rule was made from, and these two fields are the
            # only pointer to it. Without this the whole Series tab was a
            # wall of placeholder glyphs.
            #
            # SeriesTimerInfoDto only: a plain TimerInfoDto has no
            # ParentPrimaryImage* at all (nor any ImageTags), and falls
            # through to the channel-logo branch below — which is also what
            # jellyfin-web's schedule shows, via showChannelLogo.
            #
            # Not for a Thumb request: that means a landscape tile, and the
            # parent *thumb* below is the right shape for one. This branch
            # is repeated after it as the fallback for when there is none.
            return (item["ParentPrimaryImageItemId"], "Primary",
                    item["ParentPrimaryImageTag"])

        if "Thumb" in tags:
            # The mirror of the Thumb->Primary fallback above: guide programs
            # routinely carry one of the two and not the other, so a poster
            # row asking for Primary must still take the item's own thumb
            # before it borrows the channel's.
            return item["Id"], "Thumb", tags["Thumb"]

        if (inherit and item.get("ParentThumbItemId")
                and item.get("ParentThumbImageTag")):
            # Live TV programs inherit the channel's thumb this way. Gated on
            # inherit to match web (url.ts:126), though only a Thumb request
            # ever passes inherit=False today -- and that one tried this
            # above.
            return (item["ParentThumbItemId"], "Thumb",
                    item["ParentThumbImageTag"])

        if (item.get("ParentPrimaryImageItemId")
                and item.get("ParentPrimaryImageTag")):
            # The Thumb-request half of the branch above: a timer whose
            # programme has a poster but no thumb still gets its artwork
            # rather than falling through to the channel logo.
            return (item["ParentPrimaryImageItemId"], "Primary",
                    item["ParentPrimaryImageTag"])

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
        if index is None and image_type == "Backdrop":
            # Backdrops are a numbered set, and image_spec resolves to the
            # first one (BackdropImageTags[0]) without room in its 3-tuple to
            # say so. The server does serve /Images/Backdrop unindexed, but
            # only the indexed form is guaranteed to match the tag we cached
            # the bitmap under -- backdrop_url has always passed index=0 for
            # the same reason.
            index = 0
        # include_apikey=False throughout: the thumbnail store sends the
        # Authorization header instead (see auth_origins). Images are the
        # highest-volume first-party traffic here, so a token in these query
        # strings is a token in the access log of every one of them.
        if fill and height:
            # Crop to the exact tile aspect so wide library/banner art still
            # reads as a uniform poster instead of a letterboxed thumbnail.
            return api.image_url(item_id, image_type, index=index, tag=tag,
                                 fill_width=int(width), fill_height=int(height),
                                 include_apikey=False)
        return api.image_url(item_id, image_type, index=index, tag=tag,
                             max_width=int(width), include_apikey=False)

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
        """``(owner_item_id, image_type, tag)`` for the header banner, or
        None when this item has nothing wide enough to draw one from.

        The tag is part of the cache key, so it has to be the real one or the
        same item cached from another source — or against an older backdrop —
        is served forever. The *type* is in there too because the chain no
        longer ends at Backdrop.

        **The landscape fallbacks matter for video that is not a film.** A
        home video, a clip, a recording — anything scanned out of a folder
        rather than matched against a metadata provider — has no backdrop and
        never will, and the header was a blank grey box for it. What it does
        have is the still the server extracted, which is a frame of the thing
        itself and exactly what belongs across the top of its page.

        **A poster is not a fallback.** The banner is ~2.67:1; a 2:3 poster
        cropped into it is a horizontal strip through the middle of the key
        art, which is worse than the placeholder because it looks like a
        rendering fault rather than like missing artwork. So the Primary step
        is taken only when the item's own aspect ratio says it is landscape
        (``_LANDSCAPE_ART``), which is what tells a home video's extracted
        frame apart from a film's poster. Where that leaves nothing, the
        caller still draws its heading as text — see ``backdrop_node``.
        """
        tags = item.get("BackdropImageTags") or []
        if tags:
            return item["Id"], "Backdrop", tags[0]
        parent_tags = item.get("ParentBackdropImageTags") or []
        if parent_tags and item.get("ParentBackdropItemId"):
            return item["ParentBackdropItemId"], "Backdrop", parent_tags[0]
        image_tags = item.get("ImageTags") or {}
        if image_tags.get("Thumb"):
            return item["Id"], "Thumb", image_tags["Thumb"]
        if item.get("ParentThumbImageTag") and item.get("ParentThumbItemId"):
            return (item["ParentThumbItemId"], "Thumb",
                    item["ParentThumbImageTag"])
        ratio = item.get("PrimaryImageAspectRatio")
        if (image_tags.get("Primary")
                and isinstance(ratio, (int, float))
                and ratio >= _LANDSCAPE_ART):
            return item["Id"], "Primary", image_tags["Primary"]
        return None

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        spec = self.backdrop_spec(item)
        if spec is None:
            return None
        owner_id, image_type, tag = spec
        # index=0 for a Backdrop only: backdrops are a numbered set and the
        # indexed form is what matches the tag cached above, while a Thumb or
        # a Primary is a single image and has no index at all.
        return self.image_url(server_uuid, owner_id, image_type, tag,
                              width, height=height, fill=fill,
                              index=0 if image_type == "Backdrop" else None)


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
                 playlist_items=None, playlist_server=None,
                 books=None, book_items=None):
        self.rows = rows or {}
        self.items = items or []
        self.series_server = series_server or {}
        self.season_server = season_server or {}
        self.season_series = season_series or {}
        self.playlists = playlists or []
        self.playlist_items = playlist_items or {}
        self.playlist_server = playlist_server or {}
        #: The top level of the offline books library: every downloaded
        #: `Book`, plus one synthesized container per multi-file audiobook.
        self.books = books or []
        #: container id -> its AudioBook chapters, in reading order.
        self.book_items = book_items or {}
        self.art_cache = {}


class OfflineLibrarySource:
    """LibrarySource-compatible browser backed by the offline catalog.

    Mirrors the normal browsing UI (libraries → grids → series → seasons →
    episodes) filtered to downloaded content, with artwork from local files.
    Reads the catalog read-only and caches rows in memory.
    """

    def __init__(self, catalog_path):
        self.catalog_path = catalog_path
        self.root: Optional[str] = (os.path.dirname(catalog_path)
                                    if catalog_path else None)
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
        # series_id -> server_id (for series artwork)
        series_server: dict[str, Any] = {}
        # season_id -> server_id (for season artwork)
        season_server: dict[str, Any] = {}
        # season_id -> series_id (artwork fallback)
        season_series: dict[str, Any] = {}
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
        books, book_items = self._book_shelf(items)
        self._snap = _OfflineSnapshot(
            rows=by_id, items=items, series_server=series_server,
            season_server=season_server, season_series=season_series,
            playlists=playlist_dtos, playlist_items=playlist_items,
            playlist_server=playlist_server,
            books=books, book_items=book_items)

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
        if snap.books:
            # CollectionType matters here rather than being decoration: the
            # shell reads it to route a books library to BooksPage, and
            # BooksPage is what draws a folder of chapters as an album and
            # what withholds Play All from a shelf. It is also INHERITED
            # down the tree (app._open_item), which is what carries a
            # synthesized container to the same page.
            libs.append({"Id": "offline:books", "Name": _("Books"),
                         "Type": "CollectionFolder",
                         "CollectionType": BOOKS_COLLECTION,
                         "ImageTags": {}})
        if snap.playlists:
            libs.append({"Id": "offline:playlists", "Name": _("Playlists"),
                         "Type": "CollectionFolder", "CollectionType": "playlists",
                         "ImageTags": {}})
        return libs

    def _series_list(self, snap=None):
        snap = snap or self._snap
        episodes_by_series: dict[str, list[Any]]
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
                 # A synthesized DTO has to answer the questions a real one
                 # would, and the grid asks this one: GridPage._grid_shape
                 # takes the MEDIAN PrimaryImageAspectRatio across the row and
                 # falls back to SQUARE when nothing carries one -- so a
                 # downloaded-shows grid came out as square cards with the
                 # posters letterboxed inside them. A Series' Primary image is
                 # a poster; the server says 2:3 for these, and the offline
                 # catalog stores episodes, not the series, so there is
                 # nothing else to read it from.
                 "PrimaryImageAspectRatio": 2 / 3,
                 "UserData": self._aggregate_userdata(episodes_by_series[sid])}
                for sid in order]

    @staticmethod
    def _book_shelf(items):
        """The top level of the offline books library, and its containers.

        **A books library browses by folder, and offline there are no
        folders** — ``sync.manager._expand`` lists one and downloads its
        children, so the catalog holds leaves. Nothing else in the DTO puts
        them back together either, and the two halves of a books library
        disagree about which field would: measured against a real server, a
        ``Book`` carries ``SeriesName`` (the folder, or a real series when
        the file is tagged) and no ``Album``; an ``AudioBook`` carries
        ``Album`` and ``AlbumArtist`` and no ``SeriesName``; and neither
        carries ``ParentId`` under the ``Fields`` the downloader asks for.

        So the shelf is rebuilt from what is actually there, and only where
        it changes what can be done:

        * A **``Book``** is one file and one thing to read. It stands on its
          own, exactly as it does in a folder online.
        * **``AudioBook``s that share an album** are the chapters of one
          book, which is the case that needs a container: without one, a
          twelve-part recording is twelve tiles and playing "from chapter
          four" is the only way to start it. They get a synthesized
          ``Folder``, which is what ``BooksPage`` already draws as an album.
        * A **lone ``AudioBook``** is left alone, because a container around
          one chapter is a click that leads to the same thing — the same
          reason the online screens give a loose audiobook a page of its own.

        Grouping on ``AlbumId`` where the server gave one and on the album
        name otherwise: the id is stable across a retag, and the name is all
        an untagged rip has. A recording with neither is treated as loose,
        which is the honest answer — nothing joins those files.
        """
        loose, groups, order = [], {}, []
        for item in items:
            kind = item.get("Type")
            if kind == BOOK_TYPE:
                loose.append(item)
                continue
            if kind != AUDIOBOOK_TYPE:
                continue
            key = item.get("AlbumId") or (item.get("Album") or "").strip()
            if not key:
                loose.append(item)
                continue
            if key not in groups:
                groups[key] = []
                order.append(key)
            groups[key].append(item)
        shelf, contents = list(loose), {}
        for key in order:
            members = groups[key]
            if len(members) == 1:
                shelf.append(members[0])
                continue
            first = members[0]
            # IndexNumber first, because that is the chapter order and the
            # names of a rip are frequently "Track 01" ... "Track 10", which
            # sort wrong as text. Falls back to the name, which is what an
            # untagged set has.
            members = sorted(members, key=lambda i: (i.get("IndexNumber") or 0,
                                                     (i.get("Name") or "").lower()))
            cid = "offline:book:%s" % key
            contents[cid] = members
            shelf.append({
                "Id": cid, "Name": first.get("Album") or first.get("Name") or "",
                "Type": "Folder", "ImageTags": {},
                # An audiobook's cover is square, and a container with no
                # opinion is drawn as a poster -- which letterboxes every
                # one of them. Same reasoning as the synthesized Series
                # above, with the other answer.
                "PrimaryImageAspectRatio": 1.0,
                "AlbumArtist": first.get("AlbumArtist"),
                "ChildCount": len(members),
                "UserData": OfflineLibrarySource._aggregate_userdata(members),
            })
        return shelf, contents

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
        # Heterogeneous by design: titles and item lists go in first, the
        # slot/kind ints are stamped on below.
        rows: list[dict[str, Any]] = []
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
        if snap.books:
            rows.append({"title": _("Downloaded Books"), "items": snap.books,
                         "collection_type": BOOKS_COLLECTION})
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
                          filters=None, image_type=None, collection_type=None):
        # image_type and collection_type are accepted and ignored: one asks
        # the server for a kind of artwork (a downloaded item has one file per
        # type on disk) and the other scopes a server-side query. The offline
        # parent ids below already say what they list.
        snap = self._snap
        if parent_id == "offline:movies":
            items = [i for i in snap.items if i.get("Type") == "Movie"]
        elif parent_id == "offline:videos":
            items = [i for i in snap.items if i.get("Type") == "Video"]
        elif parent_id == "offline:tv":
            items = self._series_list(snap)
        elif parent_id == "offline:books":
            items = list(snap.books)
        elif parent_id in snap.book_items:
            # The chapters of one audiobook, in reading order. Returned
            # whole and NOT re-sorted below: BooksPage plays them as a
            # queue, and SortName would put chapter 10 after chapter 1.
            items = snap.book_items[parent_id]
            return items[start_index:start_index + limit], len(items)
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

    def has_live_tv(self, server_uuid):
        """There is no tuner in a folder of downloaded files.

        Declared rather than left to fail: the offline source is what the
        online one falls back TO, so a missing method here turns a degraded
        screen into an AttributeError on the fallback path.
        """
        return False

    def syncplay_access(self, server_uuid):
        """Nobody to sync with, and no server to ask. Declared for the same
        reason has_live_tv is."""
        from ..user_policy import NO_SYNCPLAY

        return NO_SYNCPLAY

    def can_manage_live_tv(self, server_uuid):
        return False

    def can_manage_collections(self, server_uuid):
        """No server to write to. Declared for the same reason the others
        are: the offline source is what the online one falls back TO."""
        return False

    def can_download(self, server_uuid):
        """Nothing to download FROM. The book screens read this to decide
        whether to offer a fetch, and offline the answer is no -- what is
        already on disk is still readable, and that path does not ask."""
        return False

    def get_genres(self, server_uuid, parent_id=None):
        genres: set[str] = set()
        for i in self._snap.items:
            genres.update(i.get("Genres") or [])
        return sorted(genres)

    def get_filter_values(self, server_uuid, parent_id=None,
                          collection_type=None):
        years = {i.get("ProductionYear") for i in self._snap.items
                 if i.get("ProductionYear")}
        return {"genres": self.get_genres(server_uuid, parent_id),
                "years": sorted(years, reverse=True)}

    def get_similar(self, server_uuid, item_id, limit=12):
        return []  # no similarity data in the offline catalog

    def get_trailers(self, server_uuid, item_id):
        return []  # trailers aren't downloaded

    def search_people(self, server_uuid, term, limit=PEOPLE_SEARCH_LIMIT):
        return []  # people aren't cached offline

    def search_artists(self, server_uuid, term, limit=ARTIST_SEARCH_LIMIT):
        # Artists are entities the server derives from its library; the
        # downloaded catalog holds items. Names could be scraped off the
        # tracks, but a tile built from one has no id to open, and a row of
        # dead tiles is worse than no row.
        return []

    def get_playlists(self, server_uuid, limit=300):
        return list(self._snap.playlists)

    def get_collections(self, server_uuid, limit=300):
        return []  # collections aren't cached offline (editing is online-only)

    def get_shuffle_ids(self, server_uuid, parent_id,
                        limit=QUEUE_LIMIT):
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

    def get_play_all_ids(self, server_uuid, parent_id, sort_by="SortName",
                         sort_order="Ascending", filters=None,
                         limit=QUEUE_LIMIT,
                         collection_type=None):
        """Downloaded items of a library, in the grid's order.

        Reuses the grid loader rather than re-deriving the pools: it already
        knows how each offline parent id maps onto the snapshot, and the two
        drifting is how Play All would come to queue something the grid does
        not show.
        """
        items, _total = self.get_library_items(
            server_uuid, parent_id, sort_by=sort_by, sort_order=sort_order,
            limit=limit, filters=filters)
        return [i["Id"] for i in items
                if i.get("Id") and i.get("MediaType") in QUEUEABLE_MEDIA]

    def chapter_image_url(self, server_uuid, item_id, chapter_index, chapter,
                          width=320):
        return None  # chapter thumbnails aren't downloaded

    def get_playlist_items(self, server_uuid, playlist_id):
        """Downloaded items of a playlist, in playlist order."""
        return list(self._snap.playlist_items.get(playlist_id, []))

    def get_seasons(self, server_uuid, series_id):
        snap = self._snap
        episodes_by_key: dict[str, list[Any]]
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
        # A synthesized audiobook container. BooksPage asks for the folder's
        # own DTO to draw the album header, so this is not optional -- the
        # album renders with no title and no action bar without it.
        if item_id in snap.book_items:
            return next((b for b in snap.books if b.get("Id") == item_id), None)
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

    def get_by_name_sections(self, server_uuid, spec, limit=20):
        """Empty offline: every row is a server-side predicate over the
        whole library. Signature parity with the live source."""
        return []

    def get_genre_sections(self, server_uuid, parent_id, collection_type,
                           limit=10, max_genres=40):
        """Empty offline: a genre row is a server-side GenreIds query over
        the whole library. Signature parity, as with every other source
        method -- the offline catalog is the failure path's fallback."""
        return []

    def get_view_settings(self, server_uuid, parent_id, collection_type):
        """No server, no saved view settings. Signature parity."""
        return {}

    def get_favorite_sections(self, server_uuid, limit=24):
        """Empty offline, and hidden rather than shown empty -- see the
        Favorites nav button. Present for signature parity: the offline
        source is what a failed load falls back TO."""
        return []

    def get_list(self, server_uuid, spec, sort_by="SortName",
                 sort_order="Ascending", start_index=0, limit=100,
                 filters=None):
        """Signature parity with the live source, which is load-bearing: the
        offline catalog is what a failed load falls back TO, so a call it
        cannot accept makes the fallback itself raise.

        Answers empty rather than guessing. Every list type is a server-side
        predicate -- still-to-air programmes, a genre across a library, the
        favourites -- and a downloaded subset cannot stand in for any of
        them; a partial answer here would read as "you have nothing in this
        genre" rather than "this needs the server".
        """
        return [], 0

    def get_next_up(self, server_uuid, series_id):
        eps = self.get_series_queue(server_uuid, series_id)
        for ep in eps:
            if not (ep.get("UserData") or {}).get("Played"):
                return ep
        return eps[0] if eps else None

    def search(self, server_uuid, term, limit=SEARCH_LIMIT):
        # Same budget as online, and for the same reason -- the screen above
        # splits one answer into a row per type. A downloaded library is
        # small enough that this is the whole of it either way.
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
            return self._representative(("Movie",), snap, self.root)
        if item_id == "offline:videos":
            return self._representative(("Video",), snap, self.root)
        if item_id == "offline:tv":
            for series_id in snap.series_server:
                path = self._art_path(series_id, "Primary", snap)
                if path:
                    return path
            return self._representative(("Episode",), snap, self.root)
        if item_id == "offline:playlists":
            for pid in snap.playlist_server:
                path = self._art_path(pid, image_type, snap)
                if path:
                    return path
            return None
        # A synthesized audiobook container has no download of its own, so
        # it borrows its chapters' cover -- which is the same file for all
        # of them, this being one recording.
        members = snap.book_items.get(item_id)
        if members:
            for member in members:
                path = self._art_path(member.get("Id"), image_type, snap)
                if path:
                    return path
            return None
        if item_id == "offline:books":
            return self._representative((BOOK_TYPE, AUDIOBOOK_TYPE), snap,
                                        self.root)
        return None

    def _representative(self, types, snap, root: str):
        """Artwork from any downloaded item of ``types``, as a stand-in for a
        synthetic library tile.

        ``root`` is passed in rather than read from ``self``: every caller is
        downstream of ``_art_path_uncached``'s "no root" early return, so it
        is known non-None there. Taking it as an argument puts that in the
        signature instead of in a comment — a fourth call site from somewhere
        that had not checked would previously have produced
        ``os.path.join(None, ...)``, and no amount of casting would have
        caught it.
        """
        for row in snap.rows.values():
            if row.get("type") in types and row.get("file_path"):
                path = self._in_dir(os.path.join(
                    root, os.path.dirname(row["file_path"])), "poster.jpg")
                if path:
                    return path
        return None

    def image_spec(self, item, image_type="Primary", width=280, inherit=True):
        # inherit is accepted and ignored: it selects between an item's own
        # artwork and its series', and a downloaded item has exactly one file
        # per type on disk (_art_path already falls back season -> series).
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
        source switches can't serve each other's cached bitmaps.

        Three-tuple like the online one, and the middle element is the
        sentinel rather than a type: a downloaded item has exactly one header
        image on disk whatever it was online, so there is no chain here to
        name a step of."""
        return item.get("Id"), "offline", "offline"

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        return self._art_path(item.get("Id"), "Backdrop")
