"""Browse-only data access for the library browser.

``LibrarySource`` is the seam the UI depends on. Today it is backed by live
Jellyfin connections; a future offline build can provide an object with the same
method surface backed by the local sync catalog without touching the views.

Every method returns plain Jellyfin item DTO dicts (or lists of them) so the
same shapes work whether they came from the server or a local cache.
"""

import logging
import urllib.parse

from jellyfin_apiclient_python import JellyfinClient

from ..constants import USER_APP_NAME, CLIENT_VERSION, USER_AGENT
from ..i18n import _

log = logging.getLogger("library_browser.repository")

# Fields requested for grids/rows. Kept lean for speed.
LIST_FIELDS = "PrimaryImageAspectRatio,Overview,ProductionYear"

# Fields requested for the detail view. Intentionally a superset (MediaSources,
# MediaStreams, People, ...) so cached DTOs are already complete for the eventual
# offline-sync feature.
DETAIL_FIELDS = (
    "Path,Overview,Genres,Studios,People,Taglines,SortName,"
    "OfficialRating,CommunityRating,CriticRating,ProductionYear,"
    "MediaSources,MediaStreams,Chapters,ProviderIds,PremiereDate,"
    "PrimaryImageAspectRatio,DateCreated"
)

# CollectionTypes we do not surface (video-only browser, phase 1).
EXCLUDED_COLLECTION_TYPES = {"music", "musicvideos", "books", "playlists", "livetv"}

# Item types that open the detail/play view rather than drilling deeper.
PLAYABLE_TYPES = {"Movie", "Episode", "Video", "MusicVideo"}
# Item types that drill into a series view.
SERIES_TYPES = {"Series"}
# Item types that drill into another grid (by ParentId).
FOLDER_TYPES = {"CollectionFolder", "Folder", "BoxSet", "Season", "UserView"}


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
        for item in result.get("Items", []):
            if item.get("CollectionType") in EXCLUDED_COLLECTION_TYPES:
                continue
            out.append(item)
        return out

    def get_home_rows(self, server_uuid):
        """Return the ordered rows shown on the home screen.

        Each row is ``{"title": str, "items": [DTO, ...]}``; empty rows are
        dropped so the home screen only shows what exists.
        """
        api = self._conn(server_uuid).api
        rows = []

        try:
            resume = api.user_items(params={
                "Recursive": True,
                "Filters": "IsResumable",
                "SortBy": "DatePlayed",
                "SortOrder": "Descending",
                "IncludeItemTypes": "Movie,Episode,Video",
                "Limit": 20,
                "Fields": LIST_FIELDS,
                "EnableImageTypes": "Primary,Thumb,Backdrop",
            }) or {}
            rows.append((_("Continue Watching"), resume.get("Items", [])))
        except Exception:
            log.warning("Failed to load resume items", exc_info=True)

        try:
            nextup = api.get_next(limit=20) or {}
            rows.append((_("Next Up"), nextup.get("Items", [])))
        except Exception:
            log.warning("Failed to load next-up items", exc_info=True)

        for title, media in ((_("Recently Added Movies"), "Movie"),
                             (_("Recently Added Episodes"), "Episode")):
            try:
                latest = api.get_recently_added(media=media, limit=20) or []
                # get_recently_added returns a bare list, not an Items dict.
                items = latest.get("Items", []) if isinstance(latest, dict) else latest
                rows.append((title, items))
            except Exception:
                log.warning("Failed to load recently added (%s)", media, exc_info=True)

        return [{"title": t, "items": i} for t, i in rows if i]

    def get_library_items(self, server_uuid, parent_id, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100):
        api = self._conn(server_uuid).api
        result = api.user_items(params={
            "ParentId": parent_id,
            "SortBy": sort_by,
            "SortOrder": sort_order,
            "StartIndex": start_index,
            "Limit": limit,
            "Fields": LIST_FIELDS,
            "ImageTypeLimit": 1,
            "EnableImageTypes": "Primary,Thumb,Backdrop",
        }) or {}
        return result.get("Items", []), result.get("TotalRecordCount", 0)

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
        return api.users("/Items/%s" % item_id, params={"Fields": DETAIL_FIELDS})

    def get_series_queue(self, server_uuid, series_id, start_item_id=None, limit=100):
        """Episodes for a series in aired order, ACROSS seasons, optionally
        starting at ``start_item_id`` — this is how the play queue crosses
        season boundaries (mirrors jellyfin-web's getEpisodes with startItemId
        and no SeasonId)."""
        api = self._conn(server_uuid).api
        params = {"UserId": "{UserId}", "Limit": limit, "Fields": LIST_FIELDS}
        if start_item_id:
            params["StartItemId"] = start_item_id
        result = api.shows("/%s/Episodes" % series_id, params) or {}
        return result.get("Items", [])

    def get_next_up(self, server_uuid, series_id):
        """The next episode to watch for a series (resume or next unwatched)."""
        api = self._conn(server_uuid).api
        result = api.shows("/NextUp", {
            "UserId": "{UserId}",
            "SeriesId": series_id,
            "Limit": 1,
            "Fields": LIST_FIELDS,
            "EnableImageTypes": "Primary,Thumb,Backdrop",
        }) or {}
        items = result.get("Items", [])
        return items[0] if items else None

    def search(self, server_uuid, term, limit=60):
        api = self._conn(server_uuid).api
        result = api.search_media_items(
            term=term, media="Movie,Series,Episode,Video", limit=limit
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
        if image_type in tags:
            return item["Id"], image_type, tags[image_type]

        if image_type == "Thumb":
            # Fall back to a primary image, then the series thumb/primary.
            if "Primary" in tags:
                return item["Id"], "Primary", tags["Primary"]

        if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
            return item["SeriesId"], "Primary", item["SeriesPrimaryImageTag"]

        if item.get("AlbumId") and item.get("AlbumPrimaryImageTag"):
            return item["AlbumId"], "Primary", item["AlbumPrimaryImageTag"]

        if "Primary" in tags:
            return item["Id"], "Primary", tags["Primary"]

        return None

    def image_url(self, server_uuid, item_id, image_type, tag, width,
                  height=None, fill=False):
        conn = self._conn(server_uuid)
        params = {"quality": 90, "api_key": conn.token}
        if fill and height:
            # Crop to the exact tile aspect so wide library/banner art still
            # reads as a uniform poster instead of a letterboxed thumbnail.
            params["fillWidth"] = int(width)
            params["fillHeight"] = int(height)
        else:
            params["maxWidth"] = int(width)
        if tag:
            params["tag"] = tag
        return "%s/Items/%s/Images/%s?%s" % (
            conn.address, item_id, image_type, urllib.parse.urlencode(params)
        )

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        tags = item.get("BackdropImageTags") or []
        if tags:
            return self.image_url(server_uuid, item["Id"], "Backdrop/0", tags[0],
                                  width, height=height, fill=fill)
        parent_tags = item.get("ParentBackdropImageTags") or []
        if parent_tags and item.get("ParentBackdropItemId"):
            return self.image_url(
                server_uuid, item["ParentBackdropItemId"], "Backdrop/0",
                parent_tags[0], width, height=height, fill=fill)
        return None
