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

# CollectionTypes we do not surface (video-only browser, phase 1). Playlists
# ARE surfaced (as a normal library tile): Jellyfin lets a playlist's declared
# type and its contents diverge, so we can't classify a playlist as music/video
# up front — instead we show every playlist and filter its *contents* to
# supported types when opened (see PLAYLIST_SUPPORTED_TYPES).
EXCLUDED_COLLECTION_TYPES = {"music", "musicvideos", "books", "livetv"}

# Item types that open the detail/play view rather than drilling deeper.
PLAYABLE_TYPES = {"Movie", "Episode", "Video", "MusicVideo"}
# Item types that drill into a series view.
SERIES_TYPES = {"Series"}
# Item types that drill into another grid (by ParentId).
FOLDER_TYPES = {"CollectionFolder", "Folder", "BoxSet", "Season", "UserView"}
# Item types shown inside a playlist. A playlist can mix in music/other entries;
# only these are surfaced (and downloaded) by this video-only browser.
PLAYLIST_SUPPORTED_TYPES = {"Movie", "Episode", "Video"}


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

    def get_home_rows(self, server_uuid, libraries=None):
        """Return the ordered rows shown on the home screen.

        Each row is ``{"title": str, "items": [DTO, ...]}``; empty rows are
        dropped so the home screen only shows what exists. ``libraries`` (the
        get_libraries result) drives the per-library "Latest" rows; passing it
        in avoids a second views fetch when the caller already has it.
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

        # Per-library "Latest in X" rows, like jellyfin-web's home screen
        # (replaces the old global Recently Added Movies/Episodes pair).
        if libraries is None:
            try:
                libraries = self.get_libraries(server_uuid)
            except Exception:
                log.warning("Failed to list libraries for latest rows",
                            exc_info=True)
                libraries = []
        for lib in libraries:
            if lib.get("CollectionType") == "playlists":
                continue
            try:
                latest = api.get_recently_added(parent_id=lib.get("Id"),
                                                limit=16) or []
                # get_recently_added returns a bare list, not an Items dict.
                items = latest.get("Items", []) if isinstance(latest, dict) else latest
                rows.append((_("Latest %s") % lib.get("Name", ""), items))
            except Exception:
                log.warning("Failed to load latest for %s", lib.get("Name"),
                            exc_info=True)

        return [{"title": t, "items": i} for t, i in rows if i]

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

    def get_genres(self, server_uuid, parent_id=None):
        """Genre names available under a library (for the filter picker)."""
        api = self._conn(server_uuid).api
        result = api.get_genres(parent_id) or {}
        return [g.get("Name") for g in result.get("Items", []) if g.get("Name")]

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

        if item.get("PrimaryImageTag"):
            # People entries carry a bare PrimaryImageTag instead of ImageTags.
            return item["Id"], "Primary", item["PrimaryImageTag"]

        if item.get("SeriesId") and item.get("SeriesPrimaryImageTag"):
            return item["SeriesId"], "Primary", item["SeriesPrimaryImageTag"]

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
                 playlist_items=None, playlist_first=None):
        self.rows = rows or {}
        self.items = items or []
        self.series_server = series_server or {}
        self.season_server = season_server or {}
        self.season_series = season_series or {}
        self.playlists = playlists or []
        self.playlist_items = playlist_items or {}
        self.playlist_first = playlist_first or {}
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
        playlist_dtos, playlist_items, playlist_first = [], {}, {}
        for pl in playlists:
            pid = pl["playlist_id"]
            pl_items = [self._item_from_row(r) for r in playlist_rows.get(pid, [])]
            pl_items = [i for i in pl_items if i is not None]
            if not pl_items:
                continue
            playlist_dtos.append({"Id": pid, "Name": pl.get("name") or _("Playlist"),
                                  "Type": "Playlist", "ImageTags": {}})
            playlist_items[pid] = pl_items
            playlist_first[pid] = pl_items[0].get("Id")
        self._snap = _OfflineSnapshot(
            rows=by_id, items=items, series_server=series_server,
            season_server=season_server, season_series=season_series,
            playlists=playlist_dtos, playlist_items=playlist_items,
            playlist_first=playlist_first)

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

    def get_home_rows(self, server_uuid, libraries=None):
        snap = self._snap
        rows = []
        movies = [i for i in snap.items if i.get("Type") == "Movie"]
        if movies:
            rows.append({"title": _("Downloaded Movies"), "items": movies})
        videos = [i for i in snap.items if i.get("Type") == "Video"]
        if videos:
            rows.append({"title": _("Downloaded Videos"), "items": videos})
        series = self._series_list(snap)
        if series:
            rows.append({"title": _("Downloaded Shows"), "items": series})
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
                seen[key] = {"Id": item.get("SeasonId") or key, "Name": name,
                             "Type": "Season", "ImageTags": {},
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
        # Playlist tile artwork borrows its first downloaded item's poster.
        first = snap.playlist_first.get(item_id)
        if first is not None:
            return self._art_path(first, image_type, snap)
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
            for pid in snap.playlist_first:
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
