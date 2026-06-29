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

from jellyfin_apiclient_python import JellyfinClient

from ..constants import USER_APP_NAME, CLIENT_VERSION, USER_AGENT
from ..i18n import _
from ..sync.db import SyncDB, STATUS_COMPLETE

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
                  height=None, fill=False, index=None):
        api = self._conn(server_uuid).api
        if fill and height:
            # Crop to the exact tile aspect so wide library/banner art still
            # reads as a uniform poster instead of a letterboxed thumbnail.
            return api.image_url(item_id, image_type, index=index, tag=tag,
                                 fill_width=int(width), fill_height=int(height))
        return api.image_url(item_id, image_type, index=index, tag=tag,
                             max_width=int(width))

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        tags = item.get("BackdropImageTags") or []
        if tags:
            return self.image_url(server_uuid, item["Id"], "Backdrop", tags[0],
                                  width, height=height, fill=fill, index=0)
        parent_tags = item.get("ParentBackdropImageTags") or []
        if parent_tags and item.get("ParentBackdropItemId"):
            return self.image_url(
                server_uuid, item["ParentBackdropItemId"], "Backdrop",
                parent_tags[0], width, height=height, fill=fill, index=0)
        return None


class OfflineLibrarySource:
    """LibrarySource-compatible browser backed by the offline catalog.

    Mirrors the normal browsing UI (libraries → grids → series → seasons →
    episodes) filtered to downloaded content, with artwork from local files.
    Reads the catalog read-only and caches rows in memory.
    """

    def __init__(self, catalog_path):
        self.catalog_path = catalog_path
        self.root = os.path.dirname(catalog_path) if catalog_path else None
        self._rows = {}
        self._items = []
        self.reload()

    def reload(self):
        rows = []
        if self.catalog_path:
            db = SyncDB(self.catalog_path, read_only=True)
            rows = db.list(status=STATUS_COMPLETE)
            db.close()
        self._rows = {r["item_id"]: r for r in rows}
        self._items = []
        self._series_server = {}  # series_id -> server_id (for series artwork)
        self._season_server = {}  # season_id -> server_id (for season artwork)
        self._season_series = {}  # season_id -> series_id (artwork fallback)
        for row in rows:
            try:
                self._items.append(json.loads(row["item_json"]))
            except (TypeError, ValueError):
                pass
            if row.get("type") == "Episode" and row.get("series_id"):
                self._series_server.setdefault(row["series_id"], row.get("server_id"))
                if row.get("season_id"):
                    self._season_server.setdefault(row["season_id"],
                                                   row.get("server_id"))
                    self._season_series.setdefault(row["season_id"],
                                                   row["series_id"])

    def stop(self):
        pass

    def servers(self):
        return [{"uuid": "offline", "name": _("Downloaded")}]

    # -- browsing ----------------------------------------------------------

    def get_libraries(self, server_uuid):
        libs = []
        if any(i.get("Type") in ("Movie", "Video") for i in self._items):
            libs.append({"Id": "offline:movies", "Name": _("Movies"),
                         "Type": "CollectionFolder", "CollectionType": "movies",
                         "ImageTags": {}})
        if any(i.get("Type") == "Episode" for i in self._items):
            libs.append({"Id": "offline:tv", "Name": _("TV Shows"),
                         "Type": "CollectionFolder", "CollectionType": "tvshows",
                         "ImageTags": {}})
        return libs

    def _series_list(self):
        seen, order = {}, []
        for item in self._items:
            if item.get("Type") != "Episode":
                continue
            sid = item.get("SeriesId")
            if not sid or sid in seen:
                continue
            seen[sid] = {"Id": sid, "Name": item.get("SeriesName") or _("Series"),
                         "Type": "Series", "ImageTags": {}}
            order.append(sid)
        return [seen[s] for s in order]

    def get_home_rows(self, server_uuid):
        rows = []
        movies = [i for i in self._items if i.get("Type") in ("Movie", "Video")]
        if movies:
            rows.append({"title": _("Downloaded Movies"), "items": movies})
        series = self._series_list()
        if series:
            rows.append({"title": _("Downloaded Shows"), "items": series})
        return rows

    def get_library_items(self, server_uuid, parent_id, sort_by="SortName",
                          sort_order="Ascending", start_index=0, limit=100):
        if parent_id == "offline:movies":
            items = [i for i in self._items if i.get("Type") in ("Movie", "Video")]
        elif parent_id == "offline:tv":
            items = self._series_list()
        else:
            items = []
        items = sorted(items, key=lambda i: (i.get("Name") or "").lower())
        return items[start_index:start_index + limit], len(items)

    def get_seasons(self, server_uuid, series_id):
        seen, order = {}, []
        for item in self._items:
            if item.get("Type") != "Episode" or item.get("SeriesId") != series_id:
                continue
            key = item.get("SeasonId") or ("p%s" % item.get("ParentIndexNumber"))
            if key in seen:
                continue
            pidx = item.get("ParentIndexNumber")
            if item.get("SeasonName"):
                name = item["SeasonName"]
            elif pidx == 0:
                name = _("Specials")
            elif pidx is not None:
                name = _("Season %d") % pidx
            else:
                name = _("Episodes")
            seen[key] = {"Id": item.get("SeasonId") or key, "Name": name,
                         "Type": "Season", "ImageTags": {}, "IndexNumber": pidx}
            order.append(key)
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
        for i in self._items:
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

    def get_item(self, server_uuid, item_id):
        row = self._rows.get(item_id)
        if row and row.get("item_json"):
            return json.loads(row["item_json"])
        # Synthesize a Series DTO so the series overview page renders offline.
        if item_id in self._series_server:
            name = next((i.get("SeriesName") for i in self._items
                         if i.get("SeriesId") == item_id), _("Series"))
            return {"Id": item_id, "Name": name, "Type": "Series", "ImageTags": {}}
        return None

    def get_series_queue(self, server_uuid, series_id, start_item_id=None, limit=100):
        eps = [i for i in self._items
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
        return [i for i in self._items
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

    def _art_path(self, item_id, image_type):
        if not self.root or not item_id:
            return None
        name = self._name_for(image_type)
        # Downloaded item (movie/episode).
        row = self._rows.get(item_id)
        if row and row.get("file_path"):
            return self._in_dir(os.path.join(self.root,
                                             os.path.dirname(row["file_path"])), name)
        # Series artwork (cached separately from its episodes).
        if item_id in self._series_server:
            series_dir = os.path.join(self.root,
                                      self._series_server[item_id] or "server",
                                      "series", item_id)
            return self._in_dir(series_dir, name)
        # Season artwork, falling back to the series image when the season has
        # no specific artwork.
        if item_id in self._season_server:
            season_dir = os.path.join(self.root,
                                      self._season_server[item_id] or "server",
                                      "season", item_id)
            found = self._in_dir(season_dir, name)
            if found:
                return found
            series_id = self._season_series.get(item_id)
            if series_id:
                return self._art_path(series_id, image_type)
            return None
        # Synthetic library previews use a representative download.
        if item_id == "offline:movies":
            return self._representative(("Movie", "Video"))
        if item_id == "offline:tv":
            for series_id in self._series_server:
                path = self._art_path(series_id, "Primary")
                if path:
                    return path
            return self._representative(("Episode",))
        return None

    def _representative(self, types):
        for row in self._rows.values():
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

    def backdrop_url(self, server_uuid, item, width=1280, height=None, fill=False):
        return self._art_path(item.get("Id"), "Backdrop")
