"""MpvtkBrowser — the app shell: route stack, async data loading, and the
``build(size)`` that turns the current route into an mpvtk widget tree.

This is the mpvtk analogue of the Tk ``BrowserApp``. It runs in the main
process next to ``playerManager`` (no ``multiprocessing`` child), attaches
its UI to the player's mpv window via ``mpvtk.MpvtkApp.attach`` (see
``mpvtk/MIGRATION.md``), and reproduces the load-bearing paradigms of the
Tk browser: a route-dict nav stack (``navigate``/``go_back``), background
API calls with epoch-guarded staleness, and full-scene rebuilds driven by
``invalidate()`` (renderer-local state — scroll, focus — survives).

Views are ``build()`` branches on the route ``kind``; Phase 1 fills them
in. This module ships the shell plus enough of Home/Grid to prove the
shape end-to-end (strips + async + routing + chrome).
"""

import logging
import threading
from concurrent.futures import ThreadPoolExecutor

from ..i18n import _
from ..mpvtk.layout import text_width
from ..mpvtk.rawimage import cache_dir
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dialog,
    Dropdown,
    HScroll,
    Icon,
    Image,
    ImageMap,
    Menu,
    Row,
    Slider,
    Spacer,
    Text,
    TextBox,
    VScroll,
)
from . import theme
from .repository import FOLDER_TYPES, PLAYABLE_TYPES, SERIES_TYPES
from .strips import (
    LANDSCAPE_GEOM,
    POSTER_GEOM,
    SQUARE_GEOM,
    StripStore,
    Tile,
    TileGeom,
)
from .thumbnails import make_key

log = logging.getLogger("mpvtk_browser.app")

# Routes that take over the whole surface (no nav chrome), like the Tk
# browser's login/locked/connecting screens.
CHROME_FREE = {"login", "locked", "connecting"}

# Grid sort modes (label, SortBy, SortOrder) — ported from the Tk browser.
SORTS = [
    (_("Name"), "SortName", "Ascending"),
    (_("Date Added"), "DateCreated", "Descending"),
    (_("Release Date"), "PremiereDate", "Descending"),
    (_("Community Rating"), "CommunityRating", "Descending"),
    (_("Date Played"), "DatePlayed", "Descending"),
    (_("Play Count"), "PlayCount", "Descending"),
    (_("Runtime"), "Runtime", "Ascending"),
    (_("Random"), "Random", "Ascending"),
]
_LETTERS = "#ABCDEFGHIJKLMNOPQRSTUVWXYZ"


class MpvtkBrowser:
    def __init__(self, app, source, strips=None, thumbs=None,
                 server_uuid=None, geom=None, controller=None, config=None):
        self.app = app            # mpvtk.MpvtkApp (attached or spawned)
        self.source = source
        # Settings accessor (settings_schema/get_settings/set_setting). None ->
        # the real in-process config module; tests inject a fake.
        self._config_obj = config
        # Optional bridge to the player (playback + browse/play window state).
        # None in tests -> playable clicks just report status; the window/OSC
        # handoff is a no-op. See mpvtk_browser.ui._PlayerController.
        self.controller = controller
        # True while the browser owns the window; False while it has yielded to
        # playback + the OSC. build() pushes an empty scene when not browsing so
        # its overlays clear off the video.
        self._browsing = True
        # Latest now-playing snapshot (from on_playstate) for the audio bar.
        self._now_playing = None
        # Open tile context menu: {"item", "server", "x", "y"} or None.
        self._menu = None
        # Banners: update-available notice + offline indicator.
        self._update = None       # {"version", "url"} or None
        self._offline = False
        # Modal dialog: a builder callable -> Dialog node, or None.
        self._dialog = None
        # Download dialog state {"server","item","est","watched"} or None.
        self._dl = None
        # Login form field values (renderer holds the live text; we mirror it
        # here via on_change so Connect can read all three fields at once).
        self._login = {"server": "", "user": "", "pass": ""}
        self._login_error = None
        # Startup-PIN lock screen state.
        self._pin = {"pin": ""}
        self._pin_error = None
        self.geom = geom or POSTER_GEOM       # default tile shape (2:3)
        self.geom_wide = LANDSCAPE_GEOM       # 16:9 (episodes / home video)
        self.geom_square = SQUARE_GEOM        # 1:1 (music)
        # Downloaded id sets (for the tile badge), refreshed from the sync db.
        self._downloaded = set()
        self._downloaded_series = set()
        # Default to a file-backed store (works on both backends / headless);
        # the libmpv integration passes a MemoryStore-backed one.
        self.strips = strips or StripStore(
            cache_dir=cache_dir("mpvtk-browser-"), geom=self.geom)
        self.thumbs = thumbs      # ThumbnailStore (optional; None -> no art)
        if self.thumbs is not None:
            # Wake our loop when a decoded poster lands, so build() can pump it.
            self.thumbs._notify = self.invalidate

        servers = []
        try:
            servers = source.servers()
        except Exception:
            log.warning("could not enumerate servers", exc_info=True)
        self.server = server_uuid or (servers[0]["uuid"] if servers else None)

        self._epoch = 0
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(max_workers=4,
                                        thread_name_prefix="mpvtk-api")
        self._posters = {}        # thumb key -> PIL image
        self._requested = set()   # thumb keys already dispatched
        self.status = ""

        self.nav_stack = [{"kind": "home", "server": self.server}]
        self._load_route(self.route)

    # ------------------------------------------------------------ routing

    @property
    def route(self):
        return self.nav_stack[-1]

    def navigate(self, route, reset=False):
        if reset:
            self.nav_stack = []
        self.nav_stack.append(route)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def go_back(self):
        if len(self.nav_stack) > 1:
            self.nav_stack.pop()
            self._bump_epoch()
            self.invalidate()

    def after_playlist_deleted(self, playlist_id):
        """Prune stale routes pointing at a now-deleted playlist (mirrors the
        Tk browser so a back-stack can't resurrect a gone item)."""
        self.nav_stack = [
            r for r in self.nav_stack
            if r.get("parent_id") != playlist_id
        ] or [{"kind": "home", "server": self.server}]
        self._bump_epoch()
        self.invalidate()

    def _bump_epoch(self):
        with self._lock:
            self._epoch += 1

    # -------------------------------------------------------- async model

    def invalidate(self):
        if self.app is not None:
            self.app.invalidate()

    def run_async(self, work, on_done, epoch):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if the epoch still matches (the user hasn't navigated away). ``on_done``
        mutates state under the lock, then the loop is woken to rebuild."""
        def task():
            try:
                result = work()
            except Exception:
                log.warning("async work failed", exc_info=True)
                return
            with self._lock:
                if epoch != self._epoch:
                    return  # superseded by a newer navigation
                try:
                    on_done(result)
                except Exception:
                    log.warning("async on_done failed", exc_info=True)
            self.invalidate()

        self._pool.submit(task)

    def _load_route(self, route):
        kind = route["kind"]
        if self.server is None:
            return
        ep = self._epoch
        if kind == "home":
            def work():
                server = route.get("server") or self.server
                libs = self.source.get_libraries(server)
                rows = self.source.get_home_rows(server, libs)
                return {"libraries": libs, "rows": rows}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "grid":
            srv = route.get("server") or self.server
            parent = route["parent_id"]
            _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
            filters = route.get("_filters") or {}

            def work():
                items, total = self.source.get_library_items(
                    srv, parent, sort_by=sort_by, sort_order=sort_order,
                    filters=filters)
                vals = route.get("_filtervals")
                if vals is None:
                    try:
                        vals = self.source.get_filter_values(srv, parent)
                    except Exception:
                        vals = {"genres": [], "years": []}
                return items, total, vals

            def done(res):
                route["_items"], route["_total"], route["_filtervals"] = res
            self.run_async(work, done, ep)
        elif kind == "detail":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                item = self.source.get_item(srv, iid)
                similar = []
                try:
                    similar = self.source.get_similar(srv, iid)
                except Exception:
                    pass
                return {"item": item, "similar": similar}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "series":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return {
                    "item": self.source.get_item(srv, iid),
                    "seasons": self.source.get_seasons(srv, iid),
                }
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "season":
            srv = route.get("server") or self.server

            def work():
                return {
                    "episodes": self.source.get_episodes(
                        srv, route.get("series_id"), route["item_id"]),
                    "seasons": self.source.get_seasons(
                        srv, route.get("series_id")),
                }
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "search":
            srv = route.get("server") or self.server
            term = route.get("term", "")

            def work():
                if not term:
                    return {"items": [], "people": []}
                items = self.source.search(srv, term)
                people = []
                try:
                    people = self.source.search_people(srv, term)
                except Exception:
                    pass
                return {"items": items, "people": people}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "music":
            srv = route.get("server") or self.server
            parent = route["parent_id"]
            tab = route.get("_tab", "albums")

            def work():
                if tab == "albumartists":
                    return self.source.get_album_artists(srv, parent)[0]
                if tab == "artists":
                    return self.source.get_artists(srv, parent)[0]
                if tab == "songs":
                    return self.source.get_songs(srv, parent)[0]
                if tab == "genres":
                    return self.source.get_music_genres(srv, parent)
                return self.source.get_music_albums(srv, parent)[0]
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "album":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return {"item": self.source.get_item(srv, iid),
                        "tracks": self.source.get_album_tracks(srv, iid)}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "artist":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                songs = []
                try:
                    songs = self.source.get_artist_songs(srv, iid)
                except Exception:
                    pass
                return {"albums": self.source.get_artist_albums(srv, iid),
                        "songs": songs}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "music_genre":
            srv = route.get("server") or self.server

            def work():
                songs = []
                try:
                    songs = self.source.get_genre_songs(
                        srv, route.get("parent_id"), route["item_id"])
                except Exception:
                    pass
                return {"albums": self.source.get_genre_albums(
                    srv, route.get("parent_id"), route["item_id"])[0],
                    "songs": songs}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "playlist":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return self.source.get_playlist_items(srv, iid)
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "person":
            srv = route.get("server") or self.server

            def work():
                return self.source.get_person_items(srv, route["person_id"])

            def done(res):
                route["_items"], route["_total"] = res
            self.run_async(work, done, ep)
        elif kind == "playlist_edit":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return self.source.get_playlist_items(srv, iid)
            self.run_async(work, lambda d: route.__setitem__("_items", d), ep)
        elif kind == "queue":
            srv = route.get("server") or self.server

            def work():
                q = ({"items": [], "current_id": None} if self.controller is None
                     else self.controller.get_queue())
                ids = [e["id"] for e in q.get("items", []) if e.get("id")]
                by_id = {}
                if ids:
                    try:
                        for it in self.source.get_items_by_ids(srv, ids):
                            by_id[it.get("Id")] = it
                    except Exception:
                        pass
                entries = [
                    {"item": by_id.get(e["id"], {"Id": e["id"],
                                                 "Name": e["id"]}),
                     "pid": e.get("playlist_item_id")}
                    for e in q.get("items", [])]
                return {"entries": entries, "current_id": q.get("current_id")}
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)

    # -------------------------------------------------------- tile helpers

    def _subtitle(self, item):
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                return "S%dE%d" % (s, e)
        return str(item.get("ProductionYear") or "")

    def _request_image(self, key, url, box):
        """Return a cached decoded PIL image for ``key`` (poster/backdrop/…),
        or None while it loads — requesting it once from the thumbnail pool.
        The next repaint (woken by the pool's notify) picks it up."""
        img = self._posters.get(key)
        if (img is None and self.thumbs is not None
                and key not in self._requested and url):
            self._requested.add(key)
            self.thumbs.request(
                key, url, box, lambda im, k=key: self._posters.__setitem__(k, im))
        return img

    def _poster_for(self, item, geom, image_type="Primary"):
        """Return (PIL image or None, cache tag). Requests the poster once
        if absent; the strip recomposites when it arrives (tag changes)."""
        spec = self.source.image_spec(item, image_type, geom.tile_w)
        if not spec or self.server is None:
            return None, ""
        item_id, itype, itag = spec
        w, h = geom.tile_w, geom.tile_h
        key = make_key(item_id, itype, itag, w, h)
        url = self.source.image_url(self.server, item_id, itype, itag,
                                    w, h, fill=True)
        return self._request_image(key, url, (w, h)), key

    def _is_watched(self, item):
        ud = item.get("UserData") or {}
        if ud.get("Played"):
            return True
        if item.get("Type") in ("Series", "Season"):
            return (ud.get("UnplayedItemCount") or 0) == 0
        return False

    def _is_downloaded(self, item):
        if item.get("Id") in self._downloaded:
            return True
        return (item.get("Type") == "Series"
                and item.get("Id") in self._downloaded_series)

    @staticmethod
    def _glyph(item):
        if item.get("Type") in ("Audio", "MusicAlbum", "MusicArtist"):
            return "♪"  # ♪
        name = (item.get("Name") or "").strip()
        return name[0].upper() if name else "?"

    def _backdrop_node(self, item, box, node_id):
        """A backdrop Image node for detail/series headers, or a placeholder
        Box while it loads / when absent."""
        spec = None
        if self.server is not None:
            spec = self.source.backdrop_spec(item)
        if spec:
            owner_id, tag = spec
            key = make_key(owner_id, "Backdrop", tag, box[0])
            url = self.source.backdrop_url(self.server, item, width=box[0],
                                           height=box[1], fill=True)
            img = self._request_image(key, url, box)
            if img is not None:
                b = self.strips.bitmap(key, img)
                return Image(b["src"], b["iw"], b["ih"], id=node_id)
        return Box(w=box[0], h=box[1], bg=theme.PLACEHOLDER_BG, radius=6,
                   id=node_id)

    def _tile(self, item, geom, image_type="Primary"):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        rt = item.get("RunTimeTicks") or 0
        poster, tag = self._poster_for(item, geom, image_type)
        return Tile(
            key=item.get("Id", ""),
            title=item.get("Name", ""),
            subtitle=self._subtitle(item),
            poster=poster,
            poster_tag=tag,
            glyph=self._glyph(item),
            watched=self._is_watched(item),
            badge=int(ud.get("UnplayedItemCount") or 0),
            progress=(pos / rt) if (pos and rt) else 0.0,
            downloaded=self._is_downloaded(item),
        )

    def _image_map(self, items, prefix, geom=None, image_type="Primary"):
        geom = geom or self.geom
        tiles = [self._tile(it, geom, image_type) for it in items]
        s = self.strips.strip(tiles, geom)
        regions = []
        for r, it in zip(s["regions"], items):
            regions.append(dict(
                r,
                id="%s-%s" % (prefix, r["key"]),
                on_click=(lambda i=it: self._open_item(i)),
                on_context=(lambda x, y, i=it: self._open_tile_menu(i, x, y)),
            ))
        return ImageMap(s["src"], s["iw"], s["ih"], regions=regions)

    # ------------------------------------------------------ tile context menu

    def _open_tile_menu(self, item, x, y):
        self._menu = {"item": item,
                      "server": self.route.get("server") or self.server,
                      "x": x, "y": y}
        self.invalidate()

    def _close_menu(self):
        self._menu = None
        self.invalidate()

    def _tile_menu_node(self):
        m = self._menu
        item = m["item"]
        ud = item.get("UserData") or {}
        watched = bool(ud.get("Played")) or (
            item.get("Type") in ("Series", "Season")
            and (ud.get("UnplayedItemCount") or 0) == 0)
        fav = bool(ud.get("IsFavorite"))
        labels = [
            _("Play"),
            _("Mark Unwatched") if watched else _("Mark Watched"),
            _("Remove from Favorites") if fav else _("Add to Favorites"),
            _("Add to Playlist"),
            _("Download"),
        ]
        return Menu("tilemenu", labels, m["x"], m["y"],
                    icons=["play_arrow", "check", "favorite", "queue_music",
                           "file_download"],
                    on_select=self._menu_action, on_dismiss=self._close_menu)

    def _menu_action(self, index, value):
        m = self._menu
        if m is None:
            return
        item, server = m["item"], m["server"]
        ud = item.setdefault("UserData", {})
        if index == 0:                                   # Play
            self._menu_play(item, server)
        elif index == 1:                                 # watched toggle
            new = not (bool(ud.get("Played")) or (
                item.get("Type") in ("Series", "Season")
                and (ud.get("UnplayedItemCount") or 0) == 0))
            ud["Played"] = new
            if item.get("Type") in ("Series", "Season"):
                ud["UnplayedItemCount"] = 0 if new else 1
            self._client_call(lambda c: c.set_watched(
                server, item.get("Id"), new))
        elif index == 2:                                 # favorite toggle
            new = not bool(ud.get("IsFavorite"))
            ud["IsFavorite"] = new
            self._client_call(lambda c: c.set_favorite(
                server, item.get("Id"), new))
        elif index == 3:                                 # add to playlist
            self._close_menu()
            self._open_add_to(item)
            return
        elif index == 4:                                 # download
            self._close_menu()
            self._open_download(item)
            return
        self._close_menu()

    def _menu_play(self, item, server):
        t = item.get("Type")
        if t == "Audio":
            self._play_list([item.get("Id")], server, audio=True)
        elif t in PLAYABLE_TYPES:
            self._play(item, server)
        else:
            self._open_item(item)

    def _client_call(self, fn):
        """Run a client-mutating action (watched/favorite) off the loop
        thread so a slow server never stalls the UI."""
        if self.controller is None:
            return
        self._pool.submit(lambda: self._safe(fn))

    def _safe(self, fn):
        try:
            fn(self.controller)
        except Exception:
            log.warning("client action failed", exc_info=True)

    def _tile_row(self, title, items, row_id, geom=None, image_type="Primary"):
        geom = geom or self.geom
        return Column(
            [
                Text(title, size=24, bold=True),
                self._hscroll_row(
                    self._image_map(items, row_id, geom, image_type),
                    row_id, geom.strip_h + 6),
            ],
            gap=8,
        )

    def _hscroll_row(self, content, row_id, h):
        """An HScroll flanked by ◀ ▶ page buttons (the renderer pages the
        container by id — see MpvtkApp.scroll)."""
        def arrow(icon, node_id, direction):
            return Box([Icon(icon, 26)], id=node_id, w=36, h=h,
                       align="center", direction="row", bg=theme.BUTTON_BG,
                       hover={"fill": theme.BUTTON_ACTIVE}, radius=6,
                       on_click=lambda: self._page_row(row_id, direction))
        return Row([
            arrow("chevron_left", row_id + "-pl", -1),
            HScroll(content, id=row_id, h=h, flex=1),
            arrow("chevron_right", row_id + "-pr", 1),
        ], gap=6, align="center", h=h)

    def _page_row(self, row_id, direction):
        # Ask the renderer to page the horizontal scroll container.
        if self.app is not None and hasattr(self.app, "scroll"):
            self.app.scroll(row_id, direction)

    # ------------------------------------------------------------- actions

    def _open_item(self, item):
        t = item.get("Type")
        server = self.route.get("server") or self.server
        base = {"server": server, "item_id": item.get("Id"),
                "title": item.get("Name", "")}
        if t == "MusicAlbum":
            self.navigate(dict(base, kind="album"))
        elif t == "MusicArtist":
            self.navigate(dict(base, kind="artist"))
        elif t == "MusicGenre":
            self.navigate(dict(base, kind="music_genre",
                               parent_id=self.route.get("parent_id")))
        elif t == "Playlist":
            self.navigate(dict(base, kind="playlist"))
        elif t == "Audio":
            self._play_list([item.get("Id")], server, audio=True)
        elif item.get("CollectionType") == "music":
            self.navigate(dict(base, kind="music", parent_id=item.get("Id")))
        elif t in SERIES_TYPES:
            self.navigate(dict(base, kind="series"))
        elif t == "Season":
            self.navigate(dict(base, kind="season",
                               series_id=item.get("SeriesId")))
        elif t in PLAYABLE_TYPES:
            self.navigate(dict(base, kind="detail"))
        elif t in ("Person", "Actor", "Director", "Writer"):
            self.navigate(dict(base, kind="person", person_id=item.get("Id")))
        elif t in FOLDER_TYPES or item.get("CollectionType"):
            self.navigate(dict(base, kind="grid", parent_id=item.get("Id")))
        else:
            self.status = _("Selected: %s") % item.get("Name", "")
            self.invalidate()

    def _yield(self):
        self._browsing = False
        if self.controller is not None:
            self.controller.on_browse_leave()
        self.invalidate()  # empty scene clears overlays off the video

    def _start(self, audio):
        """Prepare to start playback. Video yields the whole window to the
        video + OSC; audio has no picture, so we stay in browse and show the
        now-playing bar instead (playing would-be background over audio would
        stop it)."""
        if audio:
            self._now_playing = self._now_playing or {"title": _("Loading…")}
            self.invalidate()
        else:
            self._yield()

    def _play(self, item, server, offset_ticks=None, srcid=None, aid=None,
              sid=None):
        """Yield/keep-browse and start a single ``item``. Episodes queue the
        rest of the season so autoplay-next chains them (like the Tk browser)."""
        self._start(audio=item.get("Type") == "Audio")
        if self.controller is None:
            return
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            srv, iid, series = server, item.get("Id"), item.get("SeriesId")

            def work():
                try:
                    q = self.source.get_series_queue(
                        srv, series, start_item_id=iid)
                    return [e.get("Id") for e in q if e.get("Id")] or [iid]
                except Exception:
                    return [iid]

            def done(ids):
                self.controller.play_list(ids, srv, 0,
                                          offset_ticks=offset_ticks,
                                          srcid=srcid, aid=aid, sid=sid)
            self.run_async(work, done, self._epoch)
        else:
            self.controller.play(item, server, offset_ticks=offset_ticks,
                                 srcid=srcid, aid=aid, sid=sid)

    def _play_list(self, ids, server, start_index=0, audio=False):
        """Play a whole list from ``start_index`` (album/playlist/song)."""
        ids = [i for i in ids if i]
        if not ids:
            return
        self._start(audio=audio)
        if self.controller is not None:
            self.controller.play_list(ids, server, start_index)

    # ------------------------------------------------- browse <-> playback

    def enter_browse(self):
        """Show the browser: take the window + hide the OSC, then render."""
        self._browsing = True
        if self.controller is not None:
            self.controller.on_browse_enter()
        self.invalidate()

    def on_playstate(self, state):
        """Registered as playerManager.on_playstate. Drives browse/playback
        state and the now-playing bar. Audio keeps the browser visible (bar +
        browsing); video stays yielded to the picture + OSC."""
        if not state or state.get("stopped"):
            self._now_playing = None
            if not self._browsing:
                self.enter_browse()
            else:
                self.invalidate()
            return
        if state.get("is_audio"):
            self._now_playing = state
            self._browsing = True   # audio: stay in browse, show the bar
            self.invalidate()
        else:
            self._now_playing = None
            self._browsing = False  # video: yield the window
            self.invalidate()

    def set_source(self, source, server_uuid=None):
        """Swap in a live data source once servers connect (the browser opens
        immediately on a spinner and populates when the network settles)."""
        self.source = source
        try:
            servers = source.servers()
        except Exception:
            servers = []
        self.server = server_uuid or (servers[0]["uuid"] if servers else None)
        self.nav_stack = [{"kind": "home", "server": self.server}]
        self._bump_epoch()
        self._load_route(self.route)
        self._refresh_downloaded()
        self.invalidate()

    def _refresh_downloaded(self):
        """Refresh the downloaded-id sets for tile badges (from the sync db)."""
        if self.controller is None:
            return

        def work():
            try:
                items, series = self.controller.downloaded_ids()
            except Exception:
                return
            self._downloaded, self._downloaded_series = items, series
            self.invalidate()
        self._pool.submit(work)

    # --------------------------------------------------------------- build

    def build(self, size):
        w, h = size
        if not self._browsing:
            # Yielded to playback: an empty scene clears our overlays so the
            # video + OSC show through.
            return Column([], w=w, h=h)
        # Deliver any decoded posters before composing strips this frame.
        if self.thumbs is not None:
            self.thumbs.pump()
        route = self.route
        content = self._render_route(route, size)
        children = []
        if route["kind"] not in CHROME_FREE:
            children.append(self._chrome(w))
            banner = self._banner()
            if banner is not None:
                children.append(banner)
        children.append(content)
        if self._now_playing is not None and route["kind"] not in CHROME_FREE:
            children.append(self._now_playing_bar(w))
        if self._menu is not None:
            children.append(self._tile_menu_node())
        if self._dialog is not None:
            children.append(self._dialog())
        return Column(children, w=w, h=h, align="stretch")

    def _chrome(self, w):
        left = []
        if len(self.nav_stack) > 1:
            left.append(Button(_("Back"), id="nav-back", on_click=self.go_back))
        left.append(Button(
            _("Home"), id="nav-home",
            on_click=lambda: self.navigate(
                {"kind": "home", "server": self.server}, reset=True),
        ))
        title = self.route.get("title") or _("Home")
        right = []
        try:
            servers = self.source.servers()
        except Exception:
            servers = []
        if len(servers) > 1:
            names = [s["name"] for s in servers]
            cur = next((i for i, s in enumerate(servers)
                        if s["uuid"] == self.server), 0)
            right.append(Dropdown(
                "nav-server", names, selected=cur, w=160,
                on_select=lambda i, v: self._switch_server(servers[i]["uuid"])))
        right += [
            TextBox("nav-search", placeholder=_("Search…"), w=220,
                    on_submit=self._search),
            Button(_("SyncPlay"), id="nav-syncplay",
                   on_click=self._open_syncplay),
            Button(_("Settings"), id="nav-settings",
                   on_click=self._open_settings),
        ]
        return Row(
            left + [Text(title, size=22, bold=True), Spacer()] + right,
            pad=12, gap=10, align="center", h=60, bg=theme.PANEL_BG,
        )

    def _switch_server(self, uuid):
        if uuid == self.server:
            return
        self.server = uuid
        self.navigate({"kind": "home", "server": uuid}, reset=True)

    def _open_queue(self):
        self.navigate({"kind": "queue", "server": self.server,
                       "title": _("Queue")})

    def _render_route(self, route, size):
        kind = route["kind"]
        render = {
            "home": self._render_home,
            "grid": self._render_grid,
            "detail": self._render_detail,
            "series": self._render_series,
            "season": self._render_season,
            "search": self._render_search,
            "music": self._render_music,
            "album": self._render_album,
            "artist": self._render_artist,
            "music_genre": self._render_music_genre,
            "playlist": self._render_playlist,
            "settings": self._render_settings,
            "queue": self._render_queue,
            "playlist_edit": self._render_playlist_edit,
            "login": self._render_login,
            "locked": self._render_locked,
            "person": self._render_grid,
        }.get(kind)
        if render is None:
            return self._busy()
        return render(route, size)

    def _render_home(self, route, size):
        if self.server is None:
            return Box(
                [Spacer(),
                 Row([Spacer(), Busy(), Spacer()]),
                 Row([Spacer(),
                      Text(_("Connecting to your server…"), size=20,
                           color=theme.SUBTLE_FG),
                      Spacer()]),
                 Spacer()],
                flex=1, direction="column", align="stretch", gap=16)
        data = route.get("_data")
        if data is None:
            return self._busy()
        rows = []
        if data["libraries"]:
            # Libraries read as landscape cards, like the web client.
            rows.append(self._tile_row(
                _("Libraries"), data["libraries"], "row-libs",
                geom=self.geom_wide))
        for i, hr in enumerate(data["rows"]):
            if hr.get("items"):
                geom, itype = self._row_shape(hr)
                rows.append(self._tile_row(
                    hr["title"], hr["items"], "row-%d" % i,
                    geom=geom, image_type=itype))
        if not rows:
            rows.append(Text(_("Nothing to show yet."), size=20,
                             color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=16, gap=20), id="home", flex=1)

    def _row_shape(self, hr):
        """(geom, image_type) for a home row, classified like the Tk browser:
        movies/tv/boxsets -> poster; music -> square; home-video/misc or
        episode-bearing rows -> landscape Thumb."""
        ctype = hr.get("collection_type")
        has_episode = any(it.get("Type") == "Episode"
                          for it in hr.get("items", []))
        if ctype in ("movies", "tvshows", "boxsets"):
            return self.geom, "Primary"
        if ctype == "music":
            return self.geom_square, "Primary"
        if ctype:
            return self.geom_wide, ("Thumb" if has_episode else "Primary")
        if has_episode:
            return self.geom_wide, "Thumb"
        return self.geom, "Primary"

    def _render_grid(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        cols = self._cols(size[0], self.geom)
        header = [Text(route.get("title", ""), size=26, bold=True)]
        if route["kind"] == "grid":
            header.append(self._grid_filter_bar(route))
            total = route.get("_total") or 0
            header.append(Text(_("%(shown)d of %(total)d") % {
                "shown": len(items), "total": total},
                size=14, color=theme.SUBTLE_FG))
        rows = header
        for start in range(0, len(items), cols):
            chunk = items[start:start + cols]
            rows.append(self._image_map(chunk, "grid-%d" % start))
        return VScroll(
            Column(rows, pad=16, gap=12), id="grid", flex=1,
            on_scroll=lambda off, mx: self._on_grid_scroll(route, off, mx),
        )

    def _grid_filter_bar(self, route):
        vals = route.get("_filtervals") or {}
        filters = route.get("_filters") or {}
        genres = vals.get("genres") or []
        gi = 0
        if filters.get("genre") in genres:
            gi = genres.index(filters["genre"]) + 1
        bar = Row([
            Dropdown("grid-sort", [s[0] for s in SORTS],
                     selected=route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set_grid("_sort", route, i)),
            Dropdown("grid-genre", [_("All Genres")] + genres, selected=gi,
                     w=180,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "genre", None if i == 0 else genres[i - 1])),
            Checkbox(_("Unplayed"), bool(filters.get("unplayed")),
                     id="grid-unplayed",
                     on_toggle=lambda: self._toggle_grid_filter(
                         route, "unplayed")),
            Checkbox(_("Favorites"), bool(filters.get("favorite")),
                     id="grid-fav",
                     on_toggle=lambda: self._toggle_grid_filter(
                         route, "favorite")),
            Spacer(),
            Button(_("Shuffle"), id="grid-shuffle",
                   on_click=lambda: self._grid_shuffle(route)),
        ], gap=10, align="center")
        cur_letter = filters.get("letter")
        letters = Row([
            Box([Text(ch, size=15,
                      color="101010" if cur_letter == ch else theme.SUBTLE_FG)],
                id="grid-l-" + ch, w=26, h=26, align="center", direction="row",
                radius=4, bg=theme.ACCENT if cur_letter == ch else None,
                hover=None if cur_letter == ch else {"fill": theme.BUTTON_BG},
                on_click=lambda c=ch: self._set_grid_filter(
                    route, "letter", None if cur_letter == c else c))
            for ch in _LETTERS], gap=2, align="center")
        return Column([bar, letters], gap=8)

    def _reload_grid(self, route):
        for k in ("_items", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _set_grid(self, key, route, value):
        route[key] = value
        self._reload_grid(route)

    def _set_grid_filter(self, route, key, value):
        route.setdefault("_filters", {})[key] = value
        self._reload_grid(route)

    def _toggle_grid_filter(self, route, key):
        f = route.setdefault("_filters", {})
        f[key] = not f.get(key)
        self._reload_grid(route)

    def _grid_shuffle(self, route):
        srv = route.get("server") or self.server
        ep = self._epoch

        def work():
            return self.source.get_shuffle_ids(srv, route["parent_id"])

        def done(ids):
            if ids:
                self._play_list(ids, srv, 0)
        self.run_async(work, done, ep)

    def _on_grid_scroll(self, route, offset, maximum):
        if route is not self.route:
            return
        items = route.get("_items") or []
        total = route.get("_total") or 0
        if len(items) >= total or route.get("_loading"):
            return
        if maximum - offset >= 800:
            return  # only page in near the bottom
        route["_loading"] = True
        ep = self._epoch
        start = len(items)
        _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
        filters = route.get("_filters") or {}
        person = route.get("person_id")

        def work():
            srv = route.get("server") or self.server
            if person:
                return self.source.get_person_items(srv, person,
                                                    start_index=start)
            return self.source.get_library_items(
                srv, route["parent_id"], start_index=start, sort_by=sort_by,
                sort_order=sort_order, filters=filters)

        def done(res):
            new, total2 = res
            route["_items"] = (route.get("_items") or []) + new
            route["_total"] = total2
            route["_loading"] = False

        self.run_async(work, done, ep)

    # --------------------------------------------------- detail / series / etc

    def _meta_line(self, item):
        parts = []
        if item.get("ProductionYear"):
            parts.append(str(item["ProductionYear"]))
        rt = item.get("RunTimeTicks")
        if rt:
            parts.append(_("%d min") % (rt // 600000000))
        if item.get("OfficialRating"):
            parts.append(str(item["OfficialRating"]))
        if item.get("CommunityRating"):
            parts.append("★ %.1f" % item["CommunityRating"])
        return "   ·   ".join(parts)

    def _wrap(self, text, size, max_w):
        lines, cur = [], ""
        for word in text.split():
            trial = (cur + " " + word).strip()
            if not cur or text_width(trial, size) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
        if cur:
            lines.append(cur)
        return lines

    def _paragraph(self, text, size, max_w, color=None):
        return Column(
            [Text(ln, size=size, color=color or theme.TEXT_FG)
             for ln in self._wrap(text, size, max_w)],
            gap=3,
        )

    def _sel_source(self, sources, route):
        if not sources:
            return None
        return next((s for s in sources
                     if s.get("Id") == route.get("_srcid")), sources[0])

    def _pick_source(self, route, src):
        route["_srcid"] = src.get("Id")
        route["_aid"] = None   # let the new version pick its own defaults
        route["_sid"] = None
        self.invalidate()

    def _track_pickers(self, route, item):
        sources = item.get("MediaSources") or []
        controls = []
        if len(sources) > 1:
            names = [s.get("Name") or _("Version %d") % (i + 1)
                     for i, s in enumerate(sources)]
            cur = next((i for i, s in enumerate(sources)
                        if s.get("Id") == route.get("_srcid")), 0)
            controls.append(self._picker_row(
                _("Version"), "dt-version", names, cur,
                lambda i, v: self._pick_source(route, sources[i])))
        src = self._sel_source(sources, route)
        streams = (src or {}).get("MediaStreams") or []
        audio = [s for s in streams if s.get("Type") == "Audio"]
        subs = [s for s in streams if s.get("Type") == "Subtitle"]

        def label(s, kind):
            return (s.get("DisplayTitle") or s.get("Language")
                    or "%s %s" % (kind, s.get("Index")))
        if audio:
            names = [label(s, _("Audio")) for s in audio]
            cur = next((i for i, s in enumerate(audio)
                        if s.get("Index") == route.get("_aid")), 0)
            controls.append(self._picker_row(
                _("Audio"), "dt-audio", names, cur,
                lambda i, v: route.__setitem__("_aid", audio[i].get("Index"))))
        if subs:
            names = [_("None")] + [label(s, _("Sub")) for s in subs]
            cur = 0
            if route.get("_sid") not in (None, -1):
                cur = next((i + 1 for i, s in enumerate(subs)
                            if s.get("Index") == route.get("_sid")), 0)
            controls.append(self._picker_row(
                _("Subtitle"), "dt-sub", names, cur,
                lambda i, v: route.__setitem__(
                    "_sid", -1 if i == 0 else subs[i - 1].get("Index"))))
        return controls

    def _picker_row(self, label, node_id, names, selected, on_select):
        return Row([Text(label, w=90, size=16, color=theme.SUBTLE_FG),
                    Dropdown(node_id, names, selected=selected, w=300,
                             on_select=on_select)], gap=8, align="center")

    def _play_buttons(self, route, item, server):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        srcid = (route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = route.get("_aid"), route.get("_sid")
        buttons = []
        if pos > 0:
            secs = pos // 10000000
            buttons.append(Row(
                [Icon("play_arrow", 20, color="101010"),
                 Text(_("Resume") + "  %d:%02d" % (secs // 60, secs % 60),
                      size=18, color="101010")],
                id="btn-resume", gap=6, pad=10, bg=theme.ACCENT, radius=6,
                align="center",
                on_click=lambda: self._play(item, server, offset_ticks=pos,
                                            srcid=srcid, aid=aid, sid=sid)))
        buttons.append(Row(
            [Icon("play_arrow", 20), Text(_("Play"), size=18)],
            id="btn-play", gap=6, pad=10, bg=theme.BUTTON_BG,
            hover={"fill": theme.BUTTON_ACTIVE}, radius=6, align="center",
            on_click=lambda: self._play(item, server, srcid=srcid,
                                        aid=aid, sid=sid)))
        return Row(buttons, gap=10)

    def _action_btn(self, icon, text, node_id, cb, on=False):
        fg = "101010" if on else "eeeeee"
        return Row([Icon(icon, 18, color=fg), Text(text, size=16, color=fg)],
                   id=node_id, gap=6, pad=9,
                   bg=theme.ACCENT if on else theme.BUTTON_BG,
                   hover=None if on else {"fill": theme.BUTTON_ACTIVE},
                   radius=6, align="center", on_click=cb)

    def _common_actions(self, item, server, prefix):
        """Watched / Favorite / Download buttons shared by detail/series/
        season."""
        ud = item.get("UserData") or {}
        return [
            self._action_btn(
                "check", _("Watched"), prefix + "-watched",
                lambda: self._act_watched(item, server),
                on=self._is_watched(item)),
            self._action_btn(
                "favorite", _("Favorite"), prefix + "-fav",
                lambda: self._act_favorite(item, server),
                on=bool(ud.get("IsFavorite"))),
            self._action_btn(
                "file_download", _("Download"), prefix + "-download",
                lambda: self._open_download(item)),
        ]

    def _detail_actions(self, item, server):
        btns = self._common_actions(item, server, "act")
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            btns.append(Button(
                _("Go to Series"), id="act-series",
                on_click=lambda: self.navigate({
                    "kind": "series", "server": server,
                    "item_id": item["SeriesId"],
                    "title": item.get("SeriesName", "")})))
        return Row(btns, gap=8, align="center")

    def _play_next_up(self, series_id, server):
        ep = self._epoch

        def work():
            return self.source.get_next_up(server, series_id)

        def done(item):
            if item:
                self._play(item, server)
        self.run_async(work, done, ep)

    def _series_actions(self, item, server, series_id):
        btns = [self._action_btn(
            "play_arrow", _("Next Up"), "sa-nextup",
            lambda: self._play_next_up(series_id, server))]
        btns += self._common_actions(item, server, "sa")
        return Row(btns, gap=8, align="center")

    def _act_watched(self, item, server):
        ud = item.setdefault("UserData", {})
        new = not self._is_watched(item)
        ud["Played"] = new
        if item.get("Type") in ("Series", "Season"):
            ud["UnplayedItemCount"] = 0 if new else 1
        self._client_call(lambda c: c.set_watched(server, item.get("Id"), new))
        self.invalidate()

    def _act_favorite(self, item, server):
        ud = item.setdefault("UserData", {})
        new = not bool(ud.get("IsFavorite"))
        ud["IsFavorite"] = new
        self._client_call(lambda c: c.set_favorite(server, item.get("Id"), new))
        self.invalidate()

    def _media_info_line(self, item, route):
        src = self._sel_source(item.get("MediaSources") or [], route)
        streams = (src or {}).get("MediaStreams") or []
        video = next((s for s in streams if s.get("Type") == "Video"), None)
        parts = []
        if video:
            if video.get("DisplayTitle"):
                parts.append(video["DisplayTitle"])
            elif video.get("Height"):
                parts.append("%dp" % video["Height"])
            if video.get("VideoRange") and video["VideoRange"] != "SDR":
                parts.append(video["VideoRange"])
        if src and src.get("Container"):
            parts.append(src["Container"].upper())
        return "   ·   ".join(parts)

    def _people_row(self, people, server):
        cast = [p for p in people
                if p.get("Type") in ("Actor", "Director", "Writer", None)][:20]
        if not cast:
            return None
        for p in cast:
            p.setdefault("Type", "Person")
        return self._tile_row(_("Cast & Crew"), cast, "detail-people",
                              geom=self.geom_square)

    def _error(self, msg):
        return Box([Text(msg, size=20, color=theme.SUBTLE_FG)],
                   pad=24, flex=1, align="center", direction="row")

    def _render_detail(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item")
        if not item:
            return self._error(_("Item not available."))
        w = size[0]
        server = route.get("server") or self.server
        bw = min(w - 32, 960)
        bh = int(bw * 9 / 16)
        title = item.get("Name", "")
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            se = "S%sE%s" % (s, e) if s is not None and e is not None else ""
            title = "   ·   ".join(
                p for p in (item.get("SeriesName"), se, title) if p)
        blocks = [
            self._backdrop_node(item, (bw, bh), "detail-bd"),
            Text(title, size=30, bold=True),
        ]
        meta = self._meta_line(item)
        if meta:
            blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        info = self._media_info_line(item, route)
        if info:
            blocks.append(Text(info, size=15, color=theme.SUBTLE_FG))
        blocks.append(self._play_buttons(route, item, server))
        blocks.append(self._detail_actions(item, server))
        blocks.extend(self._track_pickers(route, item))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, w - 32))
        people_row = self._people_row(item.get("People") or [], server)
        if people_row is not None:
            blocks.append(people_row)
        if data.get("similar"):
            blocks.append(self._tile_row(
                _("More Like This"), data["similar"], "detail-similar"))
        return VScroll(Column(blocks, pad=16, gap=16), id="detail", flex=1)

    def _render_series(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item") or {}
        w = size[0]
        bw = min(w - 32, 960)
        bh = int(bw * 9 / 16)
        server = route.get("server") or self.server
        blocks = [
            self._backdrop_node(item, (bw, bh), "series-bd"),
            Text(item.get("Name", ""), size=30, bold=True),
        ]
        meta = self._meta_line(item)
        if meta:
            blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        blocks.append(self._series_actions(item, server, route["item_id"]))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, w - 32))
        seasons = data.get("seasons") or []
        if seasons:
            blocks.append(self._tile_row(
                _("Seasons"), seasons, "series-seasons"))
        people_row = self._people_row(item.get("People") or [], server)
        if people_row is not None:
            blocks.append(people_row)
        return VScroll(Column(blocks, pad=16, gap=16), id="series", flex=1)

    def _render_season(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        episodes = data.get("episodes") or []
        seasons = data.get("seasons") or []
        server = route.get("server") or self.server
        geom = self.geom_wide   # episodes are landscape Thumb cards
        season_item = next((s for s in seasons
                            if s.get("Id") == route["item_id"]), {})
        title_row = [Text(route.get("title", ""), size=26, bold=True)]
        if len(seasons) > 1:
            names = [s.get("Name", "") for s in seasons]
            cur = next((i for i, s in enumerate(seasons)
                        if s.get("Id") == route["item_id"]), 0)
            title_row.append(Dropdown(
                "season-switch", names, selected=cur, w=220,
                on_select=lambda i, v: self._switch_season(route, seasons[i])))
        if route.get("series_id"):
            title_row.append(Button(
                _("To Series"), id="season-to-series",
                on_click=lambda: self.navigate({
                    "kind": "series", "server": server,
                    "item_id": route["series_id"],
                    "title": season_item.get("SeriesName", "")})))
        header = [Row(title_row, gap=12, align="center"),
                  Row(self._common_actions(season_item or {"Id": route["item_id"],
                                           "Type": "Season"}, server, "se"),
                      gap=8, align="center")]
        cols = self._cols(size[0], geom)
        rows = header
        for start in range(0, len(episodes), cols):
            rows.append(self._image_map(
                episodes[start:start + cols], "ep-%d" % start,
                geom, "Thumb"))
        return VScroll(Column(rows, pad=16, gap=12), id="season", flex=1)

    def _switch_season(self, route, season):
        self.navigate({
            "kind": "season",
            "server": route.get("server") or self.server,
            "item_id": season.get("Id"),
            "series_id": route.get("series_id"),
            "title": season.get("Name", ""),
        })

    def _search(self, term):
        term = (term or "").strip()
        if not term:
            return
        self.navigate({"kind": "search", "server": self.server,
                       "term": term, "title": _("Search")})

    def _render_search(self, route, size):
        term = route.get("term", "")
        if not term:
            return self._error(_("Type in the search box above."))
        data = route.get("_data")
        if data is None:
            return self._busy()
        items = data.get("items") or []
        people = data.get("people") or []
        w = size[0]
        g = self.geom
        rows = [Text(_('Results for "%s"') % term, size=24, bold=True)]
        if people:
            rows.append(self._tile_row(_("People"), people, "search-people"))
        cols = max(1, int((w - 32 + g.gap) // (g.tile_w + g.gap)))
        for start in range(0, len(items), cols):
            rows.append(self._image_map(
                items[start:start + cols], "search-%d" % start))
        if not items and not people:
            rows.append(Text(_("No results."), size=18, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=16, gap=12), id="search", flex=1)

    # ---------------------------------------------------- music / playlists

    def _cols(self, w, geom):
        return max(1, int((w - 32 + geom.gap) // (geom.tile_w + geom.gap)))

    def _grid_of(self, items, prefix, size, heading=None, geom=None,
                 image_type="Primary"):
        geom = geom or self.geom
        cols = self._cols(size[0], geom)
        rows = [Text(heading, size=26, bold=True)] if heading else []
        for start in range(0, len(items), cols):
            rows.append(self._image_map(items[start:start + cols],
                                        "%s-%d" % (prefix, start),
                                        geom, image_type))
        if not items:
            rows.append(Text(_("Nothing here yet."), size=18,
                             color=theme.SUBTLE_FG))
        return rows

    def _track_list(self, tracks, prefix, on_click):
        rows = []
        for i, tr in enumerate(tracks):
            num = tr.get("IndexNumber") or (i + 1)
            secs = (tr.get("RunTimeTicks") or 0) // 10000000
            rows.append(Row(
                [Text(str(num), w=44, size=17, color=theme.SUBTLE_FG),
                 Text(tr.get("Name", ""), flex=1, size=17),
                 Text("%d:%02d" % (secs // 60, secs % 60), w=64, size=16,
                      color=theme.SUBTLE_FG)],
                id="%s-%d" % (prefix, i), pad=8, radius=6,
                hover={"fill": theme.BUTTON_BG},
                on_click=lambda i=i: on_click(i)))
        return Column(rows, gap=2)

    def _play_shuffle(self, ids, server):
        import random
        ids = [i for i in ids if i]
        random.shuffle(ids)
        self._play_list(ids, server, 0, audio=True)

    def _queue_items(self, ids, server):
        self._client_call(lambda c: c.queue_items(server, [i for i in ids if i]))

    def _instant_mix(self, seed_id, server):
        ep = self._epoch

        def work():
            return self.source.get_instant_mix(server, seed_id)

        def done(items):
            self._play_list([i.get("Id") for i in items], server, 0,
                            audio=True)
        self.run_async(work, done, ep)

    def _music_action_bar(self, server, ids, seed_id, prefix="ma"):
        return Row([
            self._action_btn("play_arrow", _("Play"), prefix + "-play",
                             lambda: self._play_list(ids, server, 0,
                                                     audio=True), on=True),
            self._action_btn("shuffle", _("Shuffle"), prefix + "-shuffle",
                             lambda: self._play_shuffle(ids, server)),
            self._action_btn("playlist_add", _("Add to Queue"),
                             prefix + "-queue",
                             lambda: self._queue_items(ids, server)),
            self._action_btn("queue_music", _("Instant Mix"), prefix + "-mix",
                             lambda: self._instant_mix(seed_id, server)),
        ], gap=8, align="center")

    def _music_tab(self, route, label, tab):
        active = route.get("_tab", "albums") == tab
        return Button(label, id="mtab-" + tab,
                      bg=theme.ACCENT if active else theme.BUTTON_BG,
                      fg="101010" if active else theme.TEXT_FG,
                      on_click=lambda: self._set_music_tab(route, tab))

    def _set_music_tab(self, route, tab):
        route["_tab"] = tab
        route.pop("_data", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _render_music(self, route, size):
        tabs = Row([
            self._music_tab(route, _("Albums"), "albums"),
            self._music_tab(route, _("Album Artists"), "albumartists"),
            self._music_tab(route, _("Artists"), "artists"),
            self._music_tab(route, _("Songs"), "songs"),
            self._music_tab(route, _("Genres"), "genres"),
        ], gap=8)
        data = route.get("_data")
        if data is None:
            body = self._busy()
        elif route.get("_tab") == "songs":
            server = route.get("server") or self.server
            ids = [s.get("Id") for s in data]
            body = VScroll(Column([self._track_list(
                data, "song",
                lambda i: self._play_list(ids, server, i, audio=True))],
                pad=16), id="music-songs", flex=1)
        else:
            geom = self.geom_square if route.get("_tab") in (
                None, "albums", "albumartists", "artists") else self.geom
            body = VScroll(
                Column(self._grid_of(data, "music", size, geom=geom),
                       pad=16, gap=12), id="music-grid", flex=1)
        return Column([Row([tabs], pad=12), body], flex=1, align="stretch")

    def _render_album(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item") or {}
        tracks = data.get("tracks") or []
        server = route.get("server") or self.server
        ids = [t.get("Id") for t in tracks]
        header = Column([
            Text(item.get("Name") or route.get("title", ""), size=28,
                 bold=True),
            self._music_action_bar(server, ids, route["item_id"], "album"),
        ], gap=10)
        body = self._track_list(
            tracks, "trk",
            lambda i: self._play_list(ids, server, i, audio=True))
        return VScroll(Column([header, body], pad=16, gap=12),
                       id="album", flex=1)

    def _render_artist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.server
        ids = [s.get("Id") for s in songs]
        rows = [Text(route.get("title", ""), size=26, bold=True),
                self._music_action_bar(server, ids, route["item_id"], "art")]
        rows += self._grid_of(albums, "artist", size, geom=self.geom_square)
        return VScroll(Column(rows, pad=16, gap=12), id="artist", flex=1)

    def _render_music_genre(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.server
        ids = [s.get("Id") for s in songs]
        rows = [Text(route.get("title", ""), size=26, bold=True),
                self._music_action_bar(server, ids, route["item_id"], "gen")]
        rows += self._grid_of(albums, "mgenre", size, geom=self.geom_square)
        return VScroll(Column(rows, pad=16, gap=12), id="mgenre", flex=1)

    def _render_playlist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        server = route.get("server") or self.server
        pid = route["item_id"]
        ids = [i.get("Id") for i in data]
        pl_item = {"Id": pid, "Type": "Playlist",
                   "Name": route.get("title", "")}
        header = Row([
            Text(route.get("title", ""), size=28, bold=True),
            Spacer(),
            self._action_btn("play_arrow", _("Play All"), "pl-play",
                             lambda: self._play_list(ids, server, 0,
                                                     audio=True), on=True),
            self._action_btn("shuffle", _("Shuffle"), "pl-shuffle",
                             lambda: self._play_shuffle(ids, server)),
            self._action_btn("file_download", _("Download"), "pl-download",
                             lambda: self._open_download(pl_item)),
            Button(_("Edit"), id="pl-edit", on_click=lambda: self.navigate({
                "kind": "playlist_edit", "server": server,
                "item_id": pid, "title": route.get("title", "")})),
        ], align="center", gap=10)
        rows = [header] + self._grid_of(data, "pl", size)
        return VScroll(Column(rows, pad=16, gap=12), id="playlist", flex=1)

    # -------------------------------------------------- now-playing bar

    @staticmethod
    def _fmt(secs):
        secs = int(secs or 0)
        return "%d:%02d" % (secs // 60, secs % 60)

    def _ctl(self, fn):
        if self.controller is not None:
            fn(self.controller)

    _REPEAT = ["none", "all", "one"]

    def _cycle_repeat(self):
        np = self._now_playing or {}
        cur = np.get("repeat", "none")
        nxt = self._REPEAT[(self._REPEAT.index(cur) + 1) % 3] \
            if cur in self._REPEAT else "all"
        np["repeat"] = nxt
        self._ctl(lambda c: c.set_repeat(nxt))
        self.invalidate()

    def _toggle_np_favorite(self):
        np = self._now_playing or {}
        np["favorite"] = not np.get("favorite")
        self._ctl(lambda c: c.toggle_favorite())
        self.invalidate()

    def _now_playing_bar(self, w):
        np = self._now_playing
        pos = np.get("position", 0) or 0
        dur = np.get("duration", 0) or 0
        pp = "play_arrow" if np.get("paused") else "pause"
        repeat = np.get("repeat", "none")

        def tbtn(icon, node_id, cb, color="eeeeee"):
            return Box([Icon(icon, 22, color=color)], id=node_id, pad=8,
                       bg=theme.BUTTON_BG, hover={"fill": theme.BUTTON_ACTIVE},
                       radius=6, align="center", direction="row", on_click=cb)

        seek = Slider("np-seek", value=pos, min=0, max=max(1, dur),
                      force=True, flex=1,
                      on_change=lambda v: self._ctl(lambda c: c.seek(v)))
        title = np.get("title", "")
        sub = np.get("artist") or np.get("album") or ""
        return Row(
            [
                Column([Text(title, size=16, bold=True),
                        Text(sub, size=13, color=theme.SUBTLE_FG)],
                       gap=2, w=220),
                tbtn("skip_previous", "np-prev",
                     lambda: self._ctl(lambda c: c.prev())),
                tbtn(pp, "np-pp", lambda: self._ctl(lambda c: c.toggle_pause())),
                tbtn("skip_next", "np-next",
                     lambda: self._ctl(lambda c: c.next())),
                tbtn("stop", "np-stop", lambda: self._ctl(lambda c: c.stop())),
                Text(self._fmt(pos), size=14, w=48, color=theme.SUBTLE_FG),
                seek,
                Text(self._fmt(dur), size=14, w=48, color=theme.SUBTLE_FG),
                tbtn("favorite" if np.get("favorite") else "favorite_border",
                     "np-fav", lambda: self._toggle_np_favorite(),
                     color=theme.FAV_RED if np.get("favorite") else "eeeeee"),
                tbtn("repeat_one" if repeat == "one" else "repeat", "np-repeat",
                     lambda: self._cycle_repeat(),
                     color=theme.ACCENT if repeat != "none" else "888888"),
                Icon("volume_up", 20, color="aaaaaa"),
                Slider("np-vol", value=np.get("volume", 100), min=0, max=100,
                       w=110,
                       on_change=lambda v: self._ctl(lambda c: c.set_volume(v))),
                tbtn("queue_music", "np-queue", self._open_queue),
            ],
            pad=10, gap=10, align="center", h=64, bg=theme.PANEL_BG)

    # ------------------------------------------------------------- settings

    def _config(self):
        if self._config_obj is not None:
            return self._config_obj
        from . import config as cfg
        return cfg

    def _open_settings(self):
        self.navigate({"kind": "settings", "server": self.server,
                       "title": _("Settings")})

    def _set_setting(self, key, value):
        ok = self._config().set_setting(key, value)
        self.status = ((_("Saved: %s") if ok else _("Invalid value: %s"))
                       % key)
        self.invalidate()

    def _render_settings(self, route, size):
        cfg = self._config()
        schema = cfg.settings_schema()
        values = cfg.get_settings()
        rows = [Text(_("Settings"), size=26, bold=True)]
        if self.status:
            rows.append(Text(self.status, size=15, color=theme.SUBTLE_FG))
        for key in sorted(schema):
            kind = schema[key]
            val = values.get(key)
            if kind == "bool":
                rows.append(Checkbox(
                    key, bool(val), id="set-" + key,
                    on_toggle=lambda k=key, v=val: self._set_setting(
                        k, not bool(v))))
            else:
                rows.append(Row([
                    Text(key, w=360, size=17),
                    TextBox("set-" + key,
                            text="" if val is None else str(val), w=340,
                            on_submit=lambda v, k=key: self._set_setting(k, v)),
                ], gap=12, align="center"))
        return VScroll(Column(rows, pad=16, gap=8), id="settings", flex=1)

    # --------------------------------------------------------------- queue

    def _render_queue(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        entries = data.get("entries") or []
        current = data.get("current_id")
        sel = route.get("_sel")
        toolbar = Row([
            Text(_("Play Queue"), size=26, bold=True), Spacer(),
            Button(_("Top"), id="q-top",
                   on_click=lambda: self._queue_move(route, "top")),
            Button(_("Up"), id="q-up",
                   on_click=lambda: self._queue_move(route, "up")),
            Button(_("Down"), id="q-down",
                   on_click=lambda: self._queue_move(route, "down")),
            Button(_("Bottom"), id="q-bottom",
                   on_click=lambda: self._queue_move(route, "bottom")),
        ], gap=8, align="center")
        rows = [toolbar]
        if not entries:
            rows.append(Text(_("The queue is empty."), size=18,
                             color=theme.SUBTLE_FG))
        for i, e in enumerate(entries):
            item = e["item"]
            playing = item.get("Id") == current
            secs = (item.get("RunTimeTicks") or 0) // 10000000
            rows.append(Row([
                Box([Icon("play_arrow", 18,
                          color=theme.ACCENT if playing else "cccccc")],
                    id="q-play-%d" % i, w=44, h=30, align="center",
                    direction="row", hover={"fill": theme.BUTTON_ACTIVE},
                    radius=4,
                    on_click=lambda pid=e["pid"]: self._queue_skip(pid)),
                Text(item.get("Name", ""), flex=1, size=17, bold=playing),
                Text(", ".join(item.get("Artists") or []), w=180, size=14,
                     color=theme.SUBTLE_FG),
                Text("%d:%02d" % (secs // 60, secs % 60) if secs else "",
                     w=56, size=14, color=theme.SUBTLE_FG),
                Button(_("Remove"), id="q-rm-%d" % i,
                       on_click=lambda pid=e["pid"]: self._queue_remove(pid)),
            ], id="q-%d" % i, pad=8, gap=10, radius=6, align="center",
               bg=(theme.ACCENT if sel == i else
                   (theme.PANEL_BG if playing else None)),
               hover=None if sel == i else {"fill": theme.BUTTON_BG},
               on_click=lambda i=i: self._queue_select(route, i)))
        return VScroll(Column(rows, pad=16, gap=3), id="queue", flex=1)

    def _queue_select(self, route, i):
        route["_sel"] = i
        self.invalidate()

    def _queue_move(self, route, where):
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        i = route.get("_sel")
        if i is None or not entries:
            return
        n = len(entries)
        j = {"top": 0, "up": max(0, i - 1),
             "down": min(n - 1, i + 1), "bottom": n - 1}[where]
        if j == i:
            return
        entries.insert(j, entries.pop(i))
        route["_sel"] = j
        self._client_call(lambda c: c.queue_reorder(
            [e["pid"] for e in entries if e.get("pid")]))
        self.invalidate()

    def _queue_skip(self, pid):
        if pid and self.controller is not None:
            self._safe(lambda c: c.skip_to(pid))

    def _queue_remove(self, pid):
        if pid and self.controller is not None:
            self._safe(lambda c: c.queue_remove([pid]))
        self.route.pop("_data", None)   # refresh the queue view
        self._bump_epoch()
        self._load_route(self.route)
        self.invalidate()

    # ------------------------------------------------------------- banners

    def notify_update(self, version, url):
        """Registered as playerManager.notify_update: show the update notice
        as a browser banner (mirrors the Tk browser / CLI-OSD split)."""
        self._update = {"version": version, "url": url}
        self.invalidate()

    def set_offline(self, offline):
        offline = bool(offline)
        if offline != self._offline:
            self._offline = offline
            self.invalidate()

    def _banner(self):
        if self._offline:
            return Row([
                Text(_("Offline — showing what's available."), size=16),
                Spacer(),
                Button(_("Retry"), id="banner-retry",
                       on_click=self._retry_connect),
            ], pad=10, gap=10, align="center", h=48, bg="5a3a1a")
        if self._update:
            return Row([
                Text(_("Update available: %s") % self._update["version"],
                     size=16),
                Spacer(),
                Button(_("Open"), id="banner-open",
                       on_click=lambda: self._open_url(self._update["url"])),
                Button(_("Dismiss"), id="banner-dismiss",
                       on_click=self._dismiss_update),
            ], pad=10, gap=10, align="center", h=48, bg="2a3a5a")
        return None

    def _dismiss_update(self):
        self._update = None
        self.invalidate()

    def _open_url(self, url):
        if self.controller is not None and url:
            self._safe(lambda c: c.open_url(url))
        self._dismiss_update()

    def _retry_connect(self):
        if self.controller is not None:
            self._pool.submit(lambda: self._safe(lambda c: c.retry_connect()))

    # --------------------------------------------------------- playlist edit

    def _render_playlist_edit(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        sel = route.get("_sel")
        server = route.get("server") or self.server
        pid = route["item_id"]
        toolbar = Row([
            Button(_("Top"), id="pe-top",
                   on_click=lambda: self._pe_move(route, "top")),
            Button(_("Up"), id="pe-up",
                   on_click=lambda: self._pe_move(route, "up")),
            Button(_("Down"), id="pe-down",
                   on_click=lambda: self._pe_move(route, "down")),
            Button(_("Bottom"), id="pe-bottom",
                   on_click=lambda: self._pe_move(route, "bottom")),
            Spacer(),
            Button(_("Remove"), id="pe-remove",
                   on_click=lambda: self._pe_remove(route)),
        ], gap=8, align="center")
        rename_row = Row([
            TextBox("pe-name", text=route.get("title", ""), w=280,
                    on_change=lambda v: route.__setitem__("_newname", v)),
            Button(_("Rename"), id="pe-rename",
                   on_click=lambda: self._pe_rename(route)),
            Checkbox(_("Public"), bool(route.get("_public")), id="pe-public",
                     on_toggle=lambda: self._pe_toggle_public(route)),
        ], gap=10, align="center")
        rows = [Text("%s — %s" % (route.get("title", ""), _("Edit")),
                     size=26, bold=True), rename_row, toolbar]
        for i, it in enumerate(items):
            rows.append(Row([
                Text(str(i + 1), w=44, size=17, color=theme.SUBTLE_FG),
                Text(it.get("Name", ""), flex=1, size=17,
                     bold=(sel == i)),
            ], id="pe-row-%d" % i, pad=8, gap=8, radius=6, align="center",
               bg=theme.PANEL_BG if sel == i else None,
               hover=None if sel == i else {"fill": theme.BUTTON_BG},
               on_click=lambda i=i: self._pe_select(route, i)))
        return VScroll(Column(rows, pad=16, gap=3), id="playlist-edit", flex=1)

    def _pe_select(self, route, i):
        route["_sel"] = i
        self.invalidate()

    def _pe_move(self, route, where):
        items = route.get("_items") or []
        i = route.get("_sel")
        if i is None or not items:
            return
        n = len(items)
        j = {"top": 0, "up": max(0, i - 1),
             "down": min(n - 1, i + 1), "bottom": n - 1}[where]
        if j == i:
            return
        entry = items.pop(i)
        items.insert(j, entry)
        route["_sel"] = j
        self._client_call(lambda c: c.playlist_move(
            route.get("server") or self.server, route["item_id"],
            entry.get("PlaylistItemId"), j))
        self.invalidate()

    def _pe_remove(self, route):
        items = route.get("_items") or []
        i = route.get("_sel")
        if i is None or i >= len(items):
            return
        entry = items.pop(i)
        route["_sel"] = None
        self._client_call(lambda c: c.playlist_remove(
            route.get("server") or self.server, route["item_id"],
            [entry.get("PlaylistItemId")]))
        self.invalidate()

    def _pe_rename(self, route):
        name = (route.get("_newname") or route.get("title") or "").strip()
        if not name:
            return
        route["title"] = name
        self._client_call(lambda c: c.playlist_update(
            route.get("server") or self.server, route["item_id"], name=name))
        self.invalidate()

    def _pe_toggle_public(self, route):
        route["_public"] = not route.get("_public")
        self._client_call(lambda c: c.playlist_update(
            route.get("server") or self.server, route["item_id"],
            is_public=route["_public"]))
        self.invalidate()

    # ----------------------------------------------------- add to playlist

    def _open_add_to(self, item):
        server = self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        ep = self._epoch

        def work():
            try:
                return self.source.get_playlists(server)
            except Exception:
                return []
        self.run_async(work, lambda pls: self._show_add_to(server, item, pls),
                       ep)

    def _show_add_to(self, server, item, playlists):
        item_id = item.get("Id")
        self._addto_name = {"name": ""}

        def build():
            rows = [Text(_("Add to Playlist"), size=22, bold=True)]
            for i, pl in enumerate(playlists):
                rows.append(Button(
                    pl.get("Name", ""), id="add-pl-%d" % i,
                    on_click=lambda pid=pl.get("Id"): self._add_to(
                        server, pid, item_id)))
            if not playlists:
                rows.append(Text(_("No playlists yet."), size=15,
                                 color=theme.SUBTLE_FG))
            rows.append(Row([
                TextBox("add-newname", placeholder=_("New playlist name…"),
                        w=280,
                        on_change=lambda v: self._addto_name.__setitem__(
                            "name", v)),
                Button(_("Create"), id="add-create",
                       on_click=lambda: self._add_to_new(server, item_id)),
            ], gap=10, align="center"))
            rows.append(Row([Spacer(),
                             Button(_("Close"), id="add-close",
                                    on_click=self._close_dialog)], gap=10))
            return Dialog("addto",
                          self._dialog_shell("addto", rows, w=460),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _add_to_new(self, server, item_id):
        name = (self._addto_name or {}).get("name", "").strip()
        if name and item_id:
            self._client_call(lambda c: c.playlist_new(server, name, [item_id]))
        self._close_dialog()

    def _add_to(self, server, playlist_id, item_id):
        if playlist_id and item_id:
            self._client_call(lambda c: c.playlist_add(
                server, playlist_id, [item_id]))
        self._close_dialog()

    # -------------------------------------------------------- downloads

    @staticmethod
    def _human_size(n):
        n = float(n or 0)
        for unit in ("B", "KB", "MB", "GB", "TB"):
            if n < 1024 or unit == "TB":
                return ("%d %s" % (n, unit) if unit == "B"
                        else "%.1f %s" % (n, unit))
            n /= 1024

    def _open_download(self, item):
        server = self.route.get("server") or self.server
        if self.controller is None or server is None:
            return
        self._dl = {"server": server, "item": item, "est": None,
                    "watched": False}
        ep = self._epoch

        def work():
            return self.controller.download_estimate(
                server, item.get("Id"), item.get("Type"))

        def done(est):
            if self._dl is not None:
                self._dl["est"] = est
                self._dl["watched"] = bool((est or {}).get("audio_only"))
            self._show_download()
        self.run_async(work, done, ep)
        self._show_download()   # show immediately with an "estimating" state

    def _show_download(self):
        dl = self._dl
        if dl is None:
            return

        def build():
            est = dl["est"]
            if est is None:
                info = Text(_("Estimating…"), size=15, color=theme.SUBTLE_FG)
            else:
                line = _("%(count)d items · %(size)s") % {
                    "count": est.get("count", 0),
                    "size": self._human_size(est.get("total_bytes", 0))}
                extra = []
                if est.get("already_count"):
                    extra.append(_("%d already downloaded")
                                 % est["already_count"])
                if est.get("watched_count"):
                    extra.append(_("%d watched") % est["watched_count"])
                if extra:
                    line += "   (" + ", ".join(extra) + ")"
                info = Text(line, size=15, color=theme.SUBTLE_FG)
            return Dialog("download", self._dialog_shell("download", [
                Text(_("Download"), size=22, bold=True),
                Text(dl["item"].get("Name", ""), size=17),
                info,
                Checkbox(_("Include watched"), dl["watched"],
                         id="dl-watched", on_toggle=self._dl_toggle_watched),
                Row([Spacer(),
                     Button(_("Cancel"), id="dl-cancel",
                            on_click=self._close_download),
                     Button(_("Download"), id="dl-ok",
                            on_click=self._dl_confirm)], gap=10),
            ], w=460), on_dismiss=self._close_download)
        self._show_dialog(build)

    def _dl_toggle_watched(self):
        if self._dl is not None:
            self._dl["watched"] = not self._dl["watched"]
            self._show_download()

    def _close_download(self):
        self._dl = None
        self._close_dialog()

    def _dl_confirm(self):
        dl = self._dl
        if dl is not None:
            item = dl["item"]
            self._client_call(lambda c: c.download_enqueue(
                dl["server"], item.get("Id"), item.get("Type"),
                dl["watched"]))
        self._close_download()
        self._refresh_downloaded()

    # ------------------------------------------------------------- dialogs

    def _show_dialog(self, builder):
        self._dialog = builder
        self.invalidate()

    def _close_dialog(self):
        self._dialog = None
        self.invalidate()

    @staticmethod
    def _dialog_shell(node_id, children, w=440):
        return Column(children, pad=24, gap=14, bg="1e1e1e", radius=12,
                      border="555555", w=w)

    def _message(self, text, title=None):
        title = title or _("Notice")

        def build():
            return Dialog("msg", self._dialog_shell("msg", [
                Text(title, size=22, bold=True),
                Text(text, size=16, color=theme.SUBTLE_FG),
                Row([Spacer(), Button(_("OK"), id="dlg-ok",
                                      on_click=self._close_dialog)], gap=10),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _confirm(self, text, on_yes, title=None, yes=None):
        title = title or _("Confirm")
        yes = yes or _("OK")

        def build():
            return Dialog("confirm", self._dialog_shell("confirm", [
                Text(title, size=22, bold=True),
                Text(text, size=16, color=theme.SUBTLE_FG),
                Row([Spacer(),
                     Button(_("Cancel"), id="dlg-cancel",
                            on_click=self._close_dialog),
                     Button(yes, id="dlg-ok",
                            on_click=lambda: (self._close_dialog(), on_yes()))],
                    gap=10),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    # -- SyncPlay ---------------------------------------------------------

    def _open_syncplay(self):
        server = self.server
        if self.controller is None or server is None:
            return
        ep = self._epoch

        def work():
            return self.controller.get_sync_groups(server)

        def done(groups):
            self._show_syncplay(server, groups)

        # Fetch groups off-thread, then show the dialog on the loop.
        self.run_async(work, done, ep)

    def _show_syncplay(self, server, groups):
        def build():
            rows = [Text(_("SyncPlay"), size=22, bold=True)]
            if groups:
                for i, g in enumerate(groups):
                    who = ", ".join(g.get("participants") or [])
                    rows.append(Column([
                        Button(g.get("name") or _("Group"),
                               id="sp-join-%d" % i,
                               on_click=lambda gid=g.get("id"):
                                   self._sync_join(server, gid)),
                        Text(who, size=13, color=theme.SUBTLE_FG)
                        if who else Spacer(h=0),
                    ], gap=2))
            else:
                rows.append(Text(_("No active groups."), size=15,
                                 color=theme.SUBTLE_FG))
            rows.append(Row([
                Button(_("New Group"), id="sp-new",
                       on_click=lambda: self._sync_new(server)),
                Button(_("Leave"), id="sp-leave",
                       on_click=lambda: self._sync_leave(server)),
                Button(_("Refresh"), id="sp-refresh",
                       on_click=lambda: self._open_syncplay()),
                Spacer(),
                Button(_("Close"), id="sp-close", on_click=self._close_dialog),
            ], gap=10))
            return Dialog("syncplay", self._dialog_shell("syncplay", rows,
                                                         w=480),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _sync_join(self, server, group_id):
        self._client_call(lambda c: c.sync_join(server, group_id))
        self._close_dialog()

    def _sync_new(self, server):
        self._client_call(lambda c: c.sync_new(server))
        self._close_dialog()

    def _sync_leave(self, server):
        self._client_call(lambda c: c.sync_leave(server))
        self._close_dialog()

    # --------------------------------------------------------------- login

    def show_login(self):
        """Show the add-server / login screen (no servers connected)."""
        self.navigate({"kind": "login", "title": _("Sign In")}, reset=True)

    def _render_login(self, route, size):
        def field(fid, ph, key, mask=False):
            return Row([
                Text(ph, w=140, size=17, color=theme.SUBTLE_FG),
                TextBox(fid, text=self._login[key], placeholder=ph, mask=mask,
                        w=360,
                        on_change=lambda v, k=key: self._login.__setitem__(
                            k, v)),
            ], gap=12, align="center")

        form = Column([
            Text(_("Connect to Jellyfin"), size=28, bold=True),
            field("login-server", _("Server URL"), "server"),
            field("login-user", _("Username"), "user"),
            field("login-pass", _("Password"), "pass", mask=True),
            Row([Spacer(),
                 Button(_("Connect"), id="login-connect",
                        on_click=self._do_login)], gap=10),
        ], pad=28, gap=16, bg=theme.CARD_BG, radius=12, border=theme.BORDER,
           w=560)
        if self._login_error:
            form.children.insert(1, Text(self._login_error, size=15,
                                         color=theme.FAV_RED))
        return Box([Spacer(),
                    Row([Spacer(), form, Spacer()]),
                    Spacer()],
                   flex=1, direction="column", align="stretch", gap=10)

    def _do_login(self):
        if self.controller is None:
            return
        info = dict(self._login)
        self._login_error = _("Connecting…")
        self.invalidate()
        ep = self._epoch

        def work():
            return self.controller.add_server(
                info["server"], info["user"], info["pass"])

        def done(ok):
            if ok:
                self._login_error = None
                self._after_login()
            else:
                self._login_error = _(
                    "Could not connect. Please check your details.")
        self.run_async(work, done, ep)

    def _after_login(self):
        source = None
        if self.controller is not None:
            try:
                source = self.controller.rebuild_source()
            except Exception:
                log.warning("rebuild_source failed", exc_info=True)
        if source is not None:
            self.set_source(source)

    # -------------------------------------------------------------- locked

    def show_locked(self):
        """Show the startup-PIN unlock gate."""
        self.navigate({"kind": "locked", "title": _("Locked")}, reset=True)

    def _render_locked(self, route, size):
        form = Column([
            Text(_("Enter your PIN"), size=28, bold=True),
            TextBox("lock-pin", text="", placeholder=_("PIN"), mask=True,
                    w=240, on_change=lambda v: self._pin.__setitem__("pin", v),
                    on_submit=lambda v: self._do_unlock()),
            Row([Spacer(),
                 Button(_("Unlock"), id="lock-unlock",
                        on_click=self._do_unlock)], gap=10),
        ], pad=28, gap=16, bg=theme.CARD_BG, radius=12, border=theme.BORDER,
           w=420)
        if self._pin_error:
            form.children.insert(1, Text(self._pin_error, size=15,
                                         color=theme.FAV_RED))
        return Box([Spacer(), Row([Spacer(), form, Spacer()]), Spacer()],
                   flex=1, direction="column", align="stretch")

    def _do_unlock(self):
        if self.controller is None:
            return
        pin = self._pin.get("pin", "")
        ep = self._epoch

        def work():
            if not self.controller.unlock(pin):
                return None
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is None:
                self._pin_error = _("Incorrect PIN.")
            else:
                self._pin_error = None
                self.set_source(source)
        self.run_async(work, done, ep)

    def _busy(self):
        return Box(
            [Spacer(), Row([Spacer(), Busy(), Spacer()]), Spacer()],
            flex=1, direction="column", align="stretch",
        )

    # --------------------------------------------------------------- lifecycle

    def run(self):
        """Block the calling thread driving the app loop (spawned-app / demo
        use). For the shared-window integration this runs on a dedicated
        thread next to playerManager — see 0.2/0.5 wiring."""
        self.app.run(self.build)

    def shutdown(self):
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.thumbs is not None:
            self.thumbs.shutdown()
        self.strips.clear()
