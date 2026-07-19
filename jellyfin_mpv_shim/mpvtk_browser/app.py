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
    Column,
    Dropdown,
    HScroll,
    Icon,
    Image,
    ImageMap,
    Row,
    Spacer,
    Text,
    TextBox,
    VScroll,
)
from . import theme
from .repository import FOLDER_TYPES, PLAYABLE_TYPES, SERIES_TYPES
from .strips import StripStore, Tile, TileGeom
from .thumbnails import make_key

log = logging.getLogger("mpvtk_browser.app")

# Routes that take over the whole surface (no nav chrome), like the Tk
# browser's login/locked/connecting screens.
CHROME_FREE = {"login", "locked", "connecting"}


class MpvtkBrowser:
    def __init__(self, app, source, strips=None, thumbs=None,
                 server_uuid=None, geom=None, controller=None):
        self.app = app            # mpvtk.MpvtkApp (attached or spawned)
        self.source = source
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
        self.geom = geom or TileGeom()
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
            def work():
                return self.source.get_library_items(
                    route.get("server") or self.server, route["parent_id"]
                )
            def done(res):
                route["_items"], route["_total"] = res
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
                if tab == "artists":
                    return self.source.get_album_artists(srv, parent)[0]
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
                return self.source.get_artist_albums(srv, iid)
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "music_genre":
            srv = route.get("server") or self.server

            def work():
                return self.source.get_genre_albums(
                    srv, route.get("parent_id"), route["item_id"])[0]
            self.run_async(work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "playlist":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return self.source.get_playlist_items(srv, iid)
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

    def _poster_for(self, item):
        """Return (PIL image or None, cache tag). Requests the poster once
        if absent; the strip recomposites when it arrives (tag changes)."""
        spec = self.source.image_spec(item, "Primary", self.geom.tile_w)
        if not spec or self.server is None:
            return None, ""
        item_id, itype, itag = spec
        key = make_key(item_id, itype, itag, self.geom.tile_w)
        box = (self.geom.tile_w, self.geom.tile_h)
        url = self.source.image_url(self.server, item_id, itype, itag,
                                    self.geom.tile_w, self.geom.tile_h, fill=True)
        return self._request_image(key, url, box), key

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

    def _tile(self, item):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        rt = item.get("RunTimeTicks") or 0
        poster, tag = self._poster_for(item)
        return Tile(
            key=item.get("Id", ""),
            title=item.get("Name", ""),
            subtitle=self._subtitle(item),
            poster=poster,
            poster_tag=tag,
            watched=bool(ud.get("Played")),
            badge=int(ud.get("UnplayedItemCount") or 0),
            progress=(pos / rt) if (pos and rt) else 0.0,
        )

    def _image_map(self, items, prefix):
        tiles = [self._tile(it) for it in items]
        s = self.strips.strip(tiles)
        regions = []
        for r, it in zip(s["regions"], items):
            regions.append(dict(
                r,
                id="%s-%s" % (prefix, r["key"]),
                on_click=(lambda i=it: self._open_item(i)),
            ))
        return ImageMap(s["src"], s["iw"], s["ih"], regions=regions)

    def _tile_row(self, title, items, row_id):
        return Column(
            [
                Text(title, size=24, bold=True),
                HScroll(self._image_map(items, row_id),
                        id=row_id, h=self.geom.strip_h + 6),
            ],
            gap=8,
        )

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

    def _play(self, item, server, offset_ticks=None):
        """Yield/keep-browse and start a single ``item`` (Play/Resume)."""
        self._start(audio=item.get("Type") == "Audio")
        if self.controller is not None:
            self.controller.play(item, server, offset_ticks=offset_ticks)

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
        self.invalidate()

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
        children.append(content)
        if self._now_playing is not None and route["kind"] not in CHROME_FREE:
            children.append(self._now_playing_bar(w))
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
        return Row(
            left + [
                Text(title, size=22, bold=True),
                Spacer(),
                TextBox("nav-search", placeholder=_("Search…"), w=220,
                        on_submit=self._search),
                Button(_("Settings"), id="nav-settings", on_click=lambda: None),
            ],
            pad=12, gap=10, align="center", h=60, bg=theme.PANEL_BG,
        )

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
        }.get(kind)
        if render is None:
            return self._busy()
        return render(route, size)

    def _render_home(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        rows = []
        if data["libraries"]:
            rows.append(self._tile_row(
                _("Libraries"), data["libraries"], "row-libs"))
        for i, hr in enumerate(data["rows"]):
            if hr.get("items"):
                rows.append(self._tile_row(
                    hr["title"], hr["items"], "row-%d" % i))
        if not rows:
            rows.append(Text(_("Nothing to show yet."), size=20,
                             color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=16, gap=20), id="home", flex=1)

    def _render_grid(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        w = size[0]
        g = self.geom
        cols = max(1, int((w - 32 + g.gap) // (g.tile_w + g.gap)))
        rows = [Text(route.get("title", ""), size=26, bold=True)]
        for start in range(0, len(items), cols):
            chunk = items[start:start + cols]
            rows.append(self._image_map(chunk, "grid-%d" % start))
        return VScroll(
            Column(rows, pad=16, gap=12), id="grid", flex=1,
            on_scroll=lambda off, mx: self._on_grid_scroll(route, off, mx),
        )

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

        def work():
            return self.source.get_library_items(
                route.get("server") or self.server, route["parent_id"],
                start_index=start)

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

    def _play_buttons(self, item, server):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        buttons = []
        if pos > 0:
            secs = pos // 10000000
            buttons.append(Row(
                [Icon("play_arrow", 20, color="101010"),
                 Text(_("Resume") + "  %d:%02d" % (secs // 60, secs % 60),
                      size=18, color="101010")],
                id="btn-resume", gap=6, pad=10, bg=theme.ACCENT, radius=6,
                align="center",
                on_click=lambda: self._play(item, server, offset_ticks=pos)))
        buttons.append(Row(
            [Icon("play_arrow", 20), Text(_("Play"), size=18)],
            id="btn-play", gap=6, pad=10, bg=theme.BUTTON_BG,
            hover={"fill": theme.BUTTON_ACTIVE}, radius=6, align="center",
            on_click=lambda: self._play(item, server)))
        return Row(buttons, gap=10)

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
        blocks = [
            self._backdrop_node(item, (bw, bh), "detail-bd"),
            Text(item.get("Name", ""), size=30, bold=True),
        ]
        meta = self._meta_line(item)
        if meta:
            blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        blocks.append(self._play_buttons(item, server))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, w - 32))
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
        blocks = [
            self._backdrop_node(item, (bw, bh), "series-bd"),
            Text(item.get("Name", ""), size=30, bold=True),
        ]
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, w - 32))
        seasons = data.get("seasons") or []
        if seasons:
            blocks.append(self._tile_row(
                _("Seasons"), seasons, "series-seasons"))
        return VScroll(Column(blocks, pad=16, gap=16), id="series", flex=1)

    def _render_season(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        episodes = data.get("episodes") or []
        seasons = data.get("seasons") or []
        w = size[0]
        g = self.geom
        header = [Text(route.get("title", ""), size=26, bold=True)]
        if len(seasons) > 1:
            names = [s.get("Name", "") for s in seasons]
            cur = next((i for i, s in enumerate(seasons)
                        if s.get("Id") == route["item_id"]), 0)
            header.append(Dropdown(
                "season-switch", names, selected=cur, w=220,
                on_select=lambda i, v: self._switch_season(route, seasons[i])))
        cols = max(1, int((w - 32 + g.gap) // (g.tile_w + g.gap)))
        rows = header
        for start in range(0, len(episodes), cols):
            rows.append(self._image_map(
                episodes[start:start + cols], "ep-%d" % start))
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

    def _grid_of(self, items, prefix, size, heading=None):
        w = size[0]
        g = self.geom
        cols = max(1, int((w - 32 + g.gap) // (g.tile_w + g.gap)))
        rows = [Text(heading, size=26, bold=True)] if heading else []
        for start in range(0, len(items), cols):
            rows.append(self._image_map(items[start:start + cols],
                                        "%s-%d" % (prefix, start)))
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
            self._music_tab(route, _("Artists"), "artists"),
            self._music_tab(route, _("Genres"), "genres"),
        ], gap=8)
        data = route.get("_data")
        body = self._busy() if data is None else VScroll(
            Column(self._grid_of(data, "music", size), pad=16, gap=12),
            id="music-grid", flex=1)
        return Column([Row([tabs], pad=12), body], flex=1, align="stretch")

    def _render_album(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        item = data.get("item") or {}
        tracks = data.get("tracks") or []
        server = route.get("server") or self.server
        ids = [t.get("Id") for t in tracks]
        header = Row([
            Text(item.get("Name") or route.get("title", ""), size=28,
                 bold=True),
            Spacer(),
            Row([Icon("play_arrow", 20, color="101010"),
                 Text(_("Play"), size=18, color="101010")],
                id="album-play", gap=6, pad=10, bg=theme.ACCENT, radius=6,
                align="center",
                on_click=lambda: self._play_list(ids, server, 0, audio=True)),
        ], align="center", gap=10)
        body = self._track_list(
            tracks, "trk",
            lambda i: self._play_list(ids, server, i, audio=True))
        return VScroll(Column([header, body], pad=16, gap=12),
                       id="album", flex=1)

    def _render_artist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        rows = self._grid_of(data, "artist", size,
                             heading=route.get("title", ""))
        return VScroll(Column(rows, pad=16, gap=12), id="artist", flex=1)

    def _render_music_genre(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        rows = self._grid_of(data, "mgenre", size,
                             heading=route.get("title", ""))
        return VScroll(Column(rows, pad=16, gap=12), id="mgenre", flex=1)

    def _render_playlist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        server = route.get("server") or self.server
        ids = [i.get("Id") for i in data]
        header = Row([
            Text(route.get("title", ""), size=28, bold=True),
            Spacer(),
            Row([Icon("play_arrow", 20, color="101010"),
                 Text(_("Play All"), size=18, color="101010")],
                id="pl-play", gap=6, pad=10, bg=theme.ACCENT, radius=6,
                align="center",
                on_click=lambda: self._play_list(ids, server, 0, audio=True)),
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

    def _now_playing_bar(self, w):
        np = self._now_playing
        pos = np.get("position", 0) or 0
        dur = np.get("duration", 0) or 0
        frac = max(0.0, min(1.0, pos / dur)) if dur else 0.0
        pp = "play_arrow" if np.get("paused") else "pause"

        def tbtn(icon, node_id, cb):
            return Box([Icon(icon, 22)], id=node_id, pad=8, bg=theme.BUTTON_BG,
                       hover={"fill": theme.BUTTON_ACTIVE}, radius=6,
                       align="center", direction="row", on_click=cb)

        track = Box(
            [Box(w=max(1, int((w - 620) * frac)), h=6, bg=theme.ACCENT,
                 radius=3)],
            flex=1, h=6, bg=theme.BUTTON_BG, radius=3, direction="row")
        title = np.get("title", "")
        sub = np.get("artist") or np.get("album") or ""
        return Row(
            [
                Column([Text(title, size=16, bold=True),
                        Text(sub, size=13, color=theme.SUBTLE_FG)],
                       gap=2, w=240),
                tbtn("skip_previous", "np-prev",
                     lambda: self._ctl(lambda c: c.prev())),
                tbtn(pp, "np-pp", lambda: self._ctl(lambda c: c.toggle_pause())),
                tbtn("skip_next", "np-next",
                     lambda: self._ctl(lambda c: c.next())),
                tbtn("stop", "np-stop", lambda: self._ctl(lambda c: c.stop())),
                Text(self._fmt(pos), size=14, w=52, color=theme.SUBTLE_FG),
                track,
                Text(self._fmt(dur), size=14, w=52, color=theme.SUBTLE_FG),
            ],
            pad=10, gap=12, align="center", h=64, bg=theme.PANEL_BG)

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
