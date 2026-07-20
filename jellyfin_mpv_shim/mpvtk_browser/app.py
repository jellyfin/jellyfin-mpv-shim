"""MpvtkBrowser — the app shell: route stack, async data loading, and the
``build(size)`` that turns the current route into an mpvtk widget tree.

This is the mpvtk analogue of the Tk ``BrowserApp``. It runs in the main
process next to ``playerManager`` (no ``multiprocessing`` child), attaches
its UI to the player's mpv window via ``mpvtk.MpvtkApp.attach`` (see
``mpvtk/MIGRATION.md``), and reproduces the load-bearing paradigms of the
Tk browser: a route-dict nav stack (``navigate``/``go_back``), background
API calls with epoch-guarded staleness, and full-scene rebuilds driven by
``invalidate()`` (renderer-local state — scroll, focus — survives).

This module is the *core*: ``__init__``, the nav stack, the epoch and
``run_async``, ``_load_route``, ``build``/``_render_route``, the chrome,
the browse<->playback lifecycle and HUD glue, and ``shutdown``. Everything
else is a mixin, one per feature area:

    dialogs.py     modal shell, add-to picker, download + SyncPlay dialogs
    auth.py        login / Quick Connect, lock screen, user switching
    settings.py    the Settings route and the downloads panel
    queue_edit.py  the play queue and the playlist editor
    music.py       music browsing and the now-playing bar
    views.py       home / grid / detail / series / season / search
    tiles.py       tile art, rows and grids, the tile context menu

The mixins are a partition, not a layering: they all operate on the same
``self``, so the split makes the shared state visible rather than reducing
it. No name may be defined by two of them — MRO would silently pick a
winner — and ``tests/test_mpvtk_browser_mixins.py`` enforces that.

**Adding a view** is one edit: declare the route kind in the owning mixin's
``ROUTES`` table as ``kind: (loader, renderer)``, and write those two
methods next to it. ``_routes()`` merges the tables across the MRO;
``_load_route`` and ``_render_route`` here are lookups. ``ROUTES`` is the
one name every mixin is meant to define — that merge is explicit, so the
usual override hazard doesn't apply, but a kind claimed twice is still a
test failure.

Three invariants hold the whole thing together:

**The thread contract.** Renderer event handlers and ``build()`` run on the
loop thread. ``on_playstate``, ``notify_update``, ``set_download_status``,
``display_item`` and ``on_downloads_changed`` are called from foreign
threads, as are the pool workers behind ``run_async`` — everything they
touch must be write-then-``invalidate()``, never a direct scene change.

**Epoch discipline.** ``_epoch`` and ``_lock`` live *only* here.
Dispatchers read ``ep = self._epoch`` on the loop thread and hand it to
``run_async``, which drops the result if navigation has moved on since.
Caching an ``ep`` and passing it across a module boundary reads fine and is
subtly wrong.

**``_lock`` protects writers from each other, not from the reader.**
``build()`` reads route data unlocked. That is deliberate and safe only
because every writer ends with ``invalidate()``, so a torn read is a
one-frame glitch that the next build heals. Don't "fix" it by locking
``build()``.
"""

import logging
import threading
import time

from concurrent.futures import ThreadPoolExecutor
from ..i18n import _
from ..mpvtk.layout import natural_size
from ..mpvtk.rawimage import cache_dir
from ..mpvtk.widgets import (
    Box,
    Button,
    Column,
    Dropdown,
    Float,
    Icon,
    Progress,
    Row,
    Spacer,
    Text,
    TextBox,
)
from . import theme
from .hud import build_hud
from .repository import FOLDER_TYPES, PLAYABLE_TYPES, SERIES_TYPES
from .strips import LANDSCAPE_GEOM, POSTER_GEOM, SQUARE_GEOM, StripStore
from .dialogs import DialogsMixin
from .auth import AuthMixin
from .settings import SettingsMixin
from .queue_edit import QueueEditMixin
from .music import MusicMixin
from .views import ViewsMixin, SORTS
from .tiles import TilesMixin

log = logging.getLogger("mpvtk_browser.app")

# Routes that take over the whole surface (no nav chrome), like the Tk
# browser's login/locked/connecting screens.
CHROME_FREE = {"login", "locked", "connecting"}


class MpvtkBrowser(DialogsMixin, AuthMixin, SettingsMixin, QueueEditMixin,
                   MusicMixin, ViewsMixin, TilesMixin):

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
        # Pending drag target on the now-playing bar's seek slider, in
        # seconds (None when not scrubbing) — the elapsed clock reads this
        # instead of the playhead. See MusicMixin._np_scrub_change.
        self._np_scrub = None
        # Set once, by shutdown(), and never cleared: it is the sleep every
        # background thread waits on, so clearing it would be a way to kill
        # them all. Guards the now-playing ticker, both download pollers and
        # the toast timer. (It was _np_stop, which read like the ticker's
        # own flag.)
        self._shutdown_evt = threading.Event()
        # Serialises the poller starters below. They are reachable from the
        # loop thread and from foreign ones, and "if the thread is None,
        # start it" is not atomic.
        self._poller_lock = threading.Lock()
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
        # Tail poller for the logs tab — see SettingsMixin._poll_logs.
        self._log_thread = None
        # Long job (currently only the download-folder move) — see _run_long.
        self._long_thread = None
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
        self._downloaded_seasons = set()
        self._downloaded_playlists = set()
        # Default to a file-backed store (works on both backends / headless);
        # the libmpv integration passes a MemoryStore-backed one.
        self.strips = strips or StripStore(
            cache_dir=cache_dir("mpvtk-browser-"), geom=self.geom)
        self.thumbs = thumbs      # ThumbnailStore (optional; None -> no art)
        if self.thumbs is not None:
            # Wake our loop when a decoded poster lands, so build() can pump it.
            self.thumbs.set_notify(self.invalidate)

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
        # apiclient edit-capability probe, resolved once
        self._edit_apis_ok = None
        # rebuilder for the add-to dialog (re-shown from its sub-dialog)
        self._addto_build = None
        self._addcol_name = None
        # ids the add-to dialog will post (a container resolves to many)
        self._addto_ids = None
        self._addto_explicit_ids = None
        # transient status message + when it was set (see _toast_node)
        self._status_at = 0.0
        self._toast_timer = None
        # live text of the download-folder field
        self._sync_path = {}
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
            left = self.nav_stack.pop()
            self._reset_scroll()
            self._bump_epoch()
            # Stale-while-revalidate: refresh Home on return (watched/resume
            # state may have changed) while showing the cached view meanwhile.
            if self.route.get("kind") == "home":
                self._load_route(self.route)
            # Coming out of the playlist editor, whatever is underneath is
            # showing the order and membership from before the edits.
            elif (left.get("kind") == "playlist_edit"
                  and self.route.get("kind") in ("playlist", "grid")):
                self.route.pop("_data", None)
                self.route.pop("_items", None)
                self.route.pop("_loading", None)
                self._load_route(self.route)
            self.invalidate()

    def after_playlist_deleted(self, playlist_id):
        """Drop every route pointing at a now-deleted playlist and reload
        whatever is left showing.

        A playlist page keys its id as ``item_id``; only ``parent_id`` was
        checked, so nothing was ever pruned and deleting a playlist left the
        user sitting on its now-dead page. The route we land on also has to
        re-fetch, or the grid we came from still lists the playlist."""
        self.nav_stack = [
            r for r in self.nav_stack
            if playlist_id not in (r.get("item_id"), r.get("parent_id"))
        ] or [{"kind": "home", "server": self.server}]
        route = self.route
        route.pop("_data", None)
        route.pop("_items", None)
        route.pop("_loading", None)
        self._bump_epoch()
        self._load_route(route)
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

    def run_async(self, work, on_done, epoch, on_error=None, always=None):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if the epoch still matches (the user hasn't navigated away). ``on_done``
        mutates state under the lock, then the loop is woken to rebuild.

        ``on_error(exc)`` runs when ``work()`` raises. Without one a failure
        only logs, which left the route's data at None and the view spinning
        forever — an unreachable server looked like a hang.

        **``on_error`` is deliberately not epoch-gated.** A rollback undoes an
        optimistic edit in the *route dict it captured*, or clears a paging
        guard — neither is a claim about what is currently on screen. Gating
        it meant navigating away before the failure landed dropped the
        rollback, so the route dict kept a change the server had refused and
        showed it again on the way back.

        That puts the burden on the handler: **anything in an ``on_error``
        that touches the live screen must check for itself.** Two do, both by
        testing ``route is self.route`` — ``_route_async`` before the offline
        fallback (``set_source`` discards the nav stack) and ``_page_more``
        before its toast. ``_edit_call``'s toast is deliberately unguarded:
        the user pressed a button and the server refused, so they should be
        told wherever they now are.

        ``always()`` runs after every outcome — success, failure, *and a
        result dropped because the epoch moved*. Use it for a guard that must
        not outlive the call. ``on_error`` alone is not enough: a stale
        success calls neither callback, so a flag cleared only in ``on_done``
        stays set forever (that was ``_page_more``'s ``_loading``, which
        silently killed infinite scroll for a route once you paged and then
        clicked into an item)."""
        def task():
            try:
                try:
                    result = work()
                except Exception as exc:
                    log.warning("async work failed", exc_info=True)
                    if on_error is None:
                        return
                    with self._lock:
                        try:
                            on_error(exc)
                        except Exception:
                            log.warning("async on_error failed", exc_info=True)
                    return
                with self._lock:
                    if epoch != self._epoch:
                        return  # superseded by a newer navigation
                    try:
                        on_done(result)
                    except Exception:
                        log.warning("async on_done failed", exc_info=True)
            finally:
                if always is not None:
                    with self._lock:
                        try:
                            always()
                        except Exception:
                            log.warning("async always failed", exc_info=True)
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
            # Everything above is a rollback on this route's own dict, so it
            # runs whenever the failure lands. The fallback is not: set_source
            # throws the nav stack away and drops the user on the offline
            # home. Only do that while this route is still the screen —
            # against a server that hangs rather than refuses, the failure can
            # arrive tens of seconds after the user has moved on, and yanking
            # them out of Settings mid-edit is worse than the error they
            # never saw.
            if route is self.route:
                self._offline_fallback(route)
        self.run_async(work, on_done, ep, on_error=failed)

    # How close to the bottom of a scroller a page request is triggered.
    PAGE_SLOP = 800

    def _page_more(self, route, offset, maximum, get, put, fetch, error=None):
        """One page of an infinite-scroll list.

        ``get(route)`` returns ``(items, total)``, ``put(route, items, total)``
        writes them back, and ``fetch(start_index)`` asks the server for the
        next page as ``(new_items, total)``. Three views used to carry a copy
        of this, and each learned its invariants separately:

        * **Only page the route that is on screen.** ``route is not
          self.route`` — a scroll event can arrive for a view being left.
        * **``_loading`` guards re-entry**, and must not survive a failure, or
          the list never requests anything again for the rest of the session.
          (``run_async`` runs ``on_error`` regardless of epoch for this
          reason.)
        * **An in-range page that comes back empty ends the list.** A random
          sort that reshuffles per request, or a filter the server applies
          differently than we do, otherwise gets re-asked on every scroll
          event forever.
        * **Never page from an empty list** — that is start_index=0, i.e. the
          initial load, and the loader owns it.
        """
        if route is not self.route or route.get("_loading"):
            return
        items, total = get(route)
        if not items or len(items) >= total:
            return
        if maximum - offset >= self.PAGE_SLOP:
            return                       # only page in near the bottom
        route["_loading"] = True
        ep = self._epoch
        start = len(items)

        def done(res):
            new, total2 = res
            cur, _t = get(route)
            merged = list(cur) + list(new)
            put(route, merged, total2 if new else len(merged))

        def failed(_exc):
            # The toast is about a list. Nobody asked for this page — it was
            # triggered by scrolling — so reporting it over whatever screen
            # the user moved to is noise. (An edit the user *pressed a button*
            # for is the opposite case; see _edit_call.)
            if route is self.route:
                self.set_status(error or _("Could not load more items."))

        def clear_guard():
            route["_loading"] = False

        # clear_guard is `always`, not part of done/failed: a page dropped for
        # being stale runs neither, and a _loading left set means this route
        # never pages again — scroll to the bottom, click a tile, come back,
        # and the list is silently capped for the rest of the session.
        self.run_async(lambda: fetch(start), done, ep, on_error=failed,
                       always=clear_guard)

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

    _ROUTES_CACHE = None

    @classmethod
    def _routes(cls):
        """The merged kind -> (loader, renderer) table.

        Each mixin declares the kinds it owns in its own ROUTES, so a view is
        added in one place next to the code that draws it. Reading
        ``self.ROUTES`` would only ever see the first mixin in the MRO, so
        walk it and merge; a kind claimed twice is a bug, not a silent
        override (see tests/test_mpvtk_browser_mixins.py).
        """
        # __dict__, not attribute lookup: a plain `cls._ROUTES_CACHE` resolves
        # through the MRO, so a subclass would find the parent's populated
        # cache and return it — silently dropping its own ROUTES, which is the
        # exact failure this table exists to prevent.
        if cls.__dict__.get("_ROUTES_CACHE") is None:
            merged = {}
            for base in cls.__mro__:
                for kind, pair in (base.__dict__.get("ROUTES") or {}).items():
                    merged.setdefault(kind, pair)
            cls._ROUTES_CACHE = merged
        return cls._ROUTES_CACHE

    def _load_route(self, route):
        """Dispatch to the route kind's loader, if it has one.

        Kinds are declared in each mixin's ROUTES table alongside their
        renderer, so adding a view is one edit in one place — this used to
        be a 215-line elif chain here and a dict a thousand lines away.
        """
        if self.server is None:
            return
        route.pop("_error", None)
        loader = (self._routes().get(route["kind"]) or (None, None))[0]
        if loader is not None:
            # ep is read here, on the loop thread, and handed down: a loader
            # that read it later would be racing the navigation it guards.
            getattr(self, loader)(route, self._epoch)

    def _edit_call(self, fn, on_ok=None, on_error=None, error=None):
        """A mutating edit whose failure the user must see.

        _client_call swallows: an "Add to Playlist" the server rejected
        looked exactly like one that worked. ``on_error`` undoes whatever
        the view already showed optimistically — leaving a rejected change
        on screen is worse than never showing it."""
        ep = self._epoch
        msg = error or _("The change could not be applied.")

        def work():
            fn(self.controller)

        def done(_ok):
            if on_ok is not None:
                on_ok()

        def failed(_exc):
            if on_error is not None:
                on_error()
            self.set_status(msg)
        self.run_async(work, done, ep, on_error=failed)

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
            # parent_type as well as collection_type: inside a BoxSet the
            # tile menu can offer "Remove from Collection".
            self.navigate(dict(base, kind="grid", parent_id=item.get("Id"),
                               parent_type=t,
                               collection_type=item.get("CollectionType")))
        else:
            self.set_status(_("Selected: %s") % item.get("Name", ""))
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
        self._sync_queue_highlight(state)
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

    def _sync_queue_highlight(self, state):
        """Keep the queue view's "now playing" row on the right track.

        The queue's data is fetched once when the route opens, so the
        highlight stayed on whatever was playing then — it never moved when
        a song ended or was skipped. Cheap: the id comes off the playstate,
        no refetch."""
        route = self.route
        if route.get("kind") != "queue":
            return
        data = route.get("_data")
        if not data:
            return
        new = None if (not state or state.get("stopped")) else state.get("id")
        if data.get("current_id") != new:
            data["current_id"] = new
            self.invalidate()

    def _start_daemon(self, attr, name, body):
        """Run ``body`` on a daemon thread, at most one per ``attr``.

        The check and the assignment have to be atomic. Every caller used to
        write ``if self._x_thread is not None: return`` and then assign, but
        they are reachable from the loop thread *and* from foreign ones
        (``on_playstate``, ``on_downloads_changed``), so two callers could
        both see None and both start a thread. Doubling a poller is only a
        wasted refresh today, which is exactly why it would have gone
        unnoticed.

        ``attr`` is cleared when the thread exits, so the next call starts a
        fresh one. Returns True if this call started the thread, False if one
        was already running — callers driven by a *user action* should say so
        rather than appear to do nothing.
        """
        with self._poller_lock:
            if getattr(self, attr) is not None:
                return False

            def run():
                try:
                    body()
                finally:
                    # Compare-and-clear: a body that released its own slot
                    # early (see _arm_toast_clear) may already have been
                    # replaced, and an exiting thread must not unregister its
                    # successor.
                    with self._poller_lock:
                        if getattr(self, attr) is thread:
                            setattr(self, attr, None)

            thread = threading.Thread(target=run, daemon=True, name=name)
            setattr(self, attr, thread)
        thread.start()
        return True

    def _run_long(self, work, name):
        """Run a job that can take minutes, off the pool.

        The pool has four workers and serves every route load and every
        client mutation. A job that holds one for minutes — relocating the
        download store copies the whole thing, possibly across drives —
        starves browsing, and a handful of them would stop it outright.
        Long jobs get their own thread instead.

        One at a time, and False if one is already running."""
        def run():
            try:
                work()
            except Exception:
                log.error("long job %r failed", name, exc_info=True)
        return self._start_daemon("_long_thread", name, run)

    def _start_np_ticker(self):
        """Keep the now-playing bar's clock at 1s.

        The timeline thread only pushes state every 5s (it also talks to the
        server, so speeding it up is not free). While the bar is on screen we
        ask the player for a fresh snapshot once a second instead; the thread
        exits as soon as the bar goes away."""
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(1.0):
                bar = self._now_playing is not None and self._browsing
                if not bar and not self._hud_shown:
                    break
                try:
                    self.controller.refresh_playstate()
                except Exception:
                    log.debug("playstate refresh failed", exc_info=True)

        self._start_daemon("_np_thread", "mpvtk-np-tick", tick)

    def set_source(self, source, server_uuid=None):
        """Swap in a live data source once servers connect (the browser opens
        immediately on a spinner and populates when the network settles).

        A catalog-backed source raises the offline banner: every path that
        can land offline goes through here, so deriving the banner from the
        source is what keeps the two from drifting apart."""
        from .repository import OfflineLibrarySource

        # Through set_offline, so _offline has one writer. It used to be
        # assigned here directly, which left set_offline with no production
        # caller at all — a public method only the tests reached.
        self.set_offline(isinstance(source, OfflineLibrarySource))
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

    def on_downloads_changed(self):
        """Sync-manager push: the catalog changed. Runs on the download
        worker's thread, so it only schedules the refresh."""
        try:
            self._refresh_downloaded()
        except Exception:
            log.debug("download refresh failed", exc_info=True)

    def _refresh_downloaded(self):
        """Refresh the downloaded-id sets for tile badges (from the sync db)."""
        if self.controller is None:
            return

        def work():
            try:
                # The unpack stays inside the guard: a controller that cannot
                # answer (no sync db, or a stub) returns None, and that must
                # leave the badges alone rather than raise on a pool thread.
                (self._downloaded, self._downloaded_series,
                 self._downloaded_seasons,
                 self._downloaded_playlists) = self.controller.downloaded_ids()
            except Exception:
                return
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
        toast = self._toast_node(w, h)
        if toast is not None:
            children.append(toast)
        return Column(children, w=w, h=h, align="stretch")

    # How long a status message stays on screen.
    TOAST_SECS = 6.0

    def _toast_node(self, w, h):
        """Status messages as a floating toast, on every screen.

        ``status`` used to be rendered in exactly one place — the Settings
        tab — while being written from fourteen, including _edit_call's
        "the change could not be applied". So a rejected Add to Playlist
        from the home screen wrote its error to a field that never reached
        a pixel, which is the very thing _edit_call exists to prevent."""
        if not self.status:
            return None
        left = self.TOAST_SECS - (time.time() - (self._status_at or 0))
        if left <= 0:
            self.status = ""
            return None
        self._arm_toast_clear(left)
        tw = min(max(320, len(self.status) * 9), max(360, w - 80))
        return Float(
            Box([Text(self.status, size=16, wrap=True, w=tw - 32)],
                pad=16, bg=theme.CARD_BG, radius=10, border=theme.BORDER,
                align="stretch"),
            x=(w - tw) / 2, y=max(20, h - 140), w=tw)

    def _arm_toast_clear(self, delay):
        """Repaint once the toast has expired — nothing else would."""
        def clear():
            self._shutdown_evt.wait(delay)
            # Release the slot *before* repainting: the rebuild this wakes
            # may want to arm the next toast, and it would be dropped if we
            # were still registered.
            with self._poller_lock:
                self._toast_timer = None
            self.invalidate()

        self._start_daemon("_toast_timer", "mpvtk-toast", clear)

    def set_status(self, text):
        """Show a transient message. Use this rather than assigning to
        ``status``, so the toast's timer starts."""
        self.status = text or ""
        self._status_at = time.time()
        self.invalidate()

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
            # labels sit next to, so nothing new has to be learned. The
            # tooltip carries the label in that state: compact is exactly
            # when the button stops saying what it does, and it was the
            # only mode with neither a label nor a tip.
            return Button("" if compact else label, id=node_id, icon=icon,
                          on_click=cb, tip=label if compact else None)

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
                selected=cur, min_w=110, tip=_("Server"),
                max_w=150 if compact else 260,
                on_select=lambda i, v: self._switch_server(servers[i]["uuid"])))
        users = self._users()
        # Not while offline: switching user reconnects, which cannot work
        # with no server, and Tk gated it for that reason.
        if len(users) > 1 and not self._offline:
            cur = next((i for i, u in enumerate(users)
                        if u.get("active")), 0)
            right.append(Dropdown(
                "nav-user",
                [u.get("name", "?") for u in users],
                selected=cur, min_w=100, tip=_("User"),
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
                   tip=_("Search"),
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

    def _switch_server(self, uuid):
        if uuid == self.server:
            return
        # A SyncPlay group belongs to the server it was joined on, and this
        # UI only ever talks to the selected one — so leaving the server
        # means leaving the group, or it stays joined with no way to reach
        # it from here.
        old = self.server
        if old and self.controller is not None:
            try:
                if self.controller.sync_active():
                    self._client_call(lambda c: c.sync_leave(old))
            except Exception:
                log.debug("syncplay leave on server switch failed",
                          exc_info=True)
        self.server = uuid
        self.navigate({"kind": "home", "server": uuid}, reset=True)

    def _open_queue(self):
        self.navigate({"kind": "queue", "server": self.server,
                       "title": _("Queue")})

    def _render_route(self, route, size):
        renderer = (self._routes().get(route["kind"]) or (None, None))[1]
        if renderer is None:
            return self._busy()
        # A load that failed with nothing to show says so and offers a
        # retry. Without this the route's data stayed None and the view
        # spun forever, so an unreachable server read as a hang.
        if (route.get("_error")
                and route.get("_data") is None
                and not route.get("_items")):
            return self._error_retry(route)
        return getattr(self, renderer)(route, size)

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
        # Update first: the offline banner is persistent, so checking it
        # first meant an update notice was never seen while offline.
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
        if self._offline:
            return Row([
                Text(_("Offline — showing what's available."), size=16),
                Spacer(),
                Button(_("Configure Servers"), id="banner-servers",
                       on_click=self.show_login),
                Button(_("Retry"), id="banner-retry",
                       on_click=self._retry_connect),
            ], pad=10, gap=10, align="center", h=48, bg="5a3a1a")
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
        if self.controller is None:
            return

        def tick():
            while not self._shutdown_evt.wait(2.0):
                if not self._browsing:
                    continue
                try:
                    st = self.controller.download_status()
                except Exception:
                    break
                self.set_download_status(st)

        self._start_daemon("_dlbar_thread", "mpvtk-dlbar", tick)

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
                return
            # Previously a silent no-op: the banner stayed, nothing moved,
            # and pressing Retry again looked identical to never having
            # pressed it. Say the reconnect failed.
            self.set_status(_("Still can't reach the server."))
            if self.route.get("kind") == "connecting":
                self.route["_connect_error"] = _("Still can't reach the server.")
            self.invalidate()

        self.run_async(work, done, ep)

    # --------------------------------------------------------------- lifecycle

    def run(self):
        """Block the calling thread driving the app loop (spawned-app / demo
        use). For the shared-window integration this runs on a dedicated
        thread next to playerManager — see 0.2/0.5 wiring."""
        self.app.run(self.build)

    def shutdown(self):
        self._shutdown_evt.set()   # also stops the downloads poller
        self._pool.shutdown(wait=False, cancel_futures=True)
        if self.thumbs is not None:
            self.thumbs.shutdown()
        self.strips.clear()
