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
import time
from concurrent.futures import ThreadPoolExecutor

from ..i18n import _
from ..mpvtk.rawimage import cache_dir
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Checkbox,
    Column,
    Dialog,
    Dropdown,
    Grid,
    HScroll,
    Icon,
    Image,
    ImageMap,
    Menu,
    Progress,
    Row,
    Slider,
    Spacer,
    Stack,
    Table,
    Text,
    TextBox,
    VScroll,
    virtual_window,
)
from ..mpvtk.layout import natural_size
from . import theme
from .hud import build_hud
from .repository import (FOLDER_TYPES, PLAYABLE_TYPES,
                         PLAYLIST_SUPPORTED_TYPES, SERIES_TYPES)
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
    # Horizontal padding of ordinary page content.
    CONTENT_PAD = 16

    def __init__(self, app, source, strips=None, thumbs=None,
                 server_uuid=None, geom=None, controller=None, config=None):
        # Before anything is built: the toolkit's accented widgets read the
        # palette at construction time.
        theme.apply_to_toolkit()
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
        # True in the "minimized" player state: playback_abort with
        # force_window off, i.e. no window at all and the app reachable only
        # from the tray (still a valid cast target). See minimize().
        self._minimized = False
        # Latest now-playing snapshot (from on_playstate) for the audio bar,
        # plus the 1s ticker that keeps its clock moving (see _start_np_ticker).
        self._now_playing = None
        self._np_thread = None
        self._np_stop = threading.Event()
        # Playback HUD (video, osc_style "mpvtk"): the renderer owns the
        # summon/auto-hide lifecycle and reports it via on_hud; True while
        # the HUD scene should be on screen. _hud_state is the latest video
        # playstate snapshot feeding its bar (see hud.py).
        self._hud_shown = False
        self._hud_state = None
        # Seek-scrub in flight: the slider's pending target in seconds
        # (None when not scrubbing). Drives the preview thumbnail and the
        # clock; committed to a real seek on gesture end (see hud.py).
        self._hud_scrub = None
        # Open settings-menu level in the HUD ("root", "speed", …) or None.
        self._hud_menu = None
        # Wires on_hud/on_hud_skip (and re-wires on_nav) on the app —
        # shared with mpv re-creation, which attaches a fresh app.
        self.set_app(app)
        # Poller that refreshes the downloads view while transfers run.
        self._dl_thread = None
        # Global download progress for the status bar, and its poller.
        self._dl_status = None
        self._dlbar_thread = None
        # True while keyboard/remote navigation drives the UI (renderer
        # 'nav' events): carousels hide their pointer arrows and rely on
        # focus-driven auto-scroll instead. Any mouse press clears it.
        # (Wired onto the app by set_app above.)
        self._nav_mode = False
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
        # Live text of the chrome search box (the renderer owns the widget; we
        # mirror it so the search *button* can read it).
        self._search_box = {"term": ""}
        # Live text of the "new user" box in Settings → Servers & Users.
        self._newuser = {"name": ""}
        # Scroll offsets: _live_offsets is the renderer's authoritative
        # snapshot read once per build; _scroll_off is the throttled
        # on_scroll copy, used only as a fallback. See _offset().
        self._scroll_off = {}
        self._live_offsets = None
        # Startup-PIN lock screen state. _locked is True while the gate is
        # actually gating: tray commands that would navigate (Configure
        # Servers, Show Console) are swallowed while it is set, so they
        # can't reveal content from behind the lock.
        self._pin = {"pin": ""}
        self._pin_error = None
        self._locked = False
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
        # thumb key -> (failed attempts, earliest retry time). Only holds
        # keys whose fetch failed transiently; see _image_done.
        self._img_retry = {}
        self.status = ""
        self._size = None         # last window size seen by build()

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
        self._reset_scroll()
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _reset_scroll(self):
        """Forget recorded scroll offsets on a route change.

        Scroll container ids are per-view ("grid", "playlist", …), not per
        route, so a deep scroll in one library used to carry into the next
        view opened under the same id. The renderer clamps its own offset to
        the new (shorter) content, but our copy didn't — so virtualization
        windowed rows that were far past the end and the view rendered
        empty: "7 items" in the header and nothing below it."""
        self._scroll_off.clear()

    def go_back(self):
        if len(self.nav_stack) > 1:
            self.nav_stack.pop()
            self._reset_scroll()
            self._bump_epoch()
            # Stale-while-revalidate: refresh Home on return (watched/resume
            # state may have changed) while showing the cached view meanwhile.
            if self.route.get("kind") == "home":
                self._load_route(self.route)
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

    def display_item(self, server_uuid, item_id):
        """Open an item's page because a remote asked us to (Jellyfin's
        DisplayContent — "show me this" from a phone or web client).

        This is the browsable counterpart to the legacy kiosk mirror: the
        remote picks the page, then its arrows drive the same spatial
        navigation the keyboard uses.

        Two things it deliberately does NOT do. It never starts playback —
        jellyfin-web emits DisplayContent as you *browse* on the phone, so a
        cast track has to open its album, not play it. And it never
        interrupts playback for the same reason: browsing on the phone while
        something plays here would otherwise stop the video. The page is
        simply waiting when playback ends."""
        if self._locked:
            return       # a remote must not browse past the PIN gate
        if server_uuid and server_uuid != self.server:
            self.server = server_uuid
        ep = self._epoch

        def work():
            return self.source.get_item(server_uuid or self.server, item_id)

        def done(item):
            if not item:
                return
            self._display_route(item)
            if self._minimized or self._browsing:
                # Idle or already browsing: bring the page forward. A cast
                # has to be able to wake a minimized client.
                self.enter_browse()
                if self.controller is not None:
                    self._safe(lambda c: c.raise_window())
        self.run_async(work, done, ep)

    def _display_route(self, item):
        """Navigate to an item's *page*. Same dispatch as a click, except
        that types a click would play resolve to the page they belong to."""
        if item.get("Type") == "Audio":
            # _open_item would PLAY a track. Open its album instead, or do
            # nothing if it has none — a browse gesture must never start
            # playback.
            album = item.get("AlbumId")
            if album:
                self.navigate({
                    "kind": "album",
                    "server": self.route.get("server") or self.server,
                    "item_id": album,
                    "title": item.get("Album") or ""})
            else:
                log.debug("DisplayContent for a track with no album; ignoring")
            return
        self._open_item(item)

    def on_nav_command(self, name):
        """Remote menu commands that map onto real pages here (GoHome /
        GoToSettings). Returns True when handled; the OSD menu has no such
        pages, so for every other path both still just open the menu."""
        if name == "settings":
            self.open_settings()
            return True
        if name == "home":
            self.navigate({"kind": "home", "server": self.server}, reset=True)
            return True
        return False

    def on_back(self):
        """BACK / ESC from a remote or the keyboard. Returns True when it
        consumed the press, so the player can fall back to its own handling
        (leaving fullscreen) at the root of the stack."""
        if self._dialog is not None:
            self._close_dialog()
            return True
        if self._menu is not None:
            self._close_menu()
            return True
        if len(self.nav_stack) > 1:
            self.go_back()
            return True
        return False

    def _on_nav_mode(self, active):
        """Renderer 'nav' event: keyboard/remote engaged or the mouse
        took over. Repaint so modality-dependent chrome (carousel
        arrows) follows."""
        if active != self._nav_mode:
            self._nav_mode = active
            self.invalidate()

    def _bump_epoch(self):
        with self._lock:
            self._epoch += 1

    # -------------------------------------------------------- async model

    def invalidate(self):
        if self.app is not None:
            self.app.invalidate()

    def run_async(self, work, on_done, epoch, on_error=None):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if the epoch still matches (the user hasn't navigated away). ``on_done``
        mutates state under the lock, then the loop is woken to rebuild.

        ``on_error(exc)`` runs the same way when ``work()`` raises. Without
        one a failure only logs, which left the route's data at None and the
        view spinning forever — an unreachable server looked like a hang."""
        def task():
            try:
                result = work()
            except Exception as exc:
                log.warning("async work failed", exc_info=True)
                if on_error is None:
                    return
                with self._lock:
                    if epoch != self._epoch:
                        return
                    try:
                        on_error(exc)
                    except Exception:
                        log.warning("async on_error failed", exc_info=True)
                self.invalidate()
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

    def _route_async(self, route, work, on_done, ep):
        """run_async for a route's data, recording a failure on the route so
        the view can say so and offer a retry instead of spinning."""
        def failed(exc):
            route["_error"] = _("Failed to load. Check the connection.")
            # Paging guards must not survive the failure or the view stops
            # requesting anything for the rest of the session.
            route.pop("_loading", None)
            log.info("route %r failed to load: %s", route.get("kind"), exc)
            self._offline_fallback(route)
        self.run_async(work, on_done, ep, on_error=failed)

    def _offline_fallback(self, route):
        """A failed *home* load with downloads present drops to the offline
        library, as the Tk browser does — otherwise the first thing a user
        sees with the server down is an error where their downloads are."""
        if route.get("kind") != "home" or self._offline:
            return
        if self.controller is None:
            return
        try:
            source = self.controller.offline_source()
        except Exception:
            log.debug("offline fallback failed", exc_info=True)
            return
        if source is not None:
            log.info("server unreachable; falling back to the downloads")
            self.set_source(source)

    def _retry_route(self, route):
        route.pop("_error", None)
        route.pop("_data", None)
        route.pop("_items", None)
        route.pop("_loading", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _load_route(self, route):
        kind = route["kind"]
        if self.server is None:
            return
        route.pop("_error", None)
        ep = self._epoch
        if kind == "home":
            def work():
                server = route.get("server") or self.server
                libs = self.source.get_libraries(server)
                rows = self.source.get_home_rows(server, libs)
                return {"libraries": libs, "rows": rows}
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "grid":
            srv = route.get("server") or self.server
            parent = route["parent_id"]
            _n, sort_by, sort_order = SORTS[route.get("_sort", 0)]
            filters = route.get("_filters") or {}

            collections = bool(route.get("_collections"))

            def work():
                if collections:
                    # Collections are server-wide and recursive (a BoxSet
                    # can gather items from several libraries), so this is a
                    # different query, not a filter on the library.
                    items, total = self.source.get_movie_collections(
                        srv, sort_by=sort_by, sort_order=sort_order,
                        filters=filters)
                else:
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
                # The toggle only makes sense on a movies library, and only
                # when the source can answer it (the offline catalog can't).
                route["_collection_capable"] = (
                    route.get("collection_type") == "movies"
                    and hasattr(self.source, "get_movie_collections"))
            self._route_async(route, work, done, ep)
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
                trailers = []
                if (item or {}).get("Type") in ("Movie", "Series"):
                    try:
                        trailers = self.source.get_trailers(srv, iid) or []
                    except Exception:
                        pass  # older servers / no trailers: just no button
                return {"item": item, "similar": similar,
                        "trailers": trailers}
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "series":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return {
                    "item": self.source.get_item(srv, iid),
                    "seasons": self.source.get_seasons(srv, iid),
                }
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "season":
            srv = route.get("server") or self.server

            def work():
                return {
                    "episodes": self.source.get_episodes(
                        srv, route.get("series_id"), route["item_id"]),
                    "seasons": self.source.get_seasons(
                        srv, route.get("series_id")),
                }
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
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
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "music":
            def done(res):
                items, total = res
                route["_data"], route["_total"] = items, total
                route["_loading"] = False
            self._route_async(route, self._music_page(route, 0), done, ep)
        elif kind == "album":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return {"item": self.source.get_item(srv, iid),
                        "tracks": self.source.get_album_tracks(srv, iid)}
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
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
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
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
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "playlist":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                return self.source.get_playlist_items(srv, iid)
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)
        elif kind == "person":
            srv = route.get("server") or self.server

            def work():
                return self.source.get_person_items(srv, route["person_id"])

            def done(res):
                route["_items"], route["_total"] = res
            self._route_async(route, work, done, ep)
        elif kind == "playlist_edit":
            srv = route.get("server") or self.server
            iid = route["item_id"]

            def work():
                meta = {}
                try:
                    meta = self.source.get_playlist(srv, iid) or {}
                except Exception:
                    pass
                return self.source.get_playlist_items(srv, iid), meta

            def done(res):
                items, meta = res
                route["_items"] = items
                # Read the *server's* visibility before offering the toggle;
                # assuming private meant the first click could flip a public
                # playlist's visibility based on a value we never read.
                if "OpenAccess" in meta:
                    route["_public"] = bool(meta.get("OpenAccess"))
                    route["_public_known"] = True
            self._route_async(route, work, done, ep)
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
            self._route_async(route, work, lambda d: route.__setitem__("_data", d), ep)

    # -------------------------------------------------------- tile helpers

    def _subtitle(self, item):
        if item.get("_subtitle") is not None:
            return item["_subtitle"]      # pseudo-items (chapters)
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            if s is not None and e is not None:
                return "S%dE%d" % (s, e)
        return str(item.get("ProductionYear") or "")

    # A thumbnail fetch that fails transiently is retried on a later
    # repaint, but not immediately: a server that is down or slow would
    # otherwise get a fresh burst on every scroll frame. Attempts are
    # capped so a permanently broken URL settles instead of retrying for
    # the life of the session.
    IMG_RETRY_BACKOFF = 5.0    # seconds, doubled per attempt
    IMG_MAX_ATTEMPTS = 4

    def _request_image(self, key, url, box):
        """Return a cached decoded PIL image for ``key`` (poster/backdrop/…),
        or None while it loads — requesting it once from the thumbnail pool.
        The next repaint (woken by the pool's notify) picks it up."""
        img = self._posters.get(key)
        if img is not None or self.thumbs is None or not url:
            return img
        if key in self._requested:
            return None
        retry_at = self._img_retry.get(key)
        if retry_at is not None and time.time() < retry_at[1]:
            return None            # cooling off after a failed attempt
        self._requested.add(key)
        self.thumbs.request(key, url, box,
                            lambda im, k=key: self._image_done(k, im))
        return None

    def _image_done(self, key, image):
        """Thumbnail delivery, on the loop thread.

        ``image`` is None when the fetch failed. Releasing the dedup marker
        is the whole point: it used to be set before dispatch and never
        cleared, so one timed-out poster stayed blank for the rest of the
        process — no navigation, scroll or re-open would ask again. A
        permanent miss (the server says there's no such image) keeps the
        marker, so it isn't asked for again either."""
        if image is not None:
            self._posters[key] = image
            self._img_retry.pop(key, None)
            return
        if self.thumbs is not None and self.thumbs.is_gone(key):
            return                 # no such image; stop asking
        attempts = self._img_retry.get(key, (0, 0.0))[0] + 1
        if attempts > self.IMG_MAX_ATTEMPTS:
            return                 # give up, keeping the marker set
        self._img_retry[key] = (
            attempts,
            time.time() + self.IMG_RETRY_BACKOFF * (2 ** (attempts - 1)))
        self._requested.discard(key)

    def _poster_for(self, item, geom, image_type="Primary"):
        """Return (PIL image or None, cache tag). Requests the poster once
        if absent; the strip recomposites when it arrives (tag changes)."""
        w, h = geom.tile_w, geom.tile_h
        if "_image_url" in item:
            # A pseudo-item (a chapter) carrying its own spec+url: chapter
            # art is indexed, so it isn't addressable through image_spec.
            spec, url = item.get("_image_spec"), item.get("_image_url")
            if not spec or not url:
                return None, ""
            key = make_key(spec[0], spec[1], spec[2], w, h)
            return self._request_image(key, url, (w, h)), key
        spec = self.source.image_spec(item, image_type, geom.tile_w)
        if not spec or self.server is None:
            return None, ""
        item_id, itype, itag = spec
        w, h = geom.tile_w, geom.tile_h
        key = make_key(item_id, itype, itag, w, h)
        url = self.source.image_url(self.server, item_id, itype, itag,
                                    w, h, fill=True)
        return self._request_image(key, url, (w, h)), key

    def _art_cell(self, item, size=28):
        """Small square album-art bitmap for a table cell (track lists);
        a placeholder box while it loads or when the item has none.
        Each cell is its own overlay, so only use in virtualized or
        short tables (the 63-overlay budget is shared)."""
        spec = self.source.image_spec(item, "Primary", size)
        if spec and self.server is not None:
            item_id, itype, itag = spec
            key = make_key(item_id, itype, itag, size, size)
            url = self.source.image_url(self.server, item_id, itype,
                                        itag, size, size, fill=True)
            img = self._request_image(key, url, (size, size))
            if img is not None:
                b = self.strips.bitmap(key, img)
                return Image(b["src"], b["iw"], b["ih"])
        return self._art_placeholder(size)

    @staticmethod
    def _art_placeholder(size=28):
        """Same-sized stand-in for an art cell — while it loads, when the
        item has none, and for rows outside the virtual window (which must
        not composite: see _track_list)."""
        return Box(w=size, h=size, bg=theme.PLACEHOLDER_BG, radius=4)

    def _is_watched(self, item):
        ud = item.get("UserData") or {}
        if ud.get("Played"):
            return True
        if item.get("Type") in ("Series", "Season"):
            # `or 0` would read a MISSING count as zero-unplayed, i.e.
            # fully watched — so a Series DTO without UserData (search
            # results, the synthesized season fallback) showed a watched
            # check, and the toggle computed `not watched` and marked an
            # unwatched show unwatched: a no-op that reads as a dead button.
            return ud.get("UnplayedItemCount") == 0
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

    # A banner is a wide crop, not a 16:9 frame — two-thirds the height of
    # the equivalent 16:9 box, which is roughly 2.4:1.
    BANNER_RATIO = 9 / 16 * 2 / 3

    def _banner_box(self, width):
        bw = min(width - 2 * self.CONTENT_PAD, 1100)
        return bw, int(bw * self.BANNER_RATIO)

    def _backdrop_node(self, item, box, node_id, title=None, meta=None,
                       context=None):
        """A backdrop banner for detail/series headers.

        With ``title`` the heading is *baked into the bitmap* over a bottom
        gradient, like the Tk browser did — text drawn as ASS would sit
        under the image (bitmaps composite above all script ASS), and the
        occlude punch would show the window background rather than the
        artwork. Returns a placeholder Box while the art loads or if the
        item has none, in which case the caller still draws its own heading."""
        spec = None
        if self.server is not None:
            spec = self.source.backdrop_spec(item)
        if spec:
            owner_id, tag = spec
            key = make_key(owner_id, "Backdrop", tag, box[0], box[1])
            if title:
                key += "|" + make_key(title, meta or "", context or "",
                                      box[0], box[1])
            url = self.source.backdrop_url(self.server, item, width=box[0],
                                           height=box[1], fill=True)
            # Request at the *source* aspect and crop to the banner below, so
            # a shallow banner doesn't ask the server for a squashed image.
            img = self._request_image(key, url, (box[0], box[0]))
            if img is not None:
                b = self.strips.bitmap(key, self._compose_banner(
                    img, box, title, meta, context))
                return Image(b["src"], b["iw"], b["ih"], id=node_id)
        return Box(w=box[0], h=box[1], bg=theme.PLACEHOLDER_BG, radius=6,
                   id=node_id)

    @staticmethod
    def _heading_for(item):
        """``(title, context)`` for a detail heading.

        An episode's series and SxEy go on their own line above the episode
        title rather than being joined into one string — joined, a name of
        any length ran off the end of the banner and was cut mid-word
        ("Clannad · S1E1 · On the Hillside Pa")."""
        title = item.get("Name", "")
        if item.get("Type") != "Episode":
            return title, ""
        s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
        se = "S%sE%s" % (s, e) if s is not None and e is not None else ""
        context = "   ·   ".join(
            p for p in (item.get("SeriesName"), se) if p)
        return title, context

    @staticmethod
    def _wrap_pil(draw, text, font, max_w, max_lines=2):
        """Word-wrap ``text`` to ``max_lines``, ellipsizing the last line.
        Falls back to breaking mid-word for a single word too long to fit."""
        words, lines, cur = text.split(), [], ""
        for word in words:
            trial = (cur + " " + word).strip()
            if not cur or draw.textlength(trial, font=font) <= max_w:
                cur = trial
                continue
            lines.append(cur)
            cur = word
            if len(lines) == max_lines:
                break
        if cur and len(lines) < max_lines:
            lines.append(cur)
        if not lines:
            return [text]
        # The last line absorbs whatever didn't fit, ellipsized.
        consumed = len(" ".join(lines).split())
        if consumed < len(words) or draw.textlength(
                lines[-1], font=font) > max_w:
            last = lines[-1]
            if consumed < len(words):
                last = " ".join([last] + words[consumed:])
            while last and draw.textlength(last + "…", font=font) > max_w:
                last = last[:-1]
            lines[-1] = last.rstrip() + "…"
        return lines

    @classmethod
    def _compose_banner(cls, image, box, title=None, meta=None, context=None):
        """Crop ``image`` to the banner box and bake the heading over a
        bottom-up dark gradient.

        Stacked bottom-up: meta, title (wrapped to two lines), then the
        context line above it. Text is sized off the banner height and
        stays small enough that a long episode title reads in full."""
        from PIL import ImageDraw

        from ..display_mirror import (_apply_dark_gradient, _pil_font,
                                      _scale_to_cover)

        w, h = box
        canvas = _scale_to_cover(image.convert("RGBA"), w, h)
        if not title:
            return canvas
        canvas = _apply_dark_gradient(canvas, height_fraction=0.7,
                                      max_alpha=215)
        draw = ImageDraw.Draw(canvas)
        margin = max(18, w // 40)
        avail = w - 2 * margin
        # Smaller than it was: the heading has up to three stacked lines to
        # fit inside the gradient now, not one.
        size = max(20, min(34, h // 6))
        y = h - margin
        if meta:
            f = _pil_font(int(size * 0.6), text=meta)
            asc, desc = f.getmetrics()
            draw.text((margin, y - asc - desc), meta, font=f,
                      fill=(200, 200, 200, 255))
            y -= asc + desc + 6
        f = _pil_font(size, bold=True, text=title)
        asc, desc = f.getmetrics()
        for line in reversed(cls._wrap_pil(draw, title, f, avail)):
            draw.text((margin, y - asc - desc), line, font=f,
                      fill=(255, 255, 255, 255))
            y -= asc + desc + 2
        if context:
            f = _pil_font(int(size * 0.62), text=context)
            asc, desc = f.getmetrics()
            line = cls._wrap_pil(draw, context, f, avail, max_lines=1)[0]
            draw.text((margin, y - asc - desc + 2), line, font=f,
                      fill=(215, 215, 215, 255))
        return canvas

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

    def _image_map(self, items, prefix, geom=None, image_type="Primary",
                   on_click=None):
        geom = geom or self.geom
        tiles = [self._tile(it, geom, image_type) for it in items]
        s = self.strips.strip(tiles, geom)
        regions = []
        act = on_click or self._open_item
        for r, it in zip(s["regions"], items):
            regions.append(dict(
                r,
                id="%s-%s" % (prefix, r["key"]),
                on_click=(lambda i=it: act(i)),
                on_context=(lambda x, y, i=it: self._open_tile_menu(i, x, y)),
            ))
        return ImageMap(s["src"], s["iw"], s["ih"], regions=regions)

    # ------------------------------------------------------ tile context menu

    def _open_tile_menu(self, item, x, y):
        # Nothing on offer for this type (a cast member): no menu at all,
        # rather than an empty one.
        if not self._tile_menu_entries(item):
            return
        self._menu = {"item": item,
                      "server": self.route.get("server") or self.server,
                      "x": x, "y": y}
        self.invalidate()

    def _close_menu(self):
        self._menu = None
        self.invalidate()

    # Types the tile menu offers each action for. Every entry used to be
    # shown for every item, so right-clicking a cast member offered to
    # play, download and mark a Person watched.
    MENU_PLAYABLE = PLAYABLE_TYPES | {"Audio", "MusicAlbum", "MusicArtist",
                                      "Series", "Season", "Playlist"}
    MENU_WATCHED = PLAYABLE_TYPES | {"Series", "Season"}
    MENU_FAVORITE = MENU_PLAYABLE | {"MusicAlbum", "MusicArtist"}
    MENU_ADD_TO = PLAYABLE_TYPES | {"Audio"}
    MENU_DOWNLOAD = PLAYABLE_TYPES | {"Audio", "Series", "Season", "Playlist"}

    def _tile_menu_entries(self, item):
        """``[(label, icon, action-key)]`` for this item's type."""
        t = item.get("Type")
        ud = item.get("UserData") or {}
        watched = self._is_watched(item)
        fav = bool(ud.get("IsFavorite"))
        out = []
        if t in self.MENU_PLAYABLE:
            out.append((_("Play"), "play_arrow", "play"))
            out.append((_("Add to Queue"), "playlist_add", "queue"))
        if t in self.MENU_WATCHED:
            out.append((_("Mark Unwatched") if watched
                        else _("Mark Watched"), "check", "watched"))
        if t in self.MENU_FAVORITE:
            out.append((_("Remove from Favorites") if fav
                        else _("Add to Favorites"), "favorite", "favorite"))
        if t in self.MENU_ADD_TO and not self._offline:
            out.append((_("Add to Playlist"), "queue_music", "addto"))
        if t in self.MENU_DOWNLOAD and not self._offline:
            out.append((_("Download"), "file_download", "download"))
        return out

    def _tile_menu_node(self):
        m = self._menu
        entries = self._tile_menu_entries(m["item"])
        if not entries:
            return None
        return Menu("tilemenu", [e[0] for e in entries], m["x"], m["y"],
                    icons=[e[1] for e in entries],
                    on_select=self._menu_action, on_dismiss=self._close_menu)

    def _menu_action(self, index, value):
        m = self._menu
        if m is None:
            return
        item, server = m["item"], m["server"]
        entries = self._tile_menu_entries(item)
        if not 0 <= index < len(entries):
            return self._close_menu()
        action = entries[index][2]
        if action == "play":
            self._menu_play(item, server)
        elif action == "queue":
            self._menu_queue(item, server)
        elif action == "watched":
            self._act_watched(item, server)
        elif action == "favorite":
            self._act_favorite(item, server)
        elif action == "addto":
            self._close_menu()
            self._open_add_to(item)
            return
        elif action == "download":
            self._close_menu()
            self._open_download(item)
            return
        self._close_menu()

    def _menu_queue(self, item, server):
        """Append to the playing queue. A music container is resolved to its
        tracks first — queueing the container id itself is meaningless."""
        ep = self._epoch

        def work():
            return self._resolve_play_ids(item, server)

        def done(ids):
            if ids:
                self._queue_items(ids, server)
        self.run_async(work, done, ep)

    def _resolve_play_ids(self, item, server):
        """The item ids "Play"/"Add to Queue" should act on.

        A music container (album/artist/playlist/series) is not itself a
        playable item — queueing or playing its own id does nothing, which
        is why Play on an album tile used to just navigate. Runs off the
        loop thread: these hit the server."""
        t, iid = item.get("Type"), item.get("Id")
        if not iid:
            return []
        try:
            if t == "MusicAlbum":
                return [i.get("Id")
                        for i in self.source.get_album_tracks(server, iid)]
            if t == "MusicArtist":
                return [i.get("Id")
                        for i in self.source.get_artist_songs(server, iid)]
            if t == "Playlist":
                return [i.get("Id") for i in
                        self.source.get_playlist_items(server, iid)
                        if i.get("Type") in PLAYLIST_SUPPORTED_TYPES]
            if t in ("Series", "Season"):
                return [i.get("Id") for i in
                        self.source.get_series_queue(server, iid)]
        except Exception:
            log.warning("could not resolve %s for playback", t, exc_info=True)
            return []
        return [iid]

    def _menu_play(self, item, server):
        t = item.get("Type")
        if t == "Audio":
            self._play_list([item.get("Id")], server, audio=True)
            return
        if t in PLAYABLE_TYPES:
            self._play(item, server)
            return
        # A container: resolve it to its items and play those, rather than
        # navigating (a "Play" that browses instead is just a lie).
        ep = self._epoch
        audio = t in ("MusicAlbum", "MusicArtist")

        def work():
            return self._resolve_play_ids(item, server)

        def done(ids):
            if ids:
                self._play_list(ids, server, 0, audio=audio)
            else:
                self._open_item(item)
        self.run_async(work, done, ep)

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

    # Row height of every track table (album, playlist, queue, songs).
    TRACK_ROW_H = 34

    # Square page-arrow buttons floating over the carousel's edges.
    ARROW_W = 38
    # Slack inside a scroll viewport so a tile's hover ring — which the
    # renderer draws 2px OUTSIDE the hit rect, and clips to the viewport —
    # isn't shaved off against the container edge. Without it the top of the
    # ring vanished under the row heading above.
    RING_PAD = 5

    def _tile_row(self, title, items, row_id, geom=None, image_type="Primary",
                  bleed=False, on_click=None):
        """A titled horizontal carousel.

        ``bleed`` runs the strip edge-to-edge so the page arrows sit flush
        against the window's left and right sides; the title is indented to
        line up with the content instead."""
        geom = geom or self.geom
        heading = Text(title, size=24, bold=True)
        if bleed:
            # The strip runs edge to edge; indent the heading to line up with
            # the first tile instead.
            heading = Row([Spacer(w=self.CONTENT_PAD), heading])
        return Column(
            [
                heading,
                self._hscroll_row(
                    self._image_map(items, row_id, geom, image_type,
                                    on_click=on_click),
                    row_id, geom.strip_h + 2 * self.RING_PAD,
                    len(items), geom, bleed),
            ],
            gap=10,
        )

    def _hscroll_row(self, content, row_id, h, count, geom, bleed=False):
        """An HScroll with ◀ ▶ page buttons floating over its edges.

        The arrows genuinely overlay the poster strip: a Stack layers them on
        top, and ``occlude=True`` punches their rect out of the strip bitmap
        below so the ASS button draws in the hole (bitmaps otherwise composite
        above all script ASS — GUIDE §6). They hold-repeat while pressed, and
        are omitted when the row doesn't overflow.

        The strip is inset by RING_PAD so a tile's hover ring has room inside
        the viewport; the renderer clips it to the container, and without the
        inset its top edge was shaved off under the heading above."""
        scroll = HScroll(Box([content], pad=self.RING_PAD),
                         id=row_id, h=h, flex=1)
        avail = (self._size[0] if self._size else 1280)
        if not bleed:
            avail -= 2 * self.CONTENT_PAD
        content_w = count * geom.tile_w + max(0, count - 1) * geom.gap
        if content_w <= avail or self._nav_mode:
            # keyboard/remote navigation auto-scrolls the row as focus
            # moves — pointer paging arrows would only cover artwork
            return Row([scroll], h=h)

        def arrow(icon, node_id, direction, anchor):
            # Square, and small enough to cover as little artwork as
            # possible — the occlusion punch reads as a notch, so a tall
            # slab looked wrong. Flex spacers centre the glyph (Box only
            # centres on its cross axis).
            return Box([Spacer(flex=1), Icon(icon, 22), Spacer(flex=1)],
                       id=node_id, w=self.ARROW_W, h=self.ARROW_W,
                       align="center", direction="row",
                       bg=theme.BUTTON_BG, alpha=230,
                       hover={"fill": theme.BUTTON_ACTIVE}, radius=6,
                       anchor=anchor, dx=(self.RING_PAD if anchor == "w"
                                          else -self.RING_PAD),
                       # "w"/"e" centre on the whole strip, which includes the
                       # caption block under the tile; shift up by half of it
                       # so the arrow sits on the artwork.
                       dy=-(geom.strip_h - geom.tile_h) / 2,
                       occlude=True, repeat=True,
                       on_click=lambda: self._page_row(row_id, direction))

        return Stack([
            scroll,
            arrow("chevron_left", row_id + "-pl", -1, "w"),
            arrow("chevron_right", row_id + "-pr", 1, "e"),
        ], h=h)

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
            # collection_type rides along so the grid knows whether to offer
            # the Collections toggle (movies libraries only).
            self.navigate(dict(base, kind="grid", parent_id=item.get("Id"),
                               collection_type=item.get("CollectionType")))
        else:
            self.status = _("Selected: %s") % item.get("Name", "")
            self.invalidate()

    def _set_renderer_active(self, active):
        """Suspend/resume the in-mpv renderer. Pushing an empty scene is not
        enough to yield to the OSC — the renderer's forced mouse/wheel
        bindings keep swallowing the clicks until it is suspended."""
        if self.app is not None and hasattr(self.app, "set_active"):
            try:
                self.app.set_active(active)
            except Exception:
                log.debug("set_active failed", exc_info=True)

    def set_app(self, app):
        """Point the browser at a (possibly fresh) MpvtkApp and wire the
        callbacks. mpv re-creation attaches a brand-new app per handle —
        without re-wiring here its nav/HUD events would go nowhere (the
        old app object kept the handlers)."""
        self.app = app
        # a fresh renderer has no HUD state; drop ours so build() doesn't
        # keep pushing a HUD scene at an idle renderer
        self._hud_shown = False
        self._hud_scrub = None
        # True when the scrub gesture itself paused playback (restored
        # on commit/cancel; an explicit user pause stays paused)
        self._hud_scrub_paused = False
        self._hud_menu = None
        # node the open settings/SyncPlay menu hangs off (see hud.py)
        self._hud_menu_anchor = "hud-settings"
        # clock shows remaining time instead of total (click toggles)
        self._hud_tc_remaining = False
        # pointer resting on the seek bar: hovered position in seconds
        # (drives the preview bubble; scrub takes precedence)
        self._hud_hover = None
        if app is None:
            return
        if hasattr(app, "on_nav"):
            app.on_nav = self._on_nav_mode
        if hasattr(app, "on_hud"):
            app.on_hud = self._on_hud
        if hasattr(app, "on_hud_skip"):
            app.on_hud_skip = self._on_hud_skip

    def reassert_window_state(self):
        """Re-assert window ownership on a FRESH renderer (which starts
        active): browse takes the window back; a video in flight
        re-enters attached-but-idle HUD mode; otherwise get fully out
        of the way (lua OSC / minimized)."""
        if self._browsing:
            self._set_renderer_active(True)
        elif self._use_hud() and self._hud_state is not None:
            try:
                self._engage_hud()
            except Exception:
                log.debug("set_hud failed", exc_info=True)
        else:
            self._set_renderer_active(False)

    def _use_hud(self):
        """Whether yielding to video keeps the renderer attached-but-idle
        for the playback HUD (osc_style "mpvtk") instead of getting fully
        out of the way, which is what the lua OSCs need."""
        c = self.controller
        return (self.app is not None and hasattr(self.app, "set_hud")
                and c is not None and getattr(c, "use_hud", None) is not None
                and c.use_hud())

    def _engage_hud(self):
        """set_hud(True) with the controller's keyboard policy attached
        (grab arrows vs. wake-key-only; see hud_grab_keys)."""
        opts = None
        get = getattr(self.controller, "hud_key_opts", None)
        if get is not None:
            try:
                opts = get()
            except Exception:
                opts = None
        self.app.set_hud(True, opts)

    def _hud_scrub_change(self, v):
        if self._hud_scrub is None:
            # gesture start: pause so the position is inspectable;
            # commit/cancel restores playback if WE paused it
            self._hud_scrub_paused = not (self._hud_state or {}).get(
                "paused")
            if self._hud_scrub_paused:
                self._ctl(lambda c: c.set_paused(True))
        self._hud_scrub = float(v)
        self.invalidate()

    def _hud_scrub_done(self):
        self._hud_scrub = None
        if self._hud_scrub_paused:
            self._hud_scrub_paused = False
            self._ctl(lambda c: c.set_paused(False))
        self.invalidate()

    def _hud_scrub_commit(self, v):
        self._ctl(lambda c: c.seek(float(v)))
        self._hud_scrub_done()

    def _hud_scrub_cancel(self):
        self._hud_scrub_done()

    def _on_hud_skip(self):
        """The renderer's standalone idle skip button was activated."""
        self._ctl(lambda c: c.hud_action("skip-segment"))

    def _hud_hover_move(self, v):
        self._hud_hover = float(v)
        self.invalidate()

    def _hud_hover_end(self):
        self._hud_hover = None
        self.invalidate()

    def open_hud_menu(self):
        """Summon the HUD with the gear menu open (the player routes
        the kb_menu key here during playback, replacing the OSD menu
        under the in-window OSC). Pressing it again closes the menu.
        Returns True when handled."""
        if not self._use_hud() or self._hud_state is None:
            return False
        try:
            if self._hud_shown and self._hud_menu:
                self._hud_menu = None       # kb_menu toggles
                self.invalidate()
                return True
            self._engage_hud()              # no-op when already engaged
            self.app.summon_hud()
            self._hud_menu = "root"
            self._hud_menu_anchor = "hud-settings"
            self.invalidate()
            return True
        except Exception:
            log.debug("open_hud_menu failed", exc_info=True)
            return False

    def _on_hud(self, active):
        """Renderer summoned / auto-hid the playback HUD (loop thread)."""
        self._hud_shown = bool(active)
        if self._hud_scrub_paused:
            self._hud_scrub_done()  # resumes playback the scrub paused
        self._hud_scrub = None
        if not active:
            # keep a menu opened in the same beat as a summon
            # (open_hud_menu sets it right before the hud event lands)
            self._hud_menu = None
        self._hud_hover = None
        if getattr(self.controller, "hud_sub_margin", None) is not None:
            # raise bottom subtitles clear of the bar while it shows
            try:
                self.controller.hud_sub_margin(bool(active))
            except Exception:
                log.debug("hud_sub_margin failed", exc_info=True)
        if active:
            # a fresh position snapshot before the bar first paints, then
            # the shared 1s ticker keeps its clock moving
            try:
                self.controller.refresh_playstate()
            except Exception:
                log.debug("playstate refresh failed", exc_info=True)
            self._start_np_ticker()
        self.invalidate()

    def _yield(self):
        self._browsing = False
        if self.controller is not None:
            self.controller.on_browse_leave()
        if self._use_hud():
            # keep the renderer attached: blank scene + summon bindings
            try:
                self._engage_hud()
            except Exception:
                log.debug("set_hud failed", exc_info=True)
        else:
            self._set_renderer_active(False)
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

    def _play_list(self, ids, server, start_index=0, audio=False,
                   items=None):
        """Play a whole list from ``start_index`` (album/playlist/song).

        ``items`` (the DTOs behind ``ids``) supplies the resume offset for
        the entry actually being started, as the Tk browser does —
        without it, clicking a half-watched entry restarted it from zero.

        The chosen entry is re-located by id after dropping empty ones:
        filtering first and trusting the caller's index shifted the queue
        out from under the entry that was clicked."""
        start_id = ids[start_index] if 0 <= start_index < len(ids) else None
        offset = None
        if items is not None and 0 <= start_index < len(items):
            offset = ((items[start_index].get("UserData") or {})
                      .get("PlaybackPositionTicks")) or None
        ids = [i for i in ids if i]
        if not ids:
            return
        try:
            pos = ids.index(start_id)
        except ValueError:
            pos = 0
        self._start(audio=audio)
        if self.controller is not None:
            self.controller.play_list(ids, server, pos, offset_ticks=offset)

    # ------------------------------------------------- browse <-> playback

    def start_background_work(self):
        """Kick off the pollers that keep the chrome honest (download status)
        and the one-shot startup update check. Called once the browser is
        live; separate from __init__ so tests don't spawn threads."""
        self._poll_download_status()
        if self.controller is not None:
            self._pool.submit(lambda: self._safe(lambda c: c.check_updates()))

    def enter_browse(self):
        """Show the browser: take the window + hide the OSC, then render.
        mpvtk-active yes also drops the renderer out of HUD mode."""
        self._browsing = True
        self._minimized = False
        self._hud_shown = False
        if self.controller is not None:
            self.controller.on_browse_enter()
        self._set_renderer_active(True)
        self.invalidate()

    def minimize(self):
        """Release the window entirely — the app keeps running in the tray as
        a cast target. This is the player's "playback_abort yes, force_window
        no" state; there is no separate window to hide, so minimizing *is*
        dropping force_window with nothing playing."""
        self._minimized = True
        self._browsing = False
        self._hud_shown = False
        self._set_renderer_active(False)
        if self.controller is not None:
            self.controller.on_minimize()

    @property
    def minimized(self):
        return self._minimized

    def on_playstate(self, state):
        """Registered as playerManager.on_playstate. Drives browse/playback
        state and the now-playing bar. Audio keeps the browser visible (bar +
        browsing); video stays yielded to the picture + OSC."""
        if not state or state.get("stopped"):
            self._now_playing = None
            self._hud_state = None
            if self._hud_shown and getattr(
                    self.controller, "hud_sub_margin", None) is not None:
                # playback ended with the HUD up: the renderer clears
                # without an on_hud(False), so restore the margin here
                try:
                    self.controller.hud_sub_margin(False)
                except Exception:
                    log.debug("hud_sub_margin failed", exc_info=True)
            self._hud_shown = False
            self._hud_menu = None
            if self._minimized:
                # Cast finished and the library was never open: drop back to
                # the windowless state rather than popping the browser up on
                # a screen the user wasn't looking at.
                self.minimize()
            else:
                # Unconditionally, even if we never left browse mode: stopping
                # music happens *while* browsing, and whatever stopped it may
                # have dropped force_window and taken the library's window
                # with it. enter_browse() re-asserts the browse window.
                self.enter_browse()
            return
        if state.get("is_audio"):
            self._now_playing = state
            if not self._browsing:
                self.enter_browse()   # audio: stay in browse, show the bar
            else:
                self.invalidate()
            self._start_np_ticker()
        else:
            self._now_playing = None
            self._hud_state = state   # feeds the playback HUD bar
            if self._browsing:
                self._yield()         # video: yield the window + the OSC
            else:
                self.invalidate()     # HUD/bar repaint (clock, pause icon)
            if not self._browsing and self._use_hud():
                try:
                    # Idempotent HUD-mode engage: covers playback that
                    # starts while minimized/already-yielded and a fresh
                    # renderer after mpv re-creation (a plain _yield only
                    # happens on the browsing -> video transition).
                    self._engage_hud()
                    # ... and keep the idle skip overlay in sync with the
                    # live skippable segment (the player pushes a
                    # playstate the moment one starts/ends).
                    self.app.set_hud_skip(state.get("skip_label") or "")
                except Exception:
                    log.debug("hud sync failed", exc_info=True)

    def _start_np_ticker(self):
        """Keep the now-playing bar's clock at 1s.

        The timeline thread only pushes state every 5s (it also talks to the
        server, so speeding it up is not free). While the bar is on screen we
        ask the player for a fresh snapshot once a second instead; the thread
        exits as soon as the bar goes away."""
        if self.controller is None or self._np_thread is not None:
            return

        def tick():
            try:
                while not self._np_stop.wait(1.0):
                    bar = self._now_playing is not None and self._browsing
                    if not bar and not self._hud_shown:
                        break
                    try:
                        self.controller.refresh_playstate()
                    except Exception:
                        log.debug("playstate refresh failed", exc_info=True)
            finally:
                self._np_thread = None

        self._np_thread = threading.Thread(target=tick, daemon=True,
                                           name="mpvtk-np-tick")
        self._np_thread.start()

    def set_source(self, source, server_uuid=None):
        """Swap in a live data source once servers connect (the browser opens
        immediately on a spinner and populates when the network settles).

        A catalog-backed source raises the offline banner: every path that
        can land offline goes through here, so deriving the banner from the
        source is what keeps the two from drifting apart."""
        from .repository import OfflineLibrarySource

        self._offline = isinstance(source, OfflineLibrarySource)
        self._locked = False
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
        self._size = size
        # One synchronous read per frame: the renderer's live scroll offsets,
        # which virtualization windows against (see _offset).
        self._live_offsets = None
        if self.app is not None and hasattr(self.app, "scroll_offsets"):
            try:
                self._live_offsets = self.app.scroll_offsets()
            except Exception:
                log.debug("scroll_offsets failed", exc_info=True)
        if not self._browsing:
            if self._hud_shown:
                # Summoned playback HUD over the video (see hud.py; the
                # renderer owns the summon/auto-hide lifecycle).
                return build_hud(self, size)
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
            dlbar = self._download_bar()
            if dlbar is not None:
                children.append(dlbar)
        children.append(content)
        if self._now_playing is not None and route["kind"] not in CHROME_FREE:
            children.append(self._now_playing_bar(w))
        if self._menu is not None:
            menu = self._tile_menu_node()
            if menu is not None:
                children.append(menu)
        if self._dialog is not None:
            children.append(self._dialog())
        return Column(children, w=w, h=h, align="stretch")

    # Minimum room the page title keeps in the top bar before the
    # buttons drop their labels (~a "Continue Watching" at 22px bold).
    TITLE_MIN_W = 260

    def _chrome(self, w):
        # Fit probe instead of a hardcoded breakpoint: the bar goes
        # icon-only exactly when the labelled version wouldn't leave
        # the title its minimum room at this window width — however
        # many switchers/buttons this session happens to show.
        probe = self._chrome_bar(compact=False, probe=True)
        compact = natural_size(probe)[0] + self.TITLE_MIN_W > w
        return self._chrome_bar(compact=compact)

    def _chrome_bar(self, compact, probe=False):
        title = "" if probe else (self.route.get("title") or _("Home"))

        def nav_button(label, node_id, icon, cb):
            # Icon-only when compact — the icons are the same ones the
            # labels sit next to, so nothing new has to be learned.
            return Button("" if compact else label, id=node_id, icon=icon,
                          on_click=cb)

        left = []
        if len(self.nav_stack) > 1:
            left.append(nav_button(_("Back"), "nav-back", "arrow_back",
                                   self.go_back))
        left.append(nav_button(
            _("Home"), "nav-home", "home",
            lambda: self.navigate({"kind": "home", "server": self.server},
                                  reset=True)))

        right = []
        try:
            servers = self.source.servers()
        except Exception:
            servers = []
        if len(servers) > 1:
            cur = next((i for i, s in enumerate(servers)
                        if s["uuid"] == self.server), 0)
            # sized to its content within bounds (so long names count in
            # the fit probe); overlong labels ellipsize renderer-side
            right.append(Dropdown(
                "nav-server", [s["name"] for s in servers],
                selected=cur, min_w=110,
                max_w=150 if compact else 260,
                on_select=lambda i, v: self._switch_server(servers[i]["uuid"])))
        users = self._users()
        if len(users) > 1:
            cur = next((i for i, u in enumerate(users)
                        if u.get("active")), 0)
            right.append(Dropdown(
                "nav-user",
                [u.get("name", "?") for u in users],
                selected=cur, min_w=100,
                max_w=130 if compact else 200, force=True,
                icons=["lock" if u.get("locked") else "person" for u in users],
                on_select=lambda i, v: self._switch_user(users[i])))
        right += [
            TextBox("nav-search", placeholder=_("Search…"),
                    w=140 if compact else 220,
                    on_change=lambda v: self._search_box.__setitem__("term", v),
                    on_submit=self._search),
            # The textbox submits on Enter, but a visible button is the
            # discoverable affordance (and the only one with a pointer).
            Button("", id="nav-search-go", icon="search", size=18,
                   on_click=lambda: self._search(
                       self._search_box.get("term", ""))),
            nav_button(_("SyncPlay"), "nav-syncplay", "groups",
                       self._open_syncplay),
            nav_button(_("Settings"), "nav-settings", "settings",
                       self._open_settings),
        ]
        middle = [Spacer(w=6), Text(title, size=22, bold=True), Spacer()]
        return Row(
            left + middle + right,
            pad=12, gap=8 if compact else 10, align="center", h=60,
            bg=theme.PANEL_BG,
        )

    # ------------------------------------------------------------ users

    def _users(self):
        """Local users for the switcher: ``[{id, name, locked, active}]``."""
        if self.controller is None:
            return []
        try:
            return list(self.controller.list_users() or [])
        except Exception:
            log.debug("list_users failed", exc_info=True)
            return []

    def _switch_user(self, user):
        if user.get("active"):
            return
        if user.get("locked"):
            self._ask_pin(user)
        else:
            self._do_switch_user(user, None)

    def _ask_pin(self, user):
        state = {"pin": "", "error": None}

        def build():
            rows = [Text(_("Switch to %s") % user.get("name", ""), size=22,
                         bold=True)]
            if state["error"]:
                rows.append(Text(state["error"], size=15, color=theme.FAV_RED))
            rows += [
                TextBox("switch-pin", placeholder=_("PIN"), mask=True, w=240,
                        on_change=lambda v: state.__setitem__("pin", v),
                        on_submit=lambda v: submit()),
                self._dialog_buttons([
                    Button(_("Cancel"), id="switch-cancel",
                           on_click=self._close_dialog),
                    Button(_("Switch"), id="switch-ok", on_click=submit)]),
            ]
            return Dialog("switchpin", self._dialog_shell("switchpin", rows),
                          on_dismiss=self._close_dialog)

        def submit():
            self._do_switch_user(user, state["pin"], on_bad_pin=lambda: (
                state.__setitem__("error", _("Incorrect PIN.")),
                self._show_dialog(build)))
        self._show_dialog(build)

    def _do_switch_user(self, user, pin, on_bad_pin=None):
        ep = self._epoch

        def work():
            return self.controller.switch_user(user.get("id"), pin)

        def done(source):
            if source is False:
                if on_bad_pin is not None:
                    on_bad_pin()
                return
            self._close_dialog()
            if source is None:
                # switched fine, but that user has no reachable server and
                # nothing downloaded — the login screen, not a stuck dialog.
                self._locked = False
                self.show_login()
                return
            self.set_source(source)
        self.run_async(work, done, ep)

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
        # A load that failed with nothing to show says so and offers a
        # retry. Without this the route's data stayed None and the view
        # spun forever, so an unreachable server read as a hang.
        if (route.get("_error")
                and route.get("_data") is None
                and not route.get("_items")):
            return self._error_retry(route)
        return render(route, size)

    def _error_retry(self, route):
        return Box([
            Spacer(),
            Row([Spacer(),
                 Text(route["_error"], size=20, color=theme.SUBTLE_FG),
                 Spacer()]),
            Row([Spacer(),
                 Button(_("Retry"), id="route-retry", icon="refresh",
                        on_click=lambda: self._retry_route(route)),
                 Spacer()]),
            Spacer(),
        ], flex=1, direction="column", align="stretch", gap=14)

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
                geom=self.geom_wide, bleed=True))
        for i, hr in enumerate(data["rows"]):
            if hr.get("items"):
                geom, itype = self._row_shape(hr)
                rows.append(self._tile_row(
                    hr["title"], hr["items"], "row-%d" % i,
                    geom=geom, image_type=itype, bleed=True))
        if not rows:
            rows.append(Row([Spacer(w=self.CONTENT_PAD),
                             Text(_("Nothing to show yet."), size=20,
                                  color=theme.SUBTLE_FG)]))
        # pad=0: home carousels bleed to the window edges so their page
        # arrows sit flush against them (see _hscroll_row).
        return VScroll(Column(rows, gap=20), id="home", flex=1)

    # Item types whose artwork is square, not a 2:3 poster: music, and
    # playlists (whose own Primary image is a square). Rendering them in a
    # poster frame pillarboxes the art.
    SQUARE_TYPES = {"Playlist", "MusicAlbum", "MusicArtist", "Audio",
                    "MusicGenre"}

    def _square_geom(self, items):
        """``geom_square`` when every item's art is square, else None.

        A strip is composited at one tile size, so this is a per-grid
        decision, not per-tile — hence "every item"."""
        types = {i.get("Type") for i in items or ()}
        if types and types <= self.SQUARE_TYPES:
            return self.geom_square
        return None

    def _row_shape(self, hr):
        """(geom, image_type) for a home row, classified like the Tk browser:
        movies/tv/boxsets -> poster; music/playlists -> square; home-video/misc
        or episode-bearing rows -> landscape Thumb."""
        ctype = hr.get("collection_type")
        items = hr.get("items", [])
        has_episode = any(it.get("Type") == "Episode" for it in items)
        if ctype in ("movies", "tvshows", "boxsets"):
            return self.geom, "Primary"
        if ctype in ("music", "playlists"):
            return self.geom_square, "Primary"
        # An untyped row of playlists/music (offline, the mixed rows) still
        # gets square art.
        if self._square_geom(items):
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
        header = [Text(route.get("title", ""), size=26, bold=True)]
        if route["kind"] == "grid":
            if route.get("_collection_capable"):
                header.append(Row([
                    Checkbox(_("Collections"),
                             bool(route.get("_collections")),
                             id="grid-collections",
                             on_toggle=lambda: self._toggle_collections(
                                 route))], gap=10, align="center"))
            header.append(self._grid_filter_bar(route))
            total = route.get("_total") or 0
            header.append(Text(_("%(shown)d of %(total)d") % {
                "shown": len(items), "total": total},
                size=14, color=theme.SUBTLE_FG))
        # Header height (title + optional filter bar + count) so the
        # virtualizer can map a scroll offset onto a tile row.
        head_h = 40 + (110 if route["kind"] == "grid" else 0)
        rows = header + self._grid_of(
            items, "grid", size, geom=self._square_geom(items) or self.geom,
            scroll_id="grid", head_h=head_h)
        return VScroll(
            Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP,
                   align="stretch"), id="grid",
            flex=1,
            on_scroll=lambda off, mx: self._on_scroll(
                "grid", off, mx,
                lambda o, m: self._on_grid_scroll(route, o, m)),
        )

    def _grid_filter_bar(self, route):
        vals = route.get("_filtervals") or {}
        filters = route.get("_filters") or {}
        genres = vals.get("genres") or []
        gi = 0
        if filters.get("genre") in genres:
            gi = genres.index(filters["genre"]) + 1
        # Years come back as ints; keep them that way in the filter (the
        # offline source compares against ProductionYear directly) and only
        # stringify for display.
        years = list(vals.get("years") or [])
        yi = 0
        if filters.get("year") in years:
            yi = years.index(filters["year"]) + 1
        bar = Row([
            Dropdown("grid-sort", [s[0] for s in SORTS],
                     selected=route.get("_sort", 0), w=180,
                     on_select=lambda i, v: self._set_grid("_sort", route, i)),
            Dropdown("grid-genre", [_("All Genres")] + genres, selected=gi,
                     w=180,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "genre", None if i == 0 else genres[i - 1])),
            Dropdown("grid-year",
                     [_("All Years")] + [str(y) for y in years],
                     selected=yi, w=140,
                     on_select=lambda i, v: self._set_grid_filter(
                         route, "year", None if i == 0 else years[i - 1])),
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
            # flex + align="center" centres the glyph horizontally; a bare
            # Text is packed at the box's left edge (Box only centres on its
            # cross axis), which left every letter hugging its left border.
            Box([Text(ch, size=15, align="center", flex=1,
                      color=theme.ACCENT_FG if cur_letter == ch
                      else theme.SUBTLE_FG)],
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
            if route.get("_collections"):
                return self.source.get_movie_collections(
                    srv, start_index=start, sort_by=sort_by,
                    sort_order=sort_order, filters=filters)
            return self.source.get_library_items(
                srv, route["parent_id"], start_index=start, sort_by=sort_by,
                sort_order=sort_order, filters=filters)

        def done(res):
            new, total2 = res
            route["_items"] = (route.get("_items") or []) + new
            # A server that answers an in-range page with nothing (a random
            # sort reshuffling per request, a filter the server applies
            # differently) would otherwise be re-asked on every scroll
            # event forever. Treat the page as the end of the list.
            route["_total"] = (total2 if new
                               else len(route.get("_items") or []))
            route["_loading"] = False

        def failed(_exc):
            # _loading is set before dispatch: leaving it set on a failed
            # page stopped this grid from ever paging again.
            route["_loading"] = False
            self.status = _("Could not load more items.")

        self.run_async(work, done, ep, on_error=failed)

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

    def _body_w(self, w):
        """Usable text width inside a padded, scrollable content column.

        The window width minus the content padding AND the scrollbar the
        scroll view reserves. Wrapping at ``w - 2*pad`` — the padding alone
        — makes every line 10px wider than the space it actually gets, so
        the tail of each line runs under the scrollbar, and which words land
        there changes with the window size. That is what made resizing look
        like the wrapping was unstable."""
        from ..mpvtk.layout import SCROLLBAR_W

        return max(120, w - 2 * self.CONTENT_PAD - SCROLLBAR_W)

    def _paragraph(self, text, size, max_w, color=None):
        """Wrapped body text (overviews).

        The layout engine wraps *within* a paragraph, so blank-line breaks
        are handled here. The gap is a full line height: at anything less
        the paragraph break reads as tighter than the wrapped lines around
        it, which looks like a mistake rather than a break."""
        from ..mpvtk.layout import LINE_H

        paras = [p.strip() for p in (text or "").replace("\r", "").split("\n")
                 if p.strip()]
        color = color or theme.TEXT_FG
        if len(paras) <= 1:
            return Text(paras[0] if paras else "", size=size, color=color,
                        wrap=True, w=max_w)
        return Column([Text(p, size=size, color=color, wrap=True, w=max_w)
                       for p in paras],
                      gap=round(size * LINE_H), w=max_w)

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

    def _default_track_indices(self, route, src, item):
        """``(aid, sid)`` playback will actually choose for ``src``:
        language_config first, then the server's session default — the same
        resolution media.map_streams performs.

        The pickers have to show these rather than a bare "None". A browser
        selection is taken as final downstream (``explicit_tracks``, which
        makes map_streams skip its own defaulting), so a picker that
        misreports the default doesn't just look wrong — it makes playback
        obey the lie, and remember_subtitle_track then pins it for the rest
        of the queue.

        Cached per media source: this is reached from build(), i.e. once a
        repaint, and apply() does real work and logs every call."""
        cache = route.setdefault("_def_tracks", {})
        key = (src or {}).get("Id")
        if key in cache:
            return cache[key]
        aid = sid = None
        if src:
            try:
                from ..conf import settings
                from ..language_config import apply as apply_language_config

                aid, sid = apply_language_config(
                    settings.language_config, src, item)
            except Exception:
                log.debug("language_config lookup failed", exc_info=True)
                aid = sid = None
            if aid is None:
                aid = src.get("DefaultAudioStreamIndex")
            if sid is None:
                sid = src.get("DefaultSubtitleStreamIndex")
        cache[key] = (aid, sid)
        return aid, sid

    def _effective_tracks(self, route, item):
        """``(aid, sid)`` the pickers display and playback is started with:
        the user's pick where they made one, otherwise the resolved default.

        Both are sent, not just the one that was touched — mirroring the Tk
        browser, whose comboboxes are always populated. Sending only the
        touched one marks the play explicit and map_streams then returns
        before defaulting the other, which is how picking an audio track
        silently turned the subtitles off."""
        src = self._sel_source(item.get("MediaSources") or [], route)
        streams = (src or {}).get("MediaStreams") or []
        def_aid, def_sid = self._default_track_indices(route, src, item)
        aid, sid = route.get("_aid"), route.get("_sid")
        # Only default a kind that actually has streams, so an item with no
        # subtitles isn't reported as a deliberate choice.
        if aid is None and any(s.get("Type") == "Audio" for s in streams):
            aid = def_aid
        if sid is None and any(s.get("Type") == "Subtitle" for s in streams):
            sid = def_sid
        return aid, sid

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
        # What the pickers show must be what will play — see _effective_tracks.
        eff_aid, eff_sid = self._effective_tracks(route, item)
        if audio:
            names = [label(s, _("Audio")) for s in audio]
            cur = next((i for i, s in enumerate(audio)
                        if s.get("Index") == eff_aid), 0)
            controls.append(self._picker_row(
                _("Audio"), "dt-audio", names, cur,
                lambda i, v: route.__setitem__("_aid", audio[i].get("Index"))))
        if subs:
            names = [_("None")] + [label(s, _("Sub")) for s in subs]
            cur = 0
            if eff_sid not in (None, -1):
                cur = next((i + 1 for i, s in enumerate(subs)
                            if s.get("Index") == eff_sid), 0)
            controls.append(self._picker_row(
                _("Subtitle"), "dt-sub", names, cur,
                lambda i, v: route.__setitem__(
                    "_sid", -1 if i == 0 else subs[i - 1].get("Index"))))
        return controls

    def _picker_row(self, label, node_id, names, selected, on_select):
        return Row([Text(label, w=90, size=16, color=theme.SUBTLE_FG),
                    Dropdown(node_id, names, selected=selected, w=300,
                             on_select=on_select)], gap=8, align="center")

    @staticmethod
    def _fmt_ticks(ticks):
        """h:mm:ss / m:ss — a bare minutes:seconds rendered a 1h20m resume
        offset as "80:00"."""
        secs = int((ticks or 0) // 10000000)
        h, m, sec = secs // 3600, (secs % 3600) // 60, secs % 60
        return ("%d:%02d:%02d" % (h, m, sec) if h
                else "%d:%02d" % (m, sec))

    def _play_buttons(self, route, item, server, trailers=None):
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        srcid = (route.get("_srcid")
                 or ((item.get("MediaSources") or [{}])[0]).get("Id"))
        aid, sid = self._effective_tracks(route, item)
        buttons = []
        if pos > 0:
            buttons.append(self._action_btn(
                "play_arrow", _("Resume") + "  " + self._fmt_ticks(pos),
                "btn-resume",
                lambda: self._play(item, server, offset_ticks=pos,
                                   srcid=srcid, aid=aid, sid=sid),
                primary=True, size=18))
        buttons.append(self._action_btn(
            "play_arrow", _("Play"), "btn-play",
            lambda: self._play(item, server, srcid=srcid, aid=aid, sid=sid),
            primary=(pos <= 0), size=18))
        tids = [t.get("Id") for t in (trailers or []) if t.get("Id")]
        if tids:
            buttons.append(self._action_btn(
                "movie", _("Trailer"), "btn-trailer",
                lambda: self._play_list(tids, server, 0), size=18))
        return Row(buttons, gap=10)

    def _scenes_row(self, item, server):
        """The chapter carousel ("Scenes"), each tile seeking to its start.

        Chapter art is indexed rather than tagged, so the tiles carry a
        ready-made image spec+url (see _poster_for) — image_spec can't
        address it."""
        chapters = item.get("Chapters") or []
        if len(chapters) < 2:
            return None          # a single chapter is just the start
        iid = item.get("Id")
        tiles = []
        for i, ch in enumerate(chapters):
            url = None
            try:
                url = self.source.chapter_image_url(server, iid, i, ch,
                                                    width=self.geom_wide.tile_w)
            except Exception:
                log.debug("chapter art failed", exc_info=True)
            start = ch.get("StartPositionTicks") or 0
            tiles.append({
                "Id": "%s#ch%d" % (iid, i),
                "Name": ch.get("Name") or _("Chapter %d") % (i + 1),
                "Type": "Chapter",
                "_start_ticks": start,
                "_subtitle": self._fmt_ticks(start),
                "_image_spec": ((iid, "Chapter%d" % i,
                                 ch.get("ImageTag") or "none") if url else None),
                "_image_url": url,
            })
        return self._tile_row(
            _("Scenes"), tiles, "detail-scenes", geom=self.geom_wide,
            on_click=lambda t: self._play(
                item, server, offset_ticks=t.get("_start_ticks") or 0))

    def _action_btn(self, icon, text, node_id, cb, on=False, primary=False,
                    size=16):
        """An icon+label action button.

        ``primary`` is the accent-filled call to action (Play, Next Up);
        ``on`` is a *toggle* that happens to share the accent fill (Watched,
        Favorite). Both use white on blue — black on blue read as disabled.

        Every button in an action row must come from here, icon or not: the
        plain Button widget defaults to a 20px label against this one's 16,
        which made the odd trailing button ~5px taller than its neighbours.
        """
        accent = on or primary
        fg = theme.ACCENT_FG if accent else theme.TEXT_FG
        children = []
        if icon:
            children.append(Icon(icon, size + 2, color=fg))
        children.append(Text(text, size=size, color=fg))
        return Row(children,
                   id=node_id, gap=7, pad=10,
                   bg=theme.ACCENT if accent else theme.BUTTON_BG,
                   hover={"fill": theme.ACCENT_HOVER if accent
                          else theme.BUTTON_ACTIVE},
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
            btns.append(self._action_btn(
                "movie", _("Go to Series"), "act-series",
                lambda: self.navigate({
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
            lambda: self._play_next_up(series_id, server), primary=True)]
        btns += self._common_actions(item, server, "sa")
        return Row(btns, gap=8, align="center")

    def _act_watched(self, item, server):
        ud = item.setdefault("UserData", {})
        was_played, was_count = ud.get("Played"), ud.get("UnplayedItemCount")
        new = not self._is_watched(item)
        ud["Played"] = new
        if item.get("Type") in ("Series", "Season"):
            ud["UnplayedItemCount"] = 0 if new else 1

        def work():
            # Roll the optimistic flip back if nothing recorded it (offline
            # un-watching, or nothing downloaded to queue against). Leaving
            # the tick up meant the UI claimed a change that never happened
            # and quietly reverted on the next reload.
            ok = self.controller.set_watched(server, item.get("Id"), new)
            if ok is False:
                ud["Played"] = was_played
                if was_count is None:
                    ud.pop("UnplayedItemCount", None)
                else:
                    ud["UnplayedItemCount"] = was_count
                self.invalidate()
        self._pool.submit(lambda: self._safe(lambda _c: work()))
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
        bw, bh = self._banner_box(w)
        title, context = self._heading_for(item)
        meta = self._meta_line(item)
        banner = self._backdrop_node(item, (bw, bh), "detail-bd",
                                     title=title, meta=meta, context=context)
        blocks = [banner]
        if isinstance(banner, Box):
            # No artwork (or still loading): draw the heading normally, with
            # the same title/context split the baked one uses.
            if context:
                blocks.append(Text(context, size=17, color=theme.SUBTLE_FG))
            blocks.append(Text(title, size=26, bold=True, wrap=True,
                               w=self._body_w(w)))
            if meta:
                blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        info = self._media_info_line(item, route)
        if info:
            blocks.append(Text(info, size=15, color=theme.SUBTLE_FG))
        blocks.append(self._play_buttons(route, item, server,
                                         trailers=data.get("trailers")))
        blocks.append(self._detail_actions(item, server))
        blocks.extend(self._track_pickers(route, item))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, self._body_w(w)))
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
        bw, bh = self._banner_box(w)
        server = route.get("server") or self.server
        meta = self._meta_line(item)
        banner = self._backdrop_node(item, (bw, bh), "series-bd",
                                     title=item.get("Name", ""), meta=meta)
        blocks = [banner]
        if isinstance(banner, Box):
            blocks.append(Text(item.get("Name", ""), size=30, bold=True))
            if meta:
                blocks.append(Text(meta, size=18, color=theme.SUBTLE_FG))
        blocks.append(self._series_actions(item, server, route["item_id"]))
        if item.get("Overview"):
            blocks.append(self._paragraph(item["Overview"], 18, self._body_w(w)))
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
            title_row.append(self._action_btn(
                "movie", _("To Series"), "season-to-series",
                lambda: self.navigate({
                    "kind": "series", "server": server,
                    "item_id": route["series_id"],
                    "title": season_item.get("SeriesName", "")})))
        header = [Row(title_row, gap=12, align="center"),
                  Row(self._common_actions(season_item or {"Id": route["item_id"],
                                           "Type": "Season"}, server, "se"),
                      gap=8, align="center")]
        rows = header + self._grid_of(
            episodes, "ep", size, geom=geom, image_type="Thumb",
            scroll_id="season", head_h=100)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                       id="season", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "season", off, mx))

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
        rows = [Text(_('Results for "%s"') % term, size=24, bold=True)]
        if people:
            rows.append(self._tile_row(_("People"), people, "search-people",
                                       geom=self.geom_square))
        # Group by type, each with its natural tile shape (like the Tk browser).
        groups = [
            (_("Movies"), ("Movie",), self.geom, "Primary"),
            (_("Shows"), ("Series",), self.geom, "Primary"),
            (_("Episodes"), ("Episode",), self.geom_wide, "Thumb"),
            (_("Videos"), ("Video", "MusicVideo"), self.geom_wide, "Primary"),
            (_("Albums"), ("MusicAlbum",), self.geom_square, "Primary"),
            (_("Artists"), ("MusicArtist",), self.geom_square, "Primary"),
        ]
        used = set()
        for label, types_, geom, itype in groups:
            group = [it for it in items if it.get("Type") in types_]
            if group:
                used.update(types_)
                rows.append(self._tile_row(
                    label, group, "search-" + label, geom=geom,
                    image_type=itype))
        songs = [it for it in items if it.get("Type") == "Audio"]
        if songs:
            server = route.get("server") or self.server
            ids = [s.get("Id") for s in songs]
            rows.append(Text(_("Songs"), size=24, bold=True))
            rows.append(self._track_list(
                songs, "search-song",
                lambda i: self._play_list(ids, server, i, audio=True),
                scroll_id="search", head_h=120))
        other = [it for it in items
                 if it.get("Type") not in used and it.get("Type") != "Audio"]
        if other:
            rows.append(self._tile_row(_("Other"), other, "search-other"))
        if not items and not people:
            rows.append(Text(_("No results."), size=18, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=12,
                              align="stretch"), id="search", flex=1)

    # ---------------------------------------------------- music / playlists

    def _cols(self, w, geom):
        # _body_w, not w - 32: grids sit in the same padded scroll column,
        # so ignoring the scrollbar fits one tile too many at some widths
        # and the last one is clipped.
        return max(1, int(
            (self._body_w(w) + geom.gap) // (geom.tile_w + geom.gap)))

    GRID_GAP = 12

    def _grid_of(self, items, prefix, size, heading=None, geom=None,
                 image_type="Primary", scroll_id=None, head_h=0,
                 on_click=None):
        """Tile rows for a vertical grid.

        With ``scroll_id`` the rows are **virtualized**: only those within a
        screen of the viewport are composited, the rest become fixed-height
        Spacers. Without it a long library blows past both the strip cache and
        mpv's 63-overlay budget, which showed up as tiles that came back blank
        after scrolling away and back."""
        geom = geom or self.geom
        cols = self._cols(size[0], geom)
        rows = [Text(heading, size=26, bold=True)] if heading else []
        nrows = (len(items) + cols - 1) // cols
        first, last = 0, nrows - 1
        if scroll_id is not None:
            rh = geom.strip_h + self.GRID_GAP
            vh = max(240.0, float(size[1]))
            top = max(0.0, self._offset(scroll_id) - head_h)
            first = int(max(0.0, top - vh) // rh)
            last = int((top + 2 * vh) // rh)
        for r in range(nrows):
            if first <= r <= last:
                start = r * cols
                rows.append(self._image_map(items[start:start + cols],
                                            "%s-%d" % (prefix, start),
                                            geom, image_type,
                                            on_click=on_click))
            else:
                rows.append(Spacer(h=geom.strip_h))
        if not items:
            rows.append(Text(_("Nothing here yet."), size=18,
                             color=theme.SUBTLE_FG))
        return rows

    def _offset(self, scroll_id):
        """Live scroll offset for a container.

        The renderer owns scroll state and clamps it to the *current* content,
        so its value is the only one that can't be stale — read it
        synchronously at build time and fall back to the throttled on_scroll
        copy only when the property isn't available (mpv < 0.36)."""
        live = self._live_offsets
        if live is not None and scroll_id in live:
            return float(live[scroll_id] or 0.0)
        return float(self._scroll_off.get(scroll_id, 0.0))

    # Re-render once the view has scrolled about this far, so the virtualized
    # window is refreshed well before the user reaches its edge.
    SCROLL_STEP = 120

    def _on_scroll(self, scroll_id, offset, maximum, then=None):
        """Record a scroll offset (for virtualization) and run ``then``
        (paging). Only re-renders when the offset moved enough to change the
        materialized window."""
        prev = self._scroll_off.get(scroll_id)
        self._scroll_off[scroll_id] = offset
        if then is not None:
            then(offset, maximum)
        if prev is None or abs(offset - prev) >= self.SCROLL_STEP:
            self.invalidate()

    @staticmethod
    def _duration(item):
        secs = (item.get("RunTimeTicks") or 0) // 10000000
        return "%d:%02d" % (secs // 60, secs % 60) if secs else ""

    @staticmethod
    def _artists(item):
        return ", ".join(item.get("Artists") or item.get("AlbumArtists") or [])

    def _track_list(self, tracks, prefix, on_play, playing_id=None,
                    selected=None, on_select=None, album=True,
                    art=False, scroll_id=None, head_h=0):
        """Tabular track list (album, playlist, queue, search songs).

        Uses the toolkit's Table so header and cells come from one column
        spec — hand-laid Rows drifted out of alignment as soon as a cell's
        text width changed.

        With ``on_select`` the row click selects (mods-aware) and the first
        column becomes a play button, so a selectable list still has a
        one-click way to jump to a track. Without it, clicking the row plays.

        ``art=True`` adds a leading album-art thumbnail column — useful in
        mixed-album lists (playlists); redundant on an album page."""
        selected = selected or set()
        columns = []
        if art:
            columns.append({"label": "", "w": 32})
        columns += [{"label": "#", "w": 46, "align": "right"},
                    {"label": _("Title"), "flex": 3},
                    {"label": _("Artist"), "flex": 2}]
        if album:
            columns.append({"label": _("Album"), "flex": 2})
        columns.append({"label": _("Time"), "w": 70, "align": "right"})

        def first_cell(i, tr):
            if on_select is None:
                return str(tr.get("IndexNumber") or (i + 1))
            return Box([Icon("play_arrow", 16,
                             color=theme.ACCENT if tr.get("Id") == playing_id
                             else theme.SUBTLE_FG)],
                       id="%s-play-%d" % (prefix, i), w=40, h=26,
                       direction="row", align="center", radius=4,
                       hover={"fill": theme.BUTTON_ACTIVE},
                       on_click=lambda i=i: on_play(i))

        # Virtualize against the live scroll offset. Not just a repaint
        # cost: with art=True each visible row is one mpv overlay, and a
        # few hundred tracks would blow the 63-overlay budget outright.
        virtual = None
        if scroll_id is not None and self._size is not None:
            virtual = {"offset": max(0.0, self._offset(scroll_id) - head_h),
                       "height": float(self._size[1])}
        # The window has to be known HERE, not just inside Table: art cells
        # composite a bitmap into the 48-entry strip LRU as they are built,
        # so building them for every row of a long playlist evicted (and
        # freed the backing buffer of) the very rows on screen — they drew
        # blank, deterministically, on every repaint.
        art_first, art_last = virtual_window(virtual, self.TRACK_ROW_H,
                                             len(tracks))

        rows = []
        for i, tr in enumerate(tracks):
            playing = playing_id is not None and tr.get("Id") == playing_id
            cells = [first_cell(i, tr), tr.get("Name", ""), self._artists(tr)]
            if art:
                cells.insert(0, self._art_cell(tr)
                             if art_first <= i < art_last
                             else self._art_placeholder())
            if album:
                cells.append(tr.get("Album", "") or "")
            cells.append(self._duration(tr))
            rows.append({
                "id": "%s-%d" % (prefix, i),
                "selected": i in selected or playing,
                "cells": cells,
                "on_click": ((lambda mods, i=i: on_select(i, mods))
                             if on_select is not None
                             else (lambda i=i: on_play(i))),
            })
        return Table(columns, rows, size=17, row_h=self.TRACK_ROW_H,
                     hover_bg=theme.BUTTON_BG, virtual=virtual)

    def _play_shuffle(self, ids, server, audio=True):
        import random
        ids = [i for i in ids if i]
        random.shuffle(ids)
        self._play_list(ids, server, 0, audio=audio)

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
                                                     audio=True),
                             primary=True),
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
                      fg=theme.ACCENT_FG if active else theme.TEXT_FG,
                      on_click=lambda: self._set_music_tab(route, tab))

    def _set_music_tab(self, route, tab):
        route["_tab"] = tab
        for k in ("_data", "_total"):
            route.pop(k, None)
        route["_loading"] = False
        # A new tab starts at the top; a stale offset would virtualize the
        # wrong window and show a screenful of blank rows.
        self._scroll_off.pop("music-grid", None)
        self._scroll_off.pop("music-songs", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _music_page(self, route, start_index):
        """A ``work()`` that fetches one page of the route's music tab,
        returning ``(items, total)``. Genres are unpaged server-side, so they
        report their own length as the total."""
        srv = route.get("server") or self.server
        parent = route["parent_id"]
        tab = route.get("_tab", "albums")

        def work():
            if tab == "albumartists":
                return self.source.get_album_artists(
                    srv, parent, start_index=start_index)
            if tab == "artists":
                return self.source.get_artists(
                    srv, parent, start_index=start_index)
            if tab == "songs":
                return self.source.get_songs(
                    srv, parent, start_index=start_index)
            if tab == "genres":
                genres = self.source.get_music_genres(srv, parent)
                return (genres if start_index == 0 else []), len(genres)
            return self.source.get_music_albums(
                srv, parent, start_index=start_index)
        return work

    def _on_music_scroll(self, route, offset, maximum):
        """Page the current music tab in near the bottom (the Tk browser's
        _MusicGrid did this per tab; without it a library is capped at the
        first 100 albums)."""
        if route is not self.route:
            return
        items = route.get("_data") or []
        total = route.get("_total") or 0
        if route.get("_loading") or len(items) >= total or not items:
            return
        if maximum - offset >= 800:
            return
        route["_loading"] = True
        ep = self._epoch

        def done(res):
            new, total2 = res
            if new:
                route["_data"] = (route.get("_data") or []) + new
                route["_total"] = total2
            else:
                route["_total"] = len(route.get("_data") or [])
            route["_loading"] = False
        self.run_async(self._music_page(route, len(items)), done, ep)

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
                lambda i: self._play_list(ids, server, i, audio=True),
                scroll_id="music-songs")],
                pad=self.CONTENT_PAD, align="stretch"),
                id="music-songs", flex=1,
                on_scroll=lambda off, mx: self._on_scroll(
                    "music-songs", off, mx,
                    lambda o, m: self._on_music_scroll(route, o, m)))
        else:
            tab = route.get("_tab")
            geom = (self.geom_wide if tab == "genres"
                    else self.geom_square)
            body = VScroll(
                Column(self._grid_of(data, "music", size, geom=geom,
                                     scroll_id="music-grid"),
                       pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                id="music-grid", flex=1,
                on_scroll=lambda off, mx: self._on_scroll(
                    "music-grid", off, mx,
                    lambda o, m: self._on_music_scroll(route, o, m)))
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
        ], gap=14)
        body = self._track_list(
            tracks, "trk",
            lambda i: self._play_list(ids, server, i, audio=True),
            scroll_id="album", head_h=110)
        return VScroll(Column([header, body], pad=self.CONTENT_PAD, gap=12,
                              align="stretch"),
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
                Spacer(h=4),
                self._music_action_bar(server, ids, route["item_id"], "art")]
        rows += self._grid_of(albums, "artist", size, geom=self.geom_square,
                              scroll_id="artist", head_h=110)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                       id="artist", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "artist", off, mx))

    def _render_music_genre(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        albums = data.get("albums") or []
        songs = data.get("songs") or []
        server = route.get("server") or self.server
        ids = [s.get("Id") for s in songs]
        rows = [Text(route.get("title", ""), size=26, bold=True),
                Spacer(h=4),
                self._music_action_bar(server, ids, route["item_id"], "gen")]
        rows += self._grid_of(albums, "mgenre", size, geom=self.geom_square,
                              scroll_id="mgenre", head_h=110)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=self.GRID_GAP),
                       id="mgenre", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "mgenre", off, mx))

    def _render_playlist(self, route, size):
        data = route.get("_data")
        if data is None:
            return self._busy()
        server = route.get("server") or self.server
        pid = route["item_id"]
        raw = list(data)
        # A playlist's declared type and its contents can diverge, so filter
        # by what's actually playable rather than trusting the container.
        items = [i for i in raw if i.get("Type") in PLAYLIST_SUPPORTED_TYPES]
        ids = [i.get("Id") for i in items]
        audio = bool(items) and all(i.get("Type") == "Audio" for i in items)
        pl_item = {"Id": pid, "Type": "Playlist",
                   "Name": route.get("title", "")}
        header = Row([
            Text(route.get("title", ""), size=28, bold=True),
            Spacer(),
            self._action_btn("play_arrow", _("Play All"), "pl-play",
                             lambda: self._play_list(ids, server, 0,
                                                     audio=audio),
                             primary=True),
            self._action_btn("shuffle", _("Shuffle"), "pl-shuffle",
                             lambda: self._play_shuffle(ids, server,
                                                        audio=audio)),
            self._action_btn("file_download", _("Download"), "pl-download",
                             lambda: self._open_download(pl_item)),
            self._action_btn("edit", _("Edit"), "pl-edit",
                             lambda: self.navigate({
                                 "kind": "playlist_edit", "server": server,
                                 "item_id": pid,
                                 "title": route.get("title", "")})),
        ], align="center", gap=10)
        if not items:
            body = [Text(
                _("This playlist is empty.") if not raw else
                _("This playlist has no supported media types."),
                size=18, color=theme.SUBTLE_FG)]
        elif audio:
            # Music playlists read as a track list, like the Tk browser —
            # a wall of identical album covers tells you nothing. Per-track
            # art earns its column here though: albums differ per row.
            body = [self._track_list(
                items, "pl",
                lambda i: self._play_list(ids, server, i, audio=True,
                                          items=items),
                art=True, scroll_id="playlist", head_h=70)]
        else:
            # `items`, not `data`: unsupported entries were rendering as
            # tiles whose click did something unrelated. And a click plays
            # the PLAYLIST from that point — going through _open_item meant
            # Play on the detail page queued the item's series instead,
            # silently abandoning the playlist the user was in.
            body = self._grid_of(
                items, "pl", size, scroll_id="playlist", head_h=70,
                on_click=lambda it: self._play_list(
                    ids, server, items.index(it), audio=False, items=items))
        return VScroll(Column([header, Spacer(h=2)] + body,
                              pad=self.CONTENT_PAD, gap=self.GRID_GAP,
                              align="stretch"),
                       id="playlist", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "playlist", off, mx))

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

        # commit-only: dragging shouldn't spam absolute seeks mid-gesture
        seek = Slider("np-seek", value=pos, min=0, max=max(1, dur),
                      force=True, flex=1,
                      on_commit=lambda v: self._ctl(lambda c: c.seek(v)))
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
        self.open_settings()

    def open_settings(self, tab="general"):
        """Open Settings on ``tab``. Public: the tray's Configure Servers /
        Show Console entries route here — which is why it has to respect the
        lock gate: the logs and server list are behind the PIN too."""
        if self._locked:
            return
        if self.route.get("kind") == "settings":
            self.route["_tab"] = tab   # already there — just switch tabs
            self.invalidate()
            return
        self.navigate({"kind": "settings", "server": self.server,
                       "title": _("Settings"), "_tab": tab})

    def _set_setting(self, key, value):
        ok = self._config().set_setting(key, value)
        self.status = ((_("Saved: %s") if ok else _("Invalid value: %s"))
                       % key)
        if ok and key == "work_offline":
            self._apply_work_offline(bool(value))
        self.invalidate()

    def _apply_work_offline(self, offline):
        """Swap the data source when the setting is toggled, rather than
        persisting a key that does nothing until the next launch. Tk
        applies it live too."""
        if self.controller is None or offline == self._offline:
            return

        ep = self._epoch

        def work():
            if offline:
                return self.controller.offline_source()
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is None:
                self.status = (_("Nothing downloaded to browse offline.")
                               if offline else
                               _("Could not reach a server."))
                return
            self.set_source(source)
        self.run_async(work, done, ep)

    SETTINGS_TABS = ("general", "servers", "downloads", "logs")

    def _render_settings(self, route, size):
        tab = route.get("_tab", "general")
        labels = {"general": _("General"), "servers": _("Servers & Users"),
                  "downloads": _("Downloads"), "logs": _("Logs")}
        tabs = Row([
            Button(labels[t], id="stab-" + t,
                   bg=theme.ACCENT if tab == t else theme.BUTTON_BG,
                   fg=theme.ACCENT_FG if tab == t else theme.TEXT_FG,
                   on_click=lambda t=t: self._set_settings_tab(route, t))
            for t in self.SETTINGS_TABS
        ], gap=8)
        body = {
            "servers": self._settings_servers,
            "downloads": self._settings_downloads,
            "logs": self._settings_logs,
        }.get(tab, self._settings_general)(route, size)
        head = [Row([tabs], pad=12)]
        if self.status:
            head.append(Row([Spacer(w=self.CONTENT_PAD),
                             Text(self.status, size=15,
                                  color=theme.SUBTLE_FG)]))
        return Column(head + [body], flex=1, align="stretch")

    def _set_settings_tab(self, route, tab):
        route["_tab"] = tab
        self.status = ""
        self.invalidate()

    # -- General (the generated config form) ------------------------------

    def _settings_general(self, route, size):
        cfg = self._config()
        schema = cfg.settings_schema()
        values = cfg.get_settings()
        show_adv = bool(route.get("_advanced"))
        rows = []
        for title, keys in cfg.sections():
            advanced = title == _("Advanced")
            if advanced:
                rows.append(Checkbox(
                    _("Show advanced settings"), show_adv, id="set-adv",
                    on_toggle=lambda: self._toggle_advanced(route)))
                if not show_adv:
                    continue
            rows.append(Text(title, size=20, bold=True))
            notes = getattr(cfg, "NOTES", None) or {}
            for key in keys:
                rows.append(self._setting_row(cfg, schema, values, key))
                if key in notes:
                    # An explanatory line under the setting it belongs to;
                    # the settings it qualifies follow directly below.
                    rows.append(Text(notes[key], size=14,
                                     color=theme.SUBTLE_FG, wrap=True))
        rows.append(Text(_("Some changes take effect after restarting."),
                         size=14, color=theme.SUBTLE_FG))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="settings", flex=1)

    def _toggle_collections(self, route):
        """Movies library <-> its collections, like jellyfin-web's toggle.
        Collections are server-wide and recursive, so this is a different
        query rather than a filter."""
        route["_collections"] = not route.get("_collections")
        route.pop("_items", None)
        route.pop("_total", None)
        route.pop("_loading", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _toggle_advanced(self, route):
        route["_advanced"] = not route.get("_advanced")
        self.invalidate()

    def _setting_row(self, cfg, schema, values, key):
        kind = schema.get(key, "str")
        val = values.get(key)
        label = cfg.label_for(key)
        if kind == "bool":
            return Checkbox(label, bool(val), id="set-" + key,
                            on_toggle=lambda k=key, v=val: self._set_setting(
                                k, not bool(v)))
        if key in cfg.LABELED_ENUMS:
            opts = cfg.LABELED_ENUMS[key]
            cur = next((i for i, (_l, v) in enumerate(opts)
                        if str(v) == str(val)), 0)
            widget = Dropdown(
                "set-" + key, [lbl for lbl, _v in opts], selected=cur, w=340,
                force=True,
                on_select=lambda i, _v, k=key, o=opts: self._set_setting(
                    k, o[i][1]))
        elif key in cfg.ENUMS:
            opts = cfg.ENUMS[key]
            cur = opts.index(str(val)) if str(val) in opts else 0
            widget = Dropdown(
                "set-" + key, opts, selected=cur, w=340, force=True,
                on_select=lambda i, _v, k=key, o=opts: self._set_setting(
                    k, o[i]))
        elif key == "sync_path":
            widget = Row([
                TextBox("set-" + key, text="" if val is None else str(val),
                        w=250,
                        on_submit=lambda v: self._move_downloads(v)),
                Button(_("Move"), id="set-sync-move",
                       on_click=lambda: self._move_downloads(None)),
            ], gap=8, align="center")
        else:
            widget = TextBox("set-" + key,
                             text="" if val is None else str(val), w=340,
                             on_submit=lambda v, k=key: self._set_setting(k, v))
        return Row([Text(label, w=340, size=17, color=theme.SUBTLE_FG),
                    widget], gap=12, align="center")

    def _move_downloads(self, path):
        """Relocating the download store copies files (possibly across drives),
        so it runs on the pool and reports progress into the status line."""
        if path is None:
            self.status = _("Press Enter in the folder field to move.")
            self.invalidate()
            return
        cfg = self._config()
        if not hasattr(cfg, "relocate_downloads"):
            self._set_setting("sync_path", path)
            return
        self.status = _("Moving downloads…")
        self.invalidate()

        def work():
            def progress(copied, total):
                pct = 100 if not total else min(100, int(copied * 100 / total))
                self.status = _("Moving downloads… %d%%") % pct
                self.invalidate()
            try:
                ok, message = cfg.relocate_downloads(path, progress=progress)
            except Exception:
                log.error("download folder move failed", exc_info=True)
                ok, message = False, _("Moving the downloads failed.")
            self.status = (message or (
                _("Download folder moved. Restart to finish switching.")
                if ok else _("Moving the downloads failed.")))
            self.invalidate()
        self._pool.submit(work)

    # -- Servers & Users --------------------------------------------------

    def _settings_servers(self, route, size):
        users = self._users()
        # Grid, not per-row fixed widths: the name/status/button columns
        # share tracks across rows, and the button track auto-sizes to
        # the widest button set (translations included).
        user_rows = [Grid(
            [self._user_row(u, i, len(users) > 1)
             for i, u in enumerate(users)],
            cols=[{"w": 22}, {"flex": 1}, {"w": 90},
                  {"align": "right"}],
            gap=8, row_gap=4, row_pad=8,
        )]
        user_rows.append(Row([
            TextBox("su-newuser", placeholder=_("New user name…"), w=240,
                    on_change=lambda v: self._newuser.__setitem__("name", v),
                    on_submit=self._add_user),
            Button(_("Add User"), id="su-adduser", icon="person_add",
                   on_click=lambda: self._add_user(
                       self._newuser.get("name", ""))),
            Spacer(),
        ], gap=8, align="center"))

        servers = []
        if self.controller is not None:
            try:
                servers = self.controller.list_servers()
            except Exception:
                log.debug("list_servers failed", exc_info=True)
        active = next((u.get("name") for u in users if u.get("active")), None)
        server_rows = []
        if not servers:
            server_rows.append(Text(_("No servers configured yet."), size=15,
                                    color=theme.SUBTLE_FG))
        else:
            server_rows.append(Grid(
                [self._server_row(sv, i) for i, sv in enumerate(servers)],
                cols=[{"w": 22}, {"flex": 1}, {}, {},
                      {"align": "right"}],
                gap=12, row_gap=4, row_pad=8,
            ))
        server_rows.append(Row([
            Button(_("Add Server"), id="sv-add", icon="add",
                   on_click=self.show_login),
            Spacer(),
        ], gap=8, align="center"))

        return VScroll(Column([
            self._section(
                _("Users"), user_rows,
                subtitle=_("Each user has its own servers and device "
                           "identity; a locked user needs a PIN to switch "
                           "to.")),
            self._section(
                # Servers are scoped to the active user, so name the section
                # after them — otherwise removing one looks global.
                _("Servers for %s") % active if active else _("Servers"),
                server_rows),
        ], pad=self.CONTENT_PAD, gap=14, align="stretch"),
            id="settings-servers", flex=1)

    def _user_row(self, u, i, can_delete):
        """One Grid row spec for the Users list (cells share the Grid's
        tracks; the trailing button set varies per row)."""
        buttons = []
        if not u.get("active"):
            buttons.append(Button(_("Switch"), id="su-sw-%d" % i,
                                  on_click=lambda: self._switch_user(u)))
        buttons.append(Button(
            _("Change PIN") if u.get("locked") else _("Set PIN"),
            id="su-pin-%d" % i, icon="lock",
            on_click=lambda: self._open_pin_setup(u)))
        buttons.append(Button(_("Rename"), id="su-rn-%d" % i,
                              on_click=lambda: self._open_rename_user(u)))
        if can_delete and not u.get("active"):
            buttons.append(Button(
                _("Delete"), id="su-del-%d" % i, icon="delete",
                on_click=lambda: self._confirm(
                    _("Delete user %s and its saved logins?")
                    % u.get("name", ""),
                    lambda: self._delete_user(u),
                    title=_("Delete User"), yes=_("Delete"))))
        return {
            "id": "su-%d" % i,
            "bg": theme.PANEL_BG,
            "radius": 6,
            "cells": [
                Icon("lock" if u.get("locked") else "person", 18),
                Text(u.get("name", "?"), size=17, bold=True, flex=1),
                Text(_("active") if u.get("active") else "", size=14,
                     color=theme.OK_GREEN),
                Row(buttons, gap=8),
            ],
        }

    def _server_row(self, sv, i):
        connected = sv.get("connected")
        return {
            "id": "sv-%d" % i,
            "bg": theme.PANEL_BG,
            "radius": 6,
            "cells": [
                Icon("radio", 16,
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Column([Text(sv.get("name", "?"), size=17, bold=True),
                        Text(sv.get("address", ""), size=13,
                             color=theme.SUBTLE_FG)], gap=1, flex=1),
                Text(sv.get("username", ""), size=15,
                     color=theme.SUBTLE_FG),
                Text(_("Connected") if connected else _("Offline"),
                     size=15,
                     color=theme.OK_GREEN if connected else theme.FAV_RED),
                Button(_("Remove"), id="sv-rm-%d" % i, icon="delete",
                       size=15,
                       on_click=lambda u=sv.get("uuid"), n=sv.get("name"):
                           self._confirm(
                               _("Remove %s and its saved login?") % n,
                               lambda: self._remove_server(u),
                               title=_("Remove Server"), yes=_("Remove"))),
            ],
        }

    def _remove_server(self, uuid):
        if self.controller is None:
            return
        self._pool.submit(lambda: self._safe(
            lambda c: (c.remove_server(uuid), self._after_users_changed())))

    def _add_user(self, name):
        name = (name or "").strip()
        if not name or self.controller is None:
            return
        self._safe(lambda c: c.add_user(name))
        self._newuser["name"] = ""
        self._after_users_changed()

    def _delete_user(self, u):
        if self.controller is None:
            return
        ok, err = (False, None)
        try:
            ok, err = self.controller.delete_user(u.get("id"))
        except Exception:
            log.error("delete_user failed", exc_info=True)
        if not ok and err:
            self._message(err)
        self._after_users_changed()

    def _open_rename_user(self, u):
        state = {"name": u.get("name", "")}

        def build():
            return Dialog("renameuser", self._dialog_shell("renameuser", [
                Text(_("Rename User"), size=22, bold=True),
                TextBox("ru-name", text=state["name"], w=280, force=True,
                        on_change=lambda v: state.__setitem__("name", v),
                        on_submit=lambda v: save()),
                self._dialog_buttons([
                    Button(_("Cancel"), id="ru-cancel",
                           on_click=self._close_dialog),
                    Button(_("Rename"), id="ru-ok", on_click=save)]),
            ]), on_dismiss=self._close_dialog)

        def save():
            name = (state["name"] or "").strip()
            if name:
                self._safe(lambda c: c.rename_user(u.get("id"), name))
            self._close_dialog()
            self._after_users_changed()
        self._show_dialog(build)

    def _open_pin_setup(self, u):
        state = {"cur": "", "new": "", "confirm": "", "startup": False,
                 "error": None}

        def build():
            rows = [Text(_("Set PIN for %s") % u.get("name", ""), size=22,
                         bold=True)]
            if state["error"]:
                rows.append(Text(state["error"], size=15, color=theme.FAV_RED))
            if u.get("locked"):
                rows.append(self._pin_field(_("Current PIN"), "ps-cur", state,
                                            "cur"))
            rows += [
                self._pin_field(_("New PIN"), "ps-new", state, "new"),
                self._pin_field(_("Confirm"), "ps-confirm", state, "confirm"),
                Checkbox(_("Require this PIN at startup"), state["startup"],
                         id="ps-startup",
                         on_toggle=lambda: (state.__setitem__(
                             "startup", not state["startup"]),
                             self._show_dialog(build))),
                Row([
                    Button(_("Remove PIN"), id="ps-remove",
                           on_click=lambda: save(remove=True))
                    if u.get("locked") else Spacer(h=0),
                    Spacer(),
                    Button(_("Cancel"), id="ps-cancel",
                           on_click=self._close_dialog),
                    Button(_("Save"), id="ps-ok", on_click=save),
                ], gap=10, align="center", justify="end"),
            ]
            return Dialog("pinsetup",
                          self._dialog_shell("pinsetup", rows, w=460),
                          on_dismiss=self._close_dialog)

        def save(remove=False):
            if self.controller is None:
                return self._close_dialog()
            if u.get("locked"):
                try:
                    if not self.controller.unlock_user(u.get("id"),
                                                       state["cur"]):
                        state["error"] = _("Current PIN is incorrect.")
                        return self._show_dialog(build)
                except Exception:
                    log.debug("pin verify failed", exc_info=True)
            if not remove and not state["new"]:
                # Empty new+confirm compared equal and fell through to
                # set_pin(None), i.e. Save on a "Set PIN" dialog quietly
                # removed the lock.
                state["error"] = _("Enter a new PIN.")
                return self._show_dialog(build)
            if not remove and state["new"] != state["confirm"]:
                state["error"] = _("The PINs don't match.")
                return self._show_dialog(build)
            self._safe(lambda c: c.set_user_pin(
                u.get("id"), None if remove else state["new"],
                require_startup=state["startup"]))
            self._close_dialog()
            self._after_users_changed()
        self._show_dialog(build)

    @staticmethod
    def _pin_field(label, node_id, state, key):
        return Row([Text(label, w=140, size=16, color=theme.SUBTLE_FG),
                    TextBox(node_id, placeholder=label, mask=True, w=200,
                            on_change=lambda v: state.__setitem__(key, v))],
                   gap=10, align="center")

    def _after_users_changed(self):
        self.invalidate()

    # -- Downloads --------------------------------------------------------

    def _section(self, title, children, subtitle=None):
        """A full-width titled card. Settings panels are forms, not tile
        grids — they should span the pane rather than sit in a ragged
        left-aligned column."""
        head = [Text(title, size=20, bold=True)]
        if subtitle:
            head.append(Text(subtitle, size=14, color=theme.SUBTLE_FG,
                             wrap=True))
        return Column(head + children, pad=14, gap=8, bg=theme.CARD_BG,
                      radius=10, align="stretch")

    INDENT = 26   # per hierarchy level in the downloads tree

    def _settings_downloads(self, route, size):
        groups = route.get("_downloads")
        if groups is None:
            self._load_downloads(route)
            return self._busy()
        total = sum(g.get("size", 0) or 0 for g in groups)
        count = sum(g.get("count", 0) or 0 for g in groups)
        head = Row([
            Text(_("Downloads"), size=20, bold=True),
            Text(_("%(count)d items · %(size)s") % {
                "count": count, "size": self._human_size(total)},
                size=15, color=theme.SUBTLE_FG),
            Spacer(),
            Button(_("Refresh"), id="dl-refresh", icon="refresh",
                   on_click=lambda: self._load_downloads(route, force=True)),
        ], gap=12, align="center")
        rows = [head]
        if not groups:
            rows.append(Text(_("Nothing downloaded yet."), size=16,
                             color=theme.SUBTLE_FG))
        for gi, group in enumerate(groups):
            rows.append(self._dl_group(route, group, gi))
        self._poll_downloads(route)
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=10,
                              align="stretch"),
                       id="settings-downloads", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "settings-downloads", off, mx))

    def _dl_row(self, node_id, title, meta, depth, on_delete, bold=False,
                icon=None, count=None, route=None, toggle=None,
                expanded=True, on_delete_watched=None):
        """One Grid row spec of the downloads tree. Indentation carries
        the level (inside the title cell, so the meta/Remove tracks stay
        shared across every depth); every level gets its own delete so a
        whole show can go at once. ``toggle`` (a collapse-state key)
        adds a disclosure chevron before the title."""
        title_cell = [Spacer(w=depth * self.INDENT, h=1)]
        if toggle is not None:
            title_cell.append(Box(
                [Icon("keyboard_arrow_down" if expanded
                      else "chevron_right", 16, color=theme.SUBTLE_FG)],
                id=node_id + "-tgl", pad=3, radius=4, direction="row",
                align="center", hover={"fill": theme.BUTTON_BG},
                on_click=lambda: self._dl_toggle(route, toggle)))
        else:
            # rows without a disclosure still reserve its gutter, so
            # titles stay monotonically indented down the tree
            title_cell.append(Spacer(w=22, h=1))
        if icon:
            title_cell.append(Icon(icon, 16, color=theme.SUBTLE_FG))
        title_cell.append(Text(title, size=17 if bold else 16, bold=bold))
        if count:
            # Collapsed groups (playlists) say how much they stand for.
            title_cell.append(Text(_("%d items") % count, size=14,
                                   color=theme.SUBTLE_FG))
        title_cell.append(Spacer())
        return {
            "id": node_id,
            "bg": theme.PANEL_BG if depth == 0 else None,
            "radius": 6,
            "cells": [
                Row(title_cell, gap=10, align="center", flex=1),
                Text(meta, size=14, color=theme.SUBTLE_FG,
                     align="right"),
                Row(([Button(_("Remove Watched"), id=node_id + "-rmw",
                             icon="check", size=15,
                             on_click=on_delete_watched)]
                     if on_delete_watched else []) +
                    [Button(_("Remove"), id=node_id + "-rm", icon="delete",
                            size=15, on_click=on_delete)],
                    gap=6, align="center"),
            ],
        }

    def _dl_toggle(self, route, key):
        route.setdefault(
            "_dl_collapsed", set()).symmetric_difference_update({key})
        self.invalidate()

    @staticmethod
    def _dl_key(entry, fallback):
        # stable across refreshes (ids); position only as a last resort
        return str(entry.get("id") or entry.get("title") or fallback)

    def _dl_group(self, route, group, gi):
        collapsed = route.get("_dl_collapsed") or set()
        kind = group.get("kind")
        children = group.get("children") or []
        gkey = self._dl_key(group, gi)
        g_open = gkey not in collapsed
        rows = [self._dl_row(
            "dl-g%d" % gi, group.get("title", "?"),
            self._human_size(group.get("size", 0)), 0,
            self._dl_delete_cb(
                route, group,
                series_id=group.get("id") if kind == "series" else None,
                playlist_id=group.get("id") if kind == "playlist" else None,
                # Groups without a server-side id (the flat "Movies &
                # Videos" bucket) delete their own rows explicitly. Passing
                # no scope at all used to reach syncManager.delete() with
                # every id None, which deleted the ENTIRE catalog behind a
                # prompt naming only this group.
                item_ids=(None if kind in ("series", "playlist")
                          else self._dl_group_item_ids(group))),
            bold=True, count=group.get("count"),
            icon={"movies": "movie", "playlist": "queue_music"}.get(kind),
            route=route, toggle=gkey if children else None,
            expanded=g_open,
            # Reclaim space on a finished show without losing what's
            # unwatched — the Tk browser's gesture.
            on_delete_watched=(
                self._dl_delete_cb(
                    route, group, watched_only=True,
                    series_id=group.get("id") if kind == "series" else None,
                    playlist_id=(group.get("id") if kind == "playlist"
                                 else None),
                    item_ids=(None if kind in ("series", "playlist")
                              else self._dl_group_item_ids(group)))
                if kind in ("series", "playlist") else None))]
        for ci, child in enumerate(children if g_open else []):
            if child.get("kind") == "season":
                skey = self._dl_key(child, "%d.%d" % (gi, ci))
                s_open = skey not in collapsed
                eps = child.get("children") or []
                rows.append(self._dl_row(
                    "dl-g%d-s%d" % (gi, ci), child.get("title", "?"),
                    self._human_size(child.get("size", 0)), 1,
                    self._dl_delete_cb(route, child,
                                       season_id=child.get("id")),
                    route=route, toggle=skey if eps else None,
                    expanded=s_open))
                for ei, ep in enumerate(eps if s_open else []):
                    rows.append(self._dl_item_row(
                        route, ep, "dl-g%d-s%d-e%d" % (gi, ci, ei), 2))
            else:
                rows.append(self._dl_item_row(
                    route, child, "dl-g%d-i%d" % (gi, ci), 1))
        return Grid(rows,
                    cols=[{"flex": 1}, {"w": 200, "align": "right"},
                          {"align": "right"}],
                    gap=10, row_gap=2, row_pad=6)

    def _dl_item_row(self, route, item, node_id, depth):
        num = item.get("index")
        title = ("%s. %s" % (num, item.get("title", ""))
                 if num is not None else item.get("title", ""))
        status = item.get("status") or ""
        meta = "   ".join(x for x in (
            status if status != "complete" else "",
            self._human_size(item.get("size", 0))) if x)
        return self._dl_row(node_id, title, meta, depth,
                            self._dl_delete_cb(route, item,
                                               item_id=item.get("id")))

    @staticmethod
    def _dl_group_item_ids(group):
        """Every download id under a group, including nested season rows."""
        out = []
        for child in group.get("children") or ():
            if child.get("kind") == "season":
                out += [g.get("id") for g in child.get("children") or ()]
            elif child.get("id"):
                out.append(child["id"])
        return [i for i in out if i]

    def _dl_delete_cb(self, route, entry, item_id=None, series_id=None,
                      season_id=None, playlist_id=None, item_ids=None,
                      watched_only=False):
        def go():
            self._confirm(
                (_("Delete the watched downloads in %s?") if watched_only
                 else _("Delete the downloaded copy of %s?"))
                % entry.get("title", ""),
                lambda: self._delete_download(route, item_id=item_id,
                                              series_id=series_id,
                                              season_id=season_id,
                                              playlist_id=playlist_id,
                                              item_ids=item_ids,
                                              watched_only=watched_only),
                title=_("Delete Download"), yes=_("Delete"))
        return go

    # How often the downloads view re-reads the catalog while work is
    # outstanding. Downloads land asynchronously, so a static list is stale
    # the moment it renders.
    DL_POLL_SECS = 3.0

    def _poll_downloads(self, route):
        if self.controller is None or self._dl_thread is not None:
            return

        def tick():
            try:
                while not self._np_stop.wait(self.DL_POLL_SECS):
                    if (self.route is not route
                            or route.get("_tab") != "downloads"
                            or not self._browsing):
                        break
                    try:
                        pending, _total = self.controller.download_activity()
                    except Exception:
                        break
                    if not pending:
                        break     # nothing in flight; the list can't change
                    self._load_downloads(route, force=True)
            finally:
                self._dl_thread = None

        self._dl_thread = threading.Thread(target=tick, daemon=True,
                                           name="mpvtk-dl-poll")
        self._dl_thread.start()

    def _load_downloads(self, route, force=False):
        if self.controller is None:
            route["_downloads"] = []
            return
        if route.get("_dl_loading") and not force:
            return
        route["_dl_loading"] = True
        ep = self._epoch

        def work():
            return self.controller.list_downloads()

        def done(rows):
            route["_downloads"] = rows or []
            route["_dl_loading"] = False
        self.run_async(work, done, ep)

    def _delete_download(self, route, item_id=None, series_id=None,
                         season_id=None, playlist_id=None, item_ids=None,
                         watched_only=False):
        """Delete, then re-read the catalog — in that order, on one worker.

        Submitting the delete and the reload as separate tasks raced: the
        reload could read the catalog before the delete had touched it, and
        the row came straight back."""
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            try:
                if item_ids is not None and not watched_only:
                    for one in item_ids:
                        self.controller.delete_download(item_id=one)
                else:
                    self.controller.delete_download(
                        item_id=item_id, series_id=series_id,
                        season_id=season_id, playlist_id=playlist_id,
                        watched_only=watched_only)
            except Exception:
                log.error("delete_download failed", exc_info=True)
            return self.controller.list_downloads()

        def done(rows):
            route["_downloads"] = rows or []
            route["_dl_loading"] = False
        self.run_async(work, done, ep)
        self._refresh_downloaded()

    # -- Logs -------------------------------------------------------------

    def _settings_logs(self, route, size):
        lines = []
        if self.controller is not None:
            try:
                lines = self.controller.recent_logs()
            except Exception:
                log.debug("recent_logs failed", exc_info=True)
        rows = [Row([Text(_("Logs"), size=20, bold=True), Spacer(),
                     Button(_("Refresh"), id="log-refresh", icon="refresh",
                            on_click=self.invalidate),
                     Button(_("Open Config Folder"), id="log-conf",
                            icon="folder",
                            on_click=self._open_config_folder)],
                    gap=8, align="center")]
        if not lines:
            rows.append(Text(_("No log output captured yet."), size=15,
                             color=theme.SUBTLE_FG))
        # Newest last, like a console; the scroll keeps its offset across
        # rebuilds so following the tail is a matter of staying at the bottom.
        for i, line in enumerate(lines[-500:]):
            rows.append(Text(line, size=14, color=theme.SUBTLE_FG,
                             id="log-%d" % i))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=2),
                       id="settings-logs", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "settings-logs", off, mx))

    def _open_config_folder(self):
        self._client_call(lambda c: c.open_config_folder())

    # --------------------------------------------------------------- queue

    def _render_queue(self, route, size):
        """The play queue, deliberately the same table + toolbar as the
        playlist editor: the two do the same job on the same kind of list."""
        data = route.get("_data")
        if data is None:
            return self._busy()
        entries = data.get("entries") or []
        current = data.get("current_id")
        sel = self._pe_sel(route)
        n = len(entries)
        toolbar = Row([
            Text(_("Play Queue"), size=26, bold=True), Spacer(),
            Button(_("Top"), id="q-top", icon="vertical_align_top",
                   on_click=lambda: self._queue_move(route, "top")),
            Button(_("Up"), id="q-up", icon="keyboard_arrow_up",
                   on_click=lambda: self._queue_move(route, "up")),
            Button(_("Down"), id="q-down", icon="keyboard_arrow_down",
                   on_click=lambda: self._queue_move(route, "down")),
            Button(_("Bottom"), id="q-bottom", icon="vertical_align_bottom",
                   on_click=lambda: self._queue_move(route, "bottom")),
            Text(_("%d selected") % len(sel) if sel else "", size=15,
                 color=theme.SUBTLE_FG),
            Button(_("Select All"), id="q-all",
                   on_click=lambda: self._pe_set_sel(route, set(range(n)))),
            Button(_("Clear"), id="q-none",
                   on_click=lambda: self._pe_set_sel(route, set())),
            Button(_("Remove"), id="q-remove", icon="delete",
                   on_click=lambda: self._queue_remove_selected(route)),
        ], gap=8, align="center")
        rows = [toolbar, Spacer(h=2)]
        if not entries:
            rows.append(Text(_("The queue is empty."), size=18,
                             color=theme.SUBTLE_FG))
        else:
            rows.append(self._track_list(
                [e["item"] for e in entries], "q",
                lambda i: self._queue_skip(entries[i].get("pid")),
                playing_id=current, selected=sel, scroll_id="queue",
                head_h=60,
                on_select=lambda i, mods: self._select_click(route, i, mods)))
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                          align="stretch"), id="queue",
                       flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "queue", off, mx))

    def _queue_remove_selected(self, route):
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        sel = sorted(self._pe_sel(route))
        pids = [entries[i].get("pid") for i in sel
                if i < len(entries) and entries[i].get("pid")]
        if not pids:
            return
        route["_sel"] = set()
        if self.controller is not None:
            self._safe(lambda c: c.queue_remove(pids))
        self.route.pop("_data", None)
        self._bump_epoch()
        self._load_route(self.route)
        self.invalidate()

    def _queue_select(self, route, i, mods=None):
        self._select_click(route, i, mods)

    @staticmethod
    def _block_move(items, sel, where):
        """Move the selected indices as one block. Returns (items, new_sel)
        or None when nothing moves. Shared by the queue and the playlist
        editor so the two behave identically."""
        sel = sorted(sel)
        if not sel or not items:
            return None
        n = len(items)
        target = {"top": 0, "bottom": n - len(sel),
                  "up": max(0, sel[0] - 1),
                  "down": min(n - len(sel), sel[0] + 1)}[where]
        if target == sel[0]:
            return None
        block = [items[i] for i in sel]
        rest = [it for i, it in enumerate(items) if i not in set(sel)]
        return (rest[:target] + block + rest[target:],
                set(range(target, target + len(block))))

    def _queue_move(self, route, where):
        data = route.get("_data") or {}
        entries = data.get("entries") or []
        moved = self._block_move(entries, self._pe_sel(route), where)
        if moved is None:
            return
        data["entries"], route["_sel"] = moved
        self._client_call(lambda c: c.queue_reorder(
            [e["pid"] for e in data["entries"] if e.get("pid")]))
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
                Button(_("Configure Servers"), id="banner-servers",
                       on_click=self.show_login),
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
            ], pad=10, gap=10, align="center", h=48, bg=theme.ACCENT_SOFT)
        return None

    # -- download status bar ----------------------------------------------

    def _download_bar(self):
        """A persistent bar while downloads are outstanding, with a way into
        the manager. Downloads are otherwise completely invisible once the
        confirm dialog closes."""
        st = self._dl_status
        if not st or not st.get("pending"):
            return None
        name = st.get("name") or ""
        pct = st.get("percent")
        left = (_("Downloading %(name)s — %(n)d remaining")
                if name else _("Downloading — %(n)d remaining")) % {
            "name": name, "n": st["pending"]}
        row = [Icon("file_download", 20), Text(left, size=16)]
        if pct is not None:
            row.append(Progress(pct / 100.0, w=160))
            row.append(Text("%d%%" % pct, size=15, w=48,
                            color=theme.SUBTLE_FG))
        row += [
            Spacer(),
            Button(_("View Downloads"), id="dlbar-view",
                   on_click=lambda: self.open_settings("downloads")),
        ]
        return Row(row, pad=10, gap=10, align="center", h=44,
                   bg=theme.PANEL_BG)

    def set_download_status(self, status):
        """``{"pending": int, "name": str, "percent": int|None}`` — pushed by
        the sync manager's progress hook."""
        if status == self._dl_status:
            return
        self._dl_status = status
        self.invalidate()

    def _poll_download_status(self):
        """Keep the status bar current. The sync manager has no push hook the
        browser can subscribe to, so poll it — cheaply, and only while there
        is something to report or the browser is on screen."""
        if self.controller is None or self._dlbar_thread is not None:
            return

        def tick():
            try:
                while not self._np_stop.wait(2.0):
                    if not self._browsing:
                        continue
                    try:
                        st = self.controller.download_status()
                    except Exception:
                        break
                    self.set_download_status(st)
            finally:
                self._dlbar_thread = None

        self._dlbar_thread = threading.Thread(target=tick, daemon=True,
                                              name="mpvtk-dlbar")
        self._dlbar_thread.start()

    def _dismiss_update(self):
        self._update = None
        self.invalidate()

    def _open_url(self, url):
        if self.controller is not None and url:
            self._safe(lambda c: c.open_url(url))
        self._dismiss_update()

    def _retry_connect(self):
        """Offline banner → Retry. A reconnect that works has to swap the
        source in, or the banner clears while the catalog is still what's
        being browsed."""
        if self.controller is None:
            return
        ep = self._epoch

        def work():
            # not _safe(): that swallows the return value, and the source is
            # the whole point here.
            try:
                return self.controller.retry_connect()
            except Exception:
                log.warning("retry connect failed", exc_info=True)
                return None

        def done(source):
            if source is not None:
                self.set_source(source)
        self.run_async(work, done, ep)

    # --------------------------------------------------------- playlist edit

    @staticmethod
    def _pe_title(item):
        """Series-aware entry title, like the Tk editor's: an episode reads
        "Show — S02E05 · Title" so a 300-row playlist is navigable."""
        name = item.get("Name", "")
        if item.get("Type") == "Episode":
            s, e = item.get("ParentIndexNumber"), item.get("IndexNumber")
            se = ("S%sE%s" % (s, e)) if s is not None and e is not None else ""
            parts = [p for p in (item.get("SeriesName"), se) if p]
            if parts:
                return "%s · %s" % (" — ".join(parts), name)
        artists = item.get("Artists") or []
        if artists:
            return "%s — %s" % (", ".join(artists), name)
        return name

    def _pe_sel(self, route):
        """Selected row indices as a set (multi-select)."""
        return set(route.get("_sel") or ())

    def _render_playlist_edit(self, route, size):
        items = route.get("_items")
        if items is None:
            return self._busy()
        sel = self._pe_sel(route)
        n = len(items)
        toolbar = Row([
            Button(_("Top"), id="pe-top", icon="vertical_align_top",
                   on_click=lambda: self._pe_move(route, "top")),
            Button(_("Up"), id="pe-up", icon="keyboard_arrow_up",
                   on_click=lambda: self._pe_move(route, "up")),
            Button(_("Down"), id="pe-down", icon="keyboard_arrow_down",
                   on_click=lambda: self._pe_move(route, "down")),
            Button(_("Bottom"), id="pe-bottom", icon="vertical_align_bottom",
                   on_click=lambda: self._pe_move(route, "bottom")),
            Spacer(),
            Text(_("%d selected") % len(sel) if sel else "", size=15,
                 color=theme.SUBTLE_FG),
            Button(_("Select All"), id="pe-all",
                   on_click=lambda: self._pe_set_sel(route, set(range(n)))),
            Button(_("Clear"), id="pe-none",
                   on_click=lambda: self._pe_set_sel(route, set())),
            Button(_("Remove"), id="pe-remove", icon="delete",
                   on_click=lambda: self._pe_remove(route)),
        ], gap=8, align="center")
        rename_row = Row([
            TextBox("pe-name", text=route.get("title", ""), w=280,
                    on_change=lambda v: route.__setitem__("_newname", v),
                    on_submit=lambda v: self._pe_rename(route)),
            Button(_("Rename"), id="pe-rename", icon="edit",
                   on_click=lambda: self._pe_rename(route)),
            Checkbox(_("Public"), bool(route.get("_public")), id="pe-public",
                     on_toggle=lambda: self._pe_toggle_public(route)),
            Spacer(),
            Button(_("Delete Playlist"), id="pe-delete", icon="delete",
                   on_click=lambda: self._confirm(
                       _("Delete the playlist %s?") % route.get("title", ""),
                       lambda: self._pe_delete(route),
                       title=_("Delete Playlist"), yes=_("Delete"))),
        ], gap=10, align="center")
        table = Table(
            [{"label": "#", "w": 46, "align": "right"},
             {"label": _("Title"), "flex": 3},
             {"label": _("Type"), "w": 120},
             {"label": _("Time"), "w": 80, "align": "right"}],
            [{"id": "pe-row-%d" % i,
              "selected": i in sel,
              "cells": [str(i + 1), self._pe_title(it),
                        it.get("Type", ""), self._duration(it)],
              # A one-parameter handler opts into the click modifiers, which
              # is what makes shift-range selection possible.
              "on_click": (lambda mods, i=i: self._select_click(
                  route, i, mods))}
             for i, it in enumerate(items)],
            size=17, row_h=34, hover_bg=theme.BUTTON_BG)
        rows = [Text("%s — %s" % (route.get("title", ""), _("Edit")),
                     size=26, bold=True), Spacer(h=4), rename_row, toolbar,
                Spacer(h=2), table]
        return VScroll(Column(rows, pad=self.CONTENT_PAD, gap=8,
                              align="stretch"),
                       id="playlist-edit", flex=1,
                       on_scroll=lambda off, mx: self._on_scroll(
                           "playlist-edit", off, mx))

    def _pe_set_sel(self, route, sel, anchor=None):
        route["_sel"] = set(sel)
        if anchor is not None:
            route["_anchor"] = anchor
        self.invalidate()

    def _select_click(self, route, i, mods):
        """Standard list selection semantics against ``route["_sel"]``.

        - plain click: select just this row, and make it the anchor
        - shift-click: select the whole range from the anchor to here, so two
          clicks pick any run of rows
        - ctrl-click: toggle this row, keeping the rest

        ``mods`` comes from the renderer's click payload (mpvtk carries
        shift/ctrl for handlers that declare a parameter)."""
        mods = mods or {}
        sel = self._pe_sel(route)
        anchor = route.get("_anchor")
        if mods.get("shift") and anchor is not None:
            lo, hi = (anchor, i) if anchor <= i else (i, anchor)
            self._pe_set_sel(route, set(range(lo, hi + 1)))
        elif mods.get("ctrl"):
            sel.symmetric_difference_update({i})
            self._pe_set_sel(route, sel, anchor=i)
        else:
            self._pe_set_sel(route, {i}, anchor=i)

    def _pe_move(self, route, where):
        """Move the whole selection as a block, preserving its internal
        order — moving 20 rows should not require 20 clicks."""
        items = route.get("_items") or []
        sel = sorted(self._pe_sel(route))
        moved = self._block_move(items, sel, where)
        if moved is None:
            return
        route["_items"], route["_sel"] = moved
        target = min(route["_sel"])
        server = route.get("server") or self.server
        pid = route["item_id"]
        for offset, entry in enumerate([items[i] for i in sel]):
            self._client_call(
                lambda c, e=entry, o=offset: c.playlist_move(
                    server, pid, e.get("PlaylistItemId"), target + o))
        self.invalidate()

    def _pe_remove(self, route):
        items = route.get("_items") or []
        sel = sorted(self._pe_sel(route))
        if not sel:
            return
        entries = [items[i] for i in sel if i < len(items)]
        route["_items"] = [it for i, it in enumerate(items)
                           if i not in set(sel)]
        route["_sel"] = set()
        ids = [e.get("PlaylistItemId") for e in entries
               if e.get("PlaylistItemId")]
        if ids:
            self._client_call(lambda c: c.playlist_remove(
                route.get("server") or self.server, route["item_id"], ids))
        self.invalidate()

    def _pe_delete(self, route):
        pid = route["item_id"]
        self._client_call(lambda c: c.playlist_delete(
            route.get("server") or self.server, pid))
        self.after_playlist_deleted(pid)

    def _pe_rename(self, route):
        name = (route.get("_newname") or route.get("title") or "").strip()
        if not name:
            return
        route["title"] = name
        self._client_call(lambda c: c.playlist_update(
            route.get("server") or self.server, route["item_id"], name=name))
        self.invalidate()

    def _pe_toggle_public(self, route):
        # Refuse until the loader has read the server's OpenAccess: flipping a
        # value we never read could make a public playlist private (or worse,
        # the reverse) on the very first click.
        if not route.get("_public_known"):
            self._message(_("Still reading this playlist's visibility from "
                            "the server. Try again in a moment."))
            return
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
            def fetch(fn):
                try:
                    return fn(server)
                except Exception:
                    return []
            return (fetch(self.source.get_playlists),
                    fetch(getattr(self.source, "get_collections",
                                  lambda _s: [])))
        self.run_async(
            work, lambda r: self._show_add_to(server, item, r[0], r[1]), ep)

    def _show_add_to(self, server, item, playlists, collections=()):
        item_id = item.get("Id")
        # Private by default, matching the Tk browser: the server creates
        # playlists public unless told otherwise.
        self._addto_name = {"name": "", "private": True}

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
            rows.append(Checkbox(
                _("Private (only you can see it)"),
                bool(self._addto_name.get("private")), id="add-private",
                on_toggle=lambda: self._addto_name.__setitem__(
                    "private", not self._addto_name.get("private"))))
            if collections:
                rows.append(Spacer(h=6))
                rows.append(Text(_("Add to Collection"), size=18, bold=True))
                for i, col in enumerate(collections):
                    rows.append(Button(
                        col.get("Name", ""), id="add-col-%d" % i,
                        on_click=lambda cid=col.get("Id"): self._add_to_col(
                            server, cid, item_id)))
            rows.append(self._dialog_buttons([
                Button(_("Close"), id="add-close",
                       on_click=self._close_dialog)]))
            return Dialog("addto",
                          self._dialog_shell("addto", rows, w=460),
                          on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _add_to_new(self, server, item_id):
        state = self._addto_name or {}
        name = state.get("name", "").strip()
        if name and item_id:
            private = bool(state.get("private", True))
            self._client_call(lambda c: c.playlist_new(
                server, name, [item_id], is_public=not private))
        self._close_dialog()

    def _add_to_col(self, server, collection_id, item_id):
        if collection_id and item_id:
            self._client_call(lambda c: c.collection_add(
                server, collection_id, [item_id]))
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
                self._dialog_buttons([
                    Button(_("Cancel"), id="dl-cancel",
                           on_click=self._close_download),
                    Button(_("Download"), id="dl-ok",
                           on_click=self._dl_confirm)]),
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
        # align="stretch" so button rows fill the shell's width; without it
        # they take their natural width and a trailing flex Spacer has no
        # leftover to absorb, which left the buttons hugging the left edge.
        return Column(children, pad=24, gap=16, bg="1e1e1e", radius=12,
                      border="555555", w=w, align="stretch")

    @staticmethod
    def _dialog_buttons(children):
        """Dialog action row: always trailing-aligned."""
        return Row(children, gap=10, justify="end")

    def _message(self, text, title=None):
        title = title or _("Notice")

        def build():
            return Dialog("msg", self._dialog_shell("msg", [
                Text(title, size=22, bold=True),
                Text(text, size=16, color=theme.SUBTLE_FG),
                self._dialog_buttons([
                    Button(_("OK"), id="dlg-ok",
                           on_click=self._close_dialog)]),
            ]), on_dismiss=self._close_dialog)
        self._show_dialog(build)

    def _confirm(self, text, on_yes, title=None, yes=None):
        title = title or _("Confirm")
        yes = yes or _("OK")

        def build():
            return Dialog("confirm", self._dialog_shell("confirm", [
                Text(title, size=22, bold=True),
                Text(text, size=16, color=theme.SUBTLE_FG),
                self._dialog_buttons([
                    Button(_("Cancel"), id="dlg-cancel",
                           on_click=self._close_dialog),
                    Button(yes, id="dlg-ok",
                           on_click=lambda: (self._close_dialog(),
                                             on_yes()))]),
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
            ], gap=10, align="center"))
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
        """Show the add-server / login screen.

        Only resets the nav stack when there is nowhere to go back *to*: with
        servers already connected this is "add another", and cancelling has
        to return you to the library rather than trapping you on the form.
        """
        route = {"kind": "login", "title": _("Add Server")}
        if self.server is None:
            self.navigate(route, reset=True)
        else:
            self.navigate(route)

    def _render_login(self, route, size):
        def field(fid, ph, key, mask=False):
            return Row([
                Text(ph, w=140, size=17, color=theme.SUBTLE_FG),
                TextBox(fid, text=self._login[key], placeholder=ph, mask=mask,
                        w=360,
                        on_change=lambda v, k=key: self._login.__setitem__(
                            k, v)),
            ], gap=12, align="center")

        qc = route.get("_qc")
        rows = [Text(_("Connect to Jellyfin"), size=28, bold=True)]
        if self._login_error:
            rows.append(Text(self._login_error, size=15, color=theme.FAV_RED))

        known = []
        if self.controller is not None and not qc:
            try:
                known = self.controller.known_servers() or []
            except Exception:
                known = []
        if known:
            rows.append(Text(_("Previously added servers"), size=15,
                             color=theme.SUBTLE_FG))
            for i, k in enumerate(known):
                addr = k.get("address", "")
                rows.append(Row([
                    Icon("radio", 16, color=theme.SUBTLE_FG),
                    Text(k.get("name") or addr, size=16, flex=1),
                    Button(_("Use"), id="login-known-%d" % i, size=15,
                           on_click=lambda a=addr: self._use_known_server(a)),
                ], id="login-known-row-%d" % i, pad=8, gap=10, radius=6,
                   align="center", bg=theme.PANEL_BG))

        if qc:
            # Quick Connect: the user types this code into any signed-in
            # Jellyfin client; we poll until the server authorizes it.
            rows += [
                Text(_("Quick Connect"), size=20, bold=True),
                Text(_("Enter this code in the Jellyfin app or web client:"),
                     size=15, color=theme.SUBTLE_FG, wrap=True, w=460),
                Text(qc.get("code") or _("Requesting…"), size=44, bold=True,
                     align="center"),
                Text(qc.get("status") or "", size=15,
                     color=theme.SUBTLE_FG, align="center"),
                self._dialog_buttons([
                    Button(_("Cancel"), id="login-qc-cancel",
                           on_click=lambda: self._cancel_quick_connect(route)),
                ]),
            ]
        else:
            rows += [
                field("login-server", _("Server URL"), "server"),
                field("login-user", _("Username"), "user"),
                field("login-pass", _("Password"), "pass", mask=True),
                Row([
                    Button(_("Use Quick Connect"), id="login-qc",
                           icon="radio",
                           on_click=lambda: self._start_quick_connect(route)),
                    Spacer(),
                    # Only offer Cancel when there's something to go back to;
                    # on a first run there is no library behind this screen.
                    Button(_("Cancel"), id="login-cancel",
                           on_click=self.go_back)
                    if len(self.nav_stack) > 1 else Spacer(h=0),
                    Button(_("Connect"), id="login-connect",
                           on_click=self._do_login),
                ], gap=10, align="center"),
            ]

        form = Column(rows, pad=28, gap=16, bg=theme.CARD_BG, radius=12,
                      border=theme.BORDER, w=560, align="stretch")
        return Box([Spacer(),
                    Row([Spacer(), form, Spacer()]),
                    Spacer()],
                   flex=1, direction="column", align="stretch", gap=10)

    def _use_known_server(self, address):
        self._login["server"] = address
        self.invalidate()

    def _start_quick_connect(self, route):
        server = (self._login.get("server") or "").strip()
        if not server:
            self._login_error = _("Enter the server URL first.")
            self.invalidate()
            return
        if self.controller is None:
            return
        self._login_error = None
        route["_qc"] = {"code": None, "status": _("Contacting the server…"),
                        "cancelled": False}
        self.invalidate()
        ep = self._epoch

        def on_code(code):
            qc = route.get("_qc")
            if qc is not None:
                qc["code"] = code
                qc["status"] = _("Waiting for approval…")
                self.invalidate()

        def work():
            return self.controller.quick_connect(
                server, on_code,
                lambda: (route.get("_qc") or {}).get("cancelled", True))

        def done(ok):
            if (route.get("_qc") or {}).get("cancelled"):
                return
            route.pop("_qc", None)
            if ok:
                self._login_error = None
                self._after_login()
            else:
                self._login_error = _("Quick Connect was not approved.")
        self.run_async(work, done, ep)

    def _cancel_quick_connect(self, route):
        qc = route.get("_qc")
        if qc is not None:
            qc["cancelled"] = True    # the worker polls this and gives up
        route.pop("_qc", None)
        self.invalidate()

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
        """Show the startup-PIN unlock gate.

        Idempotent: re-locking an already-locked UI must not wipe a PIN the
        user is halfway through typing (the tray can fire show/hide at any
        moment)."""
        if self._locked:
            return
        self._locked = True
        self._pin["pin"] = ""
        self._pin_error = None
        self.navigate({"kind": "locked", "title": _("Locked")}, reset=True)

    def maybe_relock(self):
        """Re-gate the UI behind the startup PIN when the window is released
        or re-surfaced. Unlocking once must not leave the client open for the
        rest of the process's life — closing to the tray and re-raising
        re-prompts, matching the Tk browser."""
        if self.controller is None:
            return
        try:
            if self.controller.needs_unlock():
                self.show_locked()
        except Exception:
            log.debug("relock check failed", exc_info=True)

    def _render_locked(self, route, size):
        """Startup PIN gate.

        A full page rather than a modal, and it offers the other local users
        — a locked user must not be able to lock the whole client out, which
        is what a bare PIN prompt with no way past it amounts to."""
        users = [u for u in self._users() if not u.get("active")]
        active = next((u.get("name") for u in self._users()
                       if u.get("active")), None)
        rows = [
            Text(_("Enter your PIN"), size=30, bold=True),
            Text(_("%s is locked.") % active if active else "",
                 size=16, color=theme.SUBTLE_FG),
        ]
        if self._pin_error:
            rows.append(Text(self._pin_error, size=15, color=theme.FAV_RED))
        rows += [
            TextBox("lock-pin", text="", placeholder=_("PIN"), mask=True,
                    w=260, on_change=lambda v: self._pin.__setitem__("pin", v),
                    on_submit=lambda v: self._do_unlock()),
            Row([Button(_("Unlock"), id="lock-unlock", icon="lock",
                        on_click=self._do_unlock)], gap=10, justify="end"),
        ]
        if users:
            rows.append(Spacer(h=6))
            rows.append(Text(_("Or switch to another user"), size=15,
                             color=theme.SUBTLE_FG))
            for i, u in enumerate(users):
                rows.append(Row([
                    Icon("lock" if u.get("locked") else "person", 18),
                    Text(u.get("name", "?"), size=17, flex=1),
                    Button(_("Switch"), id="lock-switch-%d" % i, size=15,
                           on_click=lambda u=u: self._switch_user(u)),
                ], id="lock-user-%d" % i, pad=8, gap=10, radius=6,
                   align="center", bg=theme.PANEL_BG,
                   hover={"fill": theme.BUTTON_BG}))
        form = Column(rows, pad=28, gap=14, bg=theme.CARD_BG, radius=12,
                      border=theme.BORDER, w=460, align="stretch")
        return Box([Spacer(), Row([Spacer(), form, Spacer()]), Spacer()],
                   flex=1, direction="column", align="stretch")

    def _do_unlock(self):
        if self.controller is None:
            return
        pin = self._pin.get("pin", "")
        ep = self._epoch

        def work():
            # False means the PIN was wrong; None means it was right but
            # nothing could be built (no server answered and nothing is
            # downloaded). Conflating the two reported a correct PIN as
            # incorrect — permanently so with work_offline on, since the
            # connect is skipped and there is never a live source.
            if not self.controller.unlock(pin):
                return False
            return self.controller.connect_and_rebuild()

        def done(source):
            if source is False:
                self._pin_error = _("Incorrect PIN.")
                return
            self._pin_error = None
            self._pin["pin"] = ""
            if source is None:
                self._locked = False
                self.show_login()
                return
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
        self._np_stop.set()   # also stops the downloads poller
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.thumbs is not None:
            self.thumbs.shutdown()
        self.strips.clear()
