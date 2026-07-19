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
from ..mpvtk.rawimage import cache_dir
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Column,
    HScroll,
    ImageMap,
    Row,
    Spacer,
    Text,
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

    # -------------------------------------------------------- tile helpers

    def _subtitle(self, item):
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                return "S%dE%d" % (s, e)
        return str(item.get("ProductionYear") or "")

    def _poster_for(self, item):
        """Return (PIL image or None, cache tag). Requests the poster once
        if absent; the strip recomposites when it arrives (tag changes)."""
        spec = self.source.image_spec(item, "Primary", self.geom.tile_w)
        if not spec:
            return None, ""
        item_id, itype, itag = spec
        key = make_key(item_id, itype, itag, self.geom.tile_w)
        img = self._posters.get(key)
        if (img is None and self.thumbs is not None
                and key not in self._requested and self.server is not None):
            url = self.source.image_url(
                self.server, item_id, itype, itag,
                self.geom.tile_w, self.geom.tile_h, fill=True,
            )
            if url:
                self._requested.add(key)
                self.thumbs.request(
                    key, url, (self.geom.tile_w, self.geom.tile_h),
                    lambda im, k=key: self._posters.__setitem__(k, im),
                )
        return img, key

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
        if t in FOLDER_TYPES or item.get("CollectionType") or t in SERIES_TYPES:
            self.navigate({
                "kind": "grid",
                "server": self.route.get("server") or self.server,
                "parent_id": item.get("Id"),
                "title": item.get("Name", ""),
            })
        elif t in PLAYABLE_TYPES and self.controller is not None:
            self._enter_playback(item)
        else:
            self.status = _("Selected: %s") % item.get("Name", "")
            self.invalidate()

    # ------------------------------------------------- browse <-> playback

    def enter_browse(self):
        """Show the browser: take the window + hide the OSC, then render."""
        self._browsing = True
        if self.controller is not None:
            self.controller.on_browse_enter()
        self.invalidate()

    def _enter_playback(self, item):
        """Yield the window to playback + the OSC, then start the item."""
        self._browsing = False
        server = self.route.get("server") or self.server
        if self.controller is not None:
            self.controller.on_browse_leave()
        self.invalidate()  # push an empty scene so overlays clear off the video
        if self.controller is not None:
            self.controller.play(item, server)

    def on_playstate(self, state):
        """Registered as playerManager.on_playstate. A ``stopped`` snapshot
        (playback ended / aborted) returns us to browse; anything else means
        playback is live, so stay yielded."""
        if state and state.get("stopped"):
            if not self._browsing:
                self.enter_browse()
        else:
            self._browsing = False
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
                Button(_("Settings"), id="nav-settings", on_click=lambda: None),
            ],
            pad=12, gap=10, align="center", h=60, bg=theme.PANEL_BG,
        )

    def _render_route(self, route, size):
        kind = route["kind"]
        if kind == "home":
            return self._render_home(route, size)
        if kind == "grid":
            return self._render_grid(route, size)
        return self._busy()

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
