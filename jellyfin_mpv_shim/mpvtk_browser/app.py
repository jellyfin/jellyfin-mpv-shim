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
    Busy,
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
from .repository import (FOLDER_TYPES, LIVE_TYPES, PLAYABLE_TYPES,
                         SERIES_TYPES)
from .strips import (LANDSCAPE_GEOM, POSTER_GEOM, SQUARE_GEOM, StripStore,
                     TileGeom)
from .dialogs import DialogsMixin
from .auth import AuthMixin
from .settings import SettingsMixin
from .queue_edit import QueueEditMixin
from .music import MusicMixin
from .views import ViewsMixin, SORTS
from .tiles import TilesMixin
from .cast import CastMixin

log = logging.getLogger("mpvtk_browser.app")


def now_id_of(state):
    """The playing item's id, if the payload carries one."""
    return (state or {}).get("id")

# Routes that take over the whole surface (no nav chrome), like the Tk
# browser's login/locked/connecting screens.
# "cast" is chrome-free for two reasons: it is a full-bleed backdrop
# (chrome over it would look wrong), and in headless mode the chrome IS
# the way into the library.
CHROME_FREE = {"login", "locked", "connecting", "cast"}

# Where the now-playing bar must NOT appear. Deliberately not CHROME_FREE:
# the cast screen is chrome-free but IS where audio playback lives in
# headless mode, and suppressing the bar there would leave a cast-target box
# playing music with no transport controls at all — worse than the library
# access it was meant to deny. The other three are pre-library screens where
# nothing can be playing.
NO_NOW_PLAYING = {"login", "locked", "connecting"}


class MpvtkBrowser(DialogsMixin, AuthMixin, SettingsMixin, QueueEditMixin,
                   MusicMixin, ViewsMixin, TilesMixin, CastMixin):

    # Horizontal padding of ordinary page content.
    CONTENT_PAD = 16

    # Pagination (settings.paginated). A page is one screenful of tiles with a
    # bottom bar instead of a scrollbar. Heights below are the fixed chrome/bar
    # heights the page-size math subtracts so a page fits without scrolling;
    # they mirror the real widgets (_chrome h=60, _banner/_download_bar, the
    # now-playing bar) and only need to be close — the row count rounds down, so
    # an over-estimate just shows one fewer row rather than clipping.
    PAGINATION_BAR_H = 48
    CHROME_H = 60
    BANNER_H = 48
    DLBAR_H = 44
    # Cap a page's tile count so a huge window can't blow the 63-overlay budget
    # (a non-scrolling page composites every tile at once).
    PAGE_MAX = 60
    # Route kinds that paginate. The music songs list and genre grids stay
    # scrolling (a list, and an unpaged single request).
    PAGEABLE_KINDS = {"grid", "person", "music"}

    # How long shutdown() waits for a long job (a download-store move) to
    # finish before giving up on it. Long enough to cover a same-drive move,
    # short enough not to hang a quit.
    LONG_JOB_SHUTDOWN_WAIT = 20.0

    def __init__(self, app, source, strips=None, thumbs=None,
                 server_uuid=None, geom=None, controller=None, config=None):
        # Before anything is built: apply the user's chosen theme (palette +
        # mpv browse background), then hand the accent to the toolkit's
        # accented widgets, which read the palette at construction time.
        from ..conf import settings as _settings
        self._theme_cfg = theme.apply(getattr(_settings, "theme", "default"))
        try:
            from .. import player as _player
            _player.BROWSE_BG_HEX = self._theme_cfg["browse_bg"]
        except Exception:
            pass
        # Glow is theme-driven (Nebula on, Default off); the toolkit forwards
        # it to the renderer alongside the accent.
        theme.apply_to_toolkit(glow=self._theme_cfg.get("glow", False))
        log.info(
            "theme: %s (accent %s, glow %s)",
            self._theme_cfg.get("name", "?"),
            getattr(theme, "ACCENT", "?"),
            self._theme_cfg.get("glow", False),
        )
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
        # Cast/idle screen state (see cast.py). Present whether or not
        # headless is set — without it, this is what a DisplayContent from a
        # phone renders.
        self._cast = {"idle": True}
        self._cast_entry = None
        self._cast_backdrop = None
        self._cast_backdrop_key = None
        self._cast_size = None
        self._cast_lock = threading.Lock()
        # Locked-down cast-target mode: the cast screen is the ONLY page.
        # See navigate() and mpvtk/HEADLESS.md for what this does and does
        # not protect against.
        self.headless = bool(self._cfg_headless())
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
        # Offset each container was last re-rendered at -- the baseline the
        # SCROLL_STEP threshold measures against, so slow sub-row scrolling
        # accumulates to a rebuild instead of never crossing the gap between
        # two adjacent events.
        self._scroll_rendered = {}
        self._live_offsets = None
        # Startup-PIN lock screen state. _locked is True while the gate is
        # actually gating: tray commands that would navigate (Configure
        # Servers, Show Console) are swallowed while it is set, so they
        # can't reveal content from behind the lock.
        self._pin = {"pin": ""}
        self._pin_error = None
        self._locked = False
        # Playback start feedback. _starting is {"title"} while a file is
        # loading, _load_error the failure dict once one fails. Both are
        # written from foreign threads (see on_load_start / on_load_error) and
        # rendered by build(), which is why neither is a dialog builder.
        self._starting = None
        self._load_error = None
        # Cover size: the theme's default, overridden by the Cover Size setting
        # if set. Posters/square scale; the landscape (library) tile is the
        # theme's own shape (Nebula uses a less-wide crop).
        _cs = (getattr(_settings, "poster_scale", None)
               or self._theme_cfg.get("poster_scale", 1.0))
        _lw, _lh = self._theme_cfg.get("tile_landscape", (240, 135))
        self.geom = geom or POSTER_GEOM.scaled(_cs)          # 2:3
        self.geom_wide = TileGeom(tile_w=_lw, tile_h=_lh,
                                  caption_h=LANDSCAPE_GEOM.caption_h)  # 16:9-ish
        self.geom_square = SQUARE_GEOM.scaled(_cs)           # 1:1
        # Tile caption font is theme-controlled and, when set, does NOT scale
        # with the cover (jellyfin-web-style: big art, modest labels), so long
        # titles fit more before they clip. The category headings are separate
        # (heading_size) and untouched.
        _tts = self._theme_cfg.get("tile_title_size")
        _tss = self._theme_cfg.get("tile_sub_size")
        if _tts or _tss:
            import dataclasses as _dc

            def _cap(g):
                return _dc.replace(g, title_size=_tts or g.title_size,
                                   sub_size=_tss or g.sub_size)
            self.geom = _cap(self.geom)
            self.geom_wide = _cap(self.geom_wide)
            self.geom_square = _cap(self.geom_square)
        # Downloaded id sets (for the tile badge), refreshed from the sync db.
        self._downloaded = set()
        self._downloaded_series = set()
        self._downloaded_seasons = set()
        self._downloaded_playlists = set()
        # Default to a file-backed store (works on both backends / headless);
        # the libmpv integration passes a MemoryStore-backed one.
        self.strips = strips or StripStore(
            cache_dir=cache_dir("mpvtk-browser-"), geom=self.geom)
        # Wake our loop when an async row composite lands (see StripStore.strip).
        # self.invalidate reads self.app at call time, so this survives mpv
        # re-creation without re-wiring in set_app.
        self.strips.set_notify(self.invalidate)
        self.thumbs = thumbs      # ThumbnailStore (optional; None -> no art)
        if self.thumbs is not None:
            # Wake our loop when a decoded poster lands, so build() can pump it.
            self.thumbs.set_notify(self.invalidate)

        servers = []
        try:
            servers = source.servers()
        except Exception:
            log.warning("could not enumerate servers", exc_info=True)
        self.server = self._pick_server(servers, server_uuid)

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
        # Repaints once a load has been slow enough to deserve the spinner.
        self._spinner_timer = None
        # live text of the download-folder field
        self._sync_path = {}
        self.status = ""
        self._size = None         # last window size seen by build()

        self.nav_stack = [self._default_route()]
        self._load_route(self.route)

    # ------------------------------------------------------------ routing

    def _default_route(self):
        """Where the browser lands when it has nowhere specific to go.

        Every direct ``nav_stack`` assignment must come through here.
        ``navigate()`` enforces the headless lockdown, but assigning the
        stack bypasses it entirely — which is exactly how a successful
        connect put a headless box on the library: ``set_source`` reset the
        stack to home itself, and the refusal never ran.
        """
        if self.headless:
            return {"kind": "cast"}
        return {"kind": "home", "server": self.server}

    @property
    def route(self):
        return self.nav_stack[-1]

    # Routes headless mode still allows. Everything else is the library.
    HEADLESS_ROUTES = {"cast", "connecting", "locked"}

    def navigate(self, route, reset=False, force=False):
        """Go to ``route``.

        In headless mode this is the single choke point for the lockdown:
        every way into the library ends up here (a tile click, the tray's
        "Show Library Browser", a remote's GoHome, the now-playing bar's
        Queue button, a DisplayContent from a phone), so refusing here is
        what makes the mode mean something rather than hiding one entry
        point and leaving five others open.

        ``force`` is for the screens headless itself needs to reach.
        """
        if (self.headless and not force
                and route.get("kind") not in self.HEADLESS_ROUTES):
            log.debug("headless: refusing navigation to %r", route.get("kind"))
            return
        if reset:
            self.nav_stack = []
        self.nav_stack.append(route)
        self._reset_scroll()
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def _cfg_headless(self):
        """Read the headless flag. Tolerates a config object without it (the
        test fakes hand-build a small schema)."""
        cfg = self._config_obj
        if cfg is None:
            try:
                from ..conf import settings
                return getattr(settings, "headless", False)
            except Exception:
                return False
        try:
            return bool((cfg.get_settings() or {}).get("headless", False))
        except Exception:
            return False

    def _reset_scroll(self):
        """Forget recorded scroll offsets on a route change.

        Scroll container ids are per-view ("grid", "playlist", …), not per
        route, so a deep scroll in one library used to carry into the next
        view opened under the same id. The renderer clamps its own offset to
        the new (shorter) content, but our copy didn't — so virtualization
        windowed rows that were far past the end and the view rendered
        empty: "7 items" in the header and nothing below it."""
        self._scroll_off.clear()
        self._scroll_rendered.clear()

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
        ] or [self._default_route()]
        route = self.route
        route.pop("_data", None)
        route.pop("_items", None)
        route.pop("_loading", None)
        self._bump_epoch()
        self._load_route(route)
        self.invalidate()

    def display_item(self, server_uuid, item_id):
        if self.headless:
            # Same gesture, different answer: paint it on the cast screen
            # rather than opening a page the user could then browse from.
            self.display_cast_item(server_uuid, item_id)
            return
        return self._display_item(server_uuid, item_id)

    def _display_item(self, server_uuid, item_id):
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
            # Imported here, not at module scope, like the other conf reads
            # in this file (import cycle: conf -> ... -> app).
            from ..conf import settings
            if self._minimized and not settings.display_mirror_summon:
                # Closed to the tray. The route is set either way, so the
                # page is waiting whenever the browser is opened — but
                # popping the window open because someone idly scrolled a
                # phone is not something to do by default. Opt in with
                # display_mirror_summon.
                return
            if self._minimized or self._browsing:
                # Idle or already browsing: bring the page forward.
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
        if self.headless:
            # A remote is input like any other. Declining here lets the
            # player fall back to its own OSD menu, which is transport-only.
            return False
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
        """Invalidate every in-flight async result. Returns the new epoch."""
        with self._lock:
            self._epoch += 1
            return self._epoch

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

    # ---------------------------------------------------------- pagination

    def _paginated(self):
        """The global paginate-tile-grids toggle (settings.paginated).

        Read live so the Settings toggle takes effect on the next frame; a
        missing key (test stand-ins) reads as off."""
        try:
            from ..conf import settings
            return bool(getattr(settings, "paginated", False))
        except Exception:
            return False

    def _content_h(self, route, size):
        """Vertical space the route content actually gets — the window minus
        the chrome and bars that sit above/below it in ``build``. Mirrors
        build()'s own conditions so a paginated page can size itself to fit."""
        h = size[1]
        if route.get("kind") not in CHROME_FREE:
            h -= self.CHROME_H
            if self._update or self._offline:
                h -= self.BANNER_H
            if self._dl_status and self._dl_status.get("pending"):
                h -= self.DLBAR_H
        h -= self.PAGINATION_BAR_H
        if (self._now_playing is not None
                and route.get("kind") not in NO_NOW_PLAYING):
            from .music import NOW_PLAYING_BAR_H
            h -= NOW_PLAYING_BAR_H
        return max(1, h)

    def _page_size(self, route, size, head_h, geom):
        """Tiles per page = columns × rows that fit under the header. Rounds
        the row count DOWN so a page never overflows its slot (which would
        clip the last row or force a scroll); capped at PAGE_MAX for the
        overlay budget."""
        avail = self._content_h(route, size) - head_h - self.CONTENT_PAD
        pitch = geom.strip_h + self.GRID_GAP
        rows = max(1, int((avail + self.GRID_GAP) // pitch)) if pitch > 0 else 1
        cols = self._cols(size[0], geom)
        return max(1, min(cols * rows, self.PAGE_MAX))

    def _page_count(self, route, ps):
        total = route.get("_total")
        if not total or ps <= 0:
            return None
        return max(1, -(-total // ps))         # ceil

    def _ensure_page(self, route, ps, fetch, seed=None):
        """Make the current page's items available at page size ``ps`` and
        return them (or None while a fetch is in flight).

        ``fetch(start, limit) -> (items, total)`` gets a page from the source;
        ``seed`` is an already-loaded head of the list (the initial-load chunk)
        used to fill page 0 without a second request. The current page and the
        pages on either side are fetched, so Next/Previous land instantly; the
        cache is pruned to that window so a deep library doesn't accumulate
        every page it visited."""
        if route.get("_page_size") != ps:
            route["_page_size"] = ps
            route["_pages"] = {}
            route["_page_loading"] = set()
        pages = route["_pages"]
        npages = self._page_count(route, ps)
        cur = route.get("_page") or 0
        if npages is not None:
            cur = max(0, min(cur, npages - 1))
        route["_page"] = cur
        route["_npages"] = npages
        if seed and cur == 0 and 0 not in pages and len(seed) >= ps:
            pages[0] = list(seed[:ps])
        self._fetch_page(route, cur, ps, fetch)
        for nb in (cur + 1, cur - 1):
            if nb >= 0 and (npages is None or nb < npages):
                self._fetch_page(route, nb, ps, fetch, prefetch=True)
        keep = {cur - 1, cur, cur + 1}
        for p in [p for p in pages if p not in keep]:
            pages.pop(p, None)
        return pages.get(cur)

    def _fetch_page(self, route, page, ps, fetch, prefetch=False):
        pages = route["_pages"]
        loading = route["_page_loading"]
        if page in pages or page in loading:
            return
        loading.add(page)
        ep = self._epoch
        start = page * ps

        def done(res):
            items, total = res
            route["_pages"][page] = list(items)
            if total:
                route["_total"] = total

        def failed(_exc):
            # A prefetch nobody asked for stays silent; a page the user is
            # waiting on says so (mirrors _page_more's toast rule).
            if not prefetch and route is self.route:
                self.set_status(_("Could not load this page."))

        def clear():
            route["_page_loading"].discard(page)

        self.run_async(lambda: fetch(start, ps), done, ep,
                       on_error=failed, always=clear)

    def _reset_pagination(self, route):
        """Drop the page cache and return to page 1. Called whenever the
        underlying result set changes (sort, filter, collections toggle, music
        tab) — page 3 of one ordering is nothing like page 3 of another."""
        for k in ("_pages", "_page_size", "_page_loading", "_npages"):
            route.pop(k, None)
        route["_page"] = 0

    def _page_go(self, route, page):
        """Jump to a page (0-based); _ensure_page clamps into range next
        frame, so an out-of-range target from Last/typing is harmless."""
        route["_page"] = page
        self.invalidate()

    def _page_jump(self, route, text):
        """The page-number box: a 1-based page to go to."""
        try:
            n = int(str(text).strip())
        except (TypeError, ValueError):
            return
        self._page_go(route, max(0, n - 1))

    def _pagination_bar(self, route, w):
        """`Page [n] of N     |◀ ◀ ▶ ▶|` — the bottom bar that replaces the
        scrollbar in paginated mode. None unless paginated, on a pageable
        route, and a page count is known (set by _ensure_page this frame)."""
        if not self._paginated() or route.get("kind") not in self.PAGEABLE_KINDS:
            return None
        npages = route.get("_npages")
        if not npages:
            return None
        cur = route.get("_page") or 0

        def nav(icon, node_id, target, tip):
            # Square page buttons, not the flat translucent playback-HUD
            # treatment: this is library chrome, not an overlay on video.
            # justify="center" as well as align: with a fixed width and no
            # label the lone icon would otherwise pack against the left edge.
            return Button("", id=node_id, icon=icon, w=32, h=32, pad=0,
                          justify="center", tip=tip,
                          on_click=lambda: self._page_go(route, target))

        return Row([
            Text(_("Page"), size=15, color=theme.SUBTLE_FG),
            # force: the box tracks the current page, so paging with the
            # buttons updates the number rather than leaving a stale edit.
            # on_commit as well as on_submit: ENTER jumps, and so does clicking
            # (or tabbing) out of the box. on_commit only fires when the value
            # actually changed from focus-time, and ENTER marks it agreed, so
            # the two never double-fire for one edit.
            TextBox("pg-jump", text=str(cur + 1), w=64, force=True,
                    on_submit=lambda s: self._page_jump(route, s),
                    on_commit=lambda s: self._page_jump(route, s)),
            Text(_("of %d") % npages, size=15, color=theme.SUBTLE_FG),
            Spacer(),
            nav("first_page", "pg-first", 0, _("First page")),
            nav("chevron_left", "pg-prev", cur - 1, _("Previous page")),
            nav("chevron_right", "pg-next", cur + 1, _("Next page")),
            nav("last_page", "pg-last", npages - 1, _("Last page")),
        ], pad=8, gap=8, align="center", h=self.PAGINATION_BAR_H,
            bg=theme.PANEL_BG)

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

    def _load_route(self, route, epoch=None):
        """Dispatch to the route kind's loader, if it has one.

        Kinds are declared in each mixin's ROUTES table alongside their
        renderer, so adding a view is one edit in one place — this used to
        be a 215-line elif chain here and a dict a thousand lines away.

        The epoch is re-read here rather than threaded down from the
        ``_bump_epoch()`` that every caller performs immediately above.
        **That is deliberate and it is not the race it looks like.**

        A review flagged the two statements as non-atomic and concluded a
        foreign bump in between would strand the route. It is the other way
        round: re-reading yields the *newest* epoch, so a loader can never
        capture one that is already superseded. Threading the navigation's
        value down is what breaks it — an interloping bump then makes the
        captured epoch stale, ``run_async`` drops the ``on_done``, and
        because no ``_error`` is set the view spins forever with no retry.
        That was tried, and ``TestNavigationSurvivesAConcurrentBump``
        (tests/test_mpvtk_browser_shell.py) is what caught it.

        The residue is benign: if a foreign thread bumps *and* navigates in
        between, this load applies into a route dict that is no longer on
        screen. A wasted write, not a wrong one.

        ``epoch`` therefore exists only for callers that genuinely have their
        own (none today). Leave it None.
        """
        if self.server is None:
            return
        route.pop("_error", None)
        loader = (self._routes().get(route["kind"]) or (None, None))[0]
        if loader is not None:
            # ep is read here, on the loop thread, and handed down: a loader
            # that read it later would be racing the navigation it guards.
            ep = self._epoch if epoch is None else epoch
            getattr(self, loader)(route, ep)

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
        elif t in LIVE_TYPES:
            # Straight to playback: a live channel has no detail page to open
            # and nothing to resume. A Program is not itself playable — what
            # you watch is the channel carrying it, which is how jellyfin-web
            # resolves it too. Falling back to the item's own id covers a
            # TvChannel, and a program whose ChannelInfo fields are missing
            # then fails as a normal unplayable item rather than silently
            # doing nothing.
            self._play_list([item.get("ChannelId") or item.get("Id")], server)
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
        if hasattr(app, "on_clipboard_error"):
            app.on_clipboard_error = self._on_clipboard_error

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

    def _start(self, audio, title=""):
        """Prepare to start playback. Video yields the whole window to the
        video + OSC; audio has no picture, so we stay in browse and show the
        now-playing bar instead (playing would-be background over audio would
        stop it)."""
        self._load_error = None
        if audio:
            self._now_playing = self._now_playing or {"title": _("Loading…")}
            self.invalidate()
        else:
            # Deliberately NOT _yield() yet. HUD mode is attached-but-idle
            # "with a blank scene" (mpvtk/app.py set_hud), and the non-HUD
            # branch detaches the renderer outright — so yielding here threw
            # our own scene away, which is why the load showed nothing at all
            # and the UI appeared to flash. Keep the window, draw the spinner,
            # and hand off in _clear_load_state once playback reports in.
            #
            # _starting is set here rather than only in the player's
            # on_load_start hook because this runs the instant the user
            # clicks, while the hook waits on the PlaybackInfo round trip —
            # itself seconds of the wait the spinner exists to cover. The hook
            # still matters for playback started remotely, which never passes
            # through here.
            self._starting = {"title": title, "owns_window": True,
                              "at": time.time()}
            self._browsing = False
            self._arm_spinner()
            self.invalidate()

    # ------------------------------------------------------ load feedback

    def on_load_start(self, info):
        """Player hook: a file is being loaded into mpv.

        Called from a foreign thread (pool worker, websocket, or action
        thread), so this only writes state and invalidates — see the
        threading contract at the top of this module.
        """
        # The player's title is the better one (it carries the "(Transcode)"
        # suffix), but keep the click-time title when it has none rather than
        # blanking a spinner that was already naming the item.
        prev = self._starting or {}
        title = (info or {}).get("title") or prev.get("title") or ""
        # Latched, not re-read at failure time: stop() on the failure path
        # pushes a stopped playstate that returns us to browse, so reading
        # _browsing when the error lands would always say "browsing" and
        # downgrade a video failure to a toast.
        owns = prev.get("owns_window")
        if owns is None:
            owns = not self._browsing
        # Keep the original click time: this hook lands one PlaybackInfo round
        # trip after the user pressed play, and restarting the clock here
        # would push the spinner out by that much again.
        self._starting = {"title": title, "owns_window": bool(owns),
                          "at": prev.get("at") or time.time()}
        self._load_error = None
        self._arm_spinner()
        self.invalidate()

    @staticmethod
    def _load_error_text(info):
        headline = (_("Timed out loading this item")
                    if info.get("timed_out") else _("Could not play this item"))
        title, detail = info.get("title"), info.get("detail")
        if title:
            headline = "%s: %s" % (headline, title)
        return "%s — %s" % (headline, detail) if detail else headline

    def on_load_error(self, info):
        """Player hook: the load failed. Foreign thread — write, don't draw.

        The error is state rather than a `_show_dialog` call because that
        helper is loop-thread only; `build()` renders it on the next frame.

        A failure that owns the window gets the full-screen knock-out: there
        is nothing behind it and the user has no other context to lose. One
        that does not — audio, which keeps the library on screen — gets a
        toast instead, because a knock-out would yank the user out of the
        page they are still using over a single failed track.
        """
        info = dict(info or {})
        starting, self._starting = self._starting or {}, None
        owns_window = starting.get("owns_window")
        if owns_window is None:
            owns_window = not self._browsing
        if not owns_window:
            self._load_error = None
            self.set_status(self._load_error_text(info))   # invalidates
            return
        self._load_error = info
        self.invalidate()

    # A load faster than this never shows the spinner, so a snappy start does
    # not flash one up and straight back out. Short on purpose: the point is
    # only to clear the common instant start, and anything slower than about
    # half a second already reads as a hang worth acknowledging. The case the
    # spinner exists for — a file that was never qtfaststart'ed, so mpv sits
    # there relocating the moov atom — runs orders of magnitude longer.
    SPINNER_DELAY = 0.5

    def _spinner_due(self):
        """Whether the in-flight load has been slow enough to show it."""
        # One read: _clear_load_state can drop _starting from another thread
        # between two of them.
        starting = self._starting
        if starting is None:
            return False
        started = starting.get("at")
        if started is None:
            return True                 # no timestamp: don't hide it forever
        return (time.time() - started) >= self.SPINNER_DELAY

    def _arm_spinner(self):
        """Repaint once the grace period is up — nothing else would.

        The load holds no ticker: if it finishes first, _clear_load_state
        repaints and this wakes to find nothing to do.

        Waits against the CURRENT load's timestamp in a loop rather than
        sleeping a flat SPINNER_DELAY. The timer slot holds one thread, so a
        start that begins while one is pending has its own arm dropped; a
        flat sleep would then fire early against the new load, find it not
        yet due, and schedule nothing further — leaving that load with no
        spinner however long it ran.
        """
        def show():
            while not self._shutdown_evt.is_set():
                starting = self._starting
                if starting is None:
                    break               # resolved while we waited
                due_in = self.SPINNER_DELAY - (
                    time.time() - (starting.get("at") or 0))
                if due_in <= 0:
                    break
                self._shutdown_evt.wait(due_in)
            with self._poller_lock:
                self._spinner_timer = None
            self.invalidate()

        self._start_daemon("_spinner_timer", "mpvtk-spinner", show)

    def _clear_load_state(self):
        """Drop the loading/error screens once playback actually reports in.

        This is also the handoff: a video start held the window to draw the
        spinner instead of yielding (see _start), so the yield it skipped
        happens here, now that there is a picture to yield to. Audio never
        took the window (_browsing stays set), so it has nothing to hand
        over.
        """
        if self._starting is None and self._load_error is None:
            return
        handoff = self._starting is not None and not self._browsing
        self._starting = None
        self._load_error = None
        if handoff:
            self._yield()   # invalidates
        else:
            self.invalidate()

    def _loading_scene(self, size):
        """Spinner shown from play intent until duration arrives.

        The window has already yielded to video by this point (_yield leaves
        the renderer attached in HUD mode), so this is the video UI standing
        in for a picture that has not started yet — not a browser page. It
        replaces the empty scene the yield used to leave, which meant a load
        looked identical to a silent failure for up to playback_timeout, and
        made the player UI appear to flash in only once duration landed.

        Busy animates renderer-side, so holding this on screen for a 30s
        stall costs no repaints from here.
        """
        w, h = size
        title = (self._starting or {}).get("title") or ""
        rows = [Busy(w=52, h=52)]
        if title:
            rows.append(Text(title, size=22, bold=True, wrap=True,
                             align="center", w=min(760, max(280, w - 160))))
        rows.append(Text(_("Loading…"), size=16, color=theme.SUBTLE_FG))
        rows.append(Button(_("Cancel"), id="load-cancel-start",
                           on_click=self._cancel_loading))
        return Column(
            [Spacer(flex=1),
             Column(rows, gap=18, align="center"),
             Spacer(flex=1)],
            w=w, h=h, align="center", bg=theme.WINDOW_BG,
        )

    def _cancel_loading(self):
        """Abandon a load that is still in flight.

        The player aborts the duration wait rather than letting it run out
        playback_timeout, so this actually stops within a poll interval —
        the case worth cancelling is precisely the one where mpv sits on a
        stalled stream for the full 30s.
        """
        self._starting = None
        self._load_error = None
        # Abort first, then take the window back: the player is what actually
        # stops the load, and doing it in this order leaves no window where
        # we have returned to browse while the start is still running.
        cancel = getattr(self.controller, "cancel_load", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                log.error("could not cancel the load", exc_info=True)
        self.enter_browse()

    def _load_error_scene(self, size):
        """Full-screen playback failure, with the retries worth offering.

        Retry-with-transcode is deliberately a separate button rather than
        something automatic: transcoding is expensive for the server, and an
        unexpected one is a signal something is wrong rather than a fix to
        apply silently.
        """
        w, h = size
        err = self._load_error or {}
        title = err.get("title") or ""
        detail = err.get("detail")
        headline = (_("Timed out loading this item")
                    if err.get("timed_out") else _("Could not play this item"))
        rows = [Text(headline, size=28, bold=True)]
        if title:
            rows.append(Text(title, size=20, wrap=True,
                             w=min(760, max(280, w - 160))))
        if detail:
            rows.append(Text(str(detail), size=15, color=theme.SUBTLE_FG,
                             wrap=True, w=min(760, max(280, w - 160))))
        buttons = [Button(_("Retry"), id="load-retry",
                          on_click=lambda: self._retry_playback(False))]
        if err.get("can_transcode"):
            buttons.append(Button(_("Retry with Transcode"),
                                  id="load-retry-transcode",
                                  on_click=lambda: self._retry_playback(True)))
        buttons.append(Button(_("Cancel"), id="load-cancel",
                              on_click=self._cancel_failed_playback))
        rows.append(Row(buttons, gap=10, justify="center"))
        return Column(
            [Spacer(flex=1),
             Column(rows, gap=16, align="center"),
             Spacer(flex=1)],
            w=w, h=h, align="center", bg=theme.WINDOW_BG,
        )

    def _retry_playback(self, force_transcode):
        """Re-attempt the failed start. The controller queues the replay onto
        the action thread, so this returns immediately and the loop thread
        keeps drawing."""
        err = self._load_error or {}
        self._load_error = None
        # Straight to the loading screen: the retry is dispatched, and leaving
        # the error up until the player reports back reads as a dead button.
        self._starting = {"title": err.get("title") or "",
                          "owns_window": True, "at": time.time()}
        self._arm_spinner()
        self.invalidate()
        retry = getattr(self.controller, "retry_playback", None)
        if retry is None:
            return
        try:
            retry(force_transcode)
        except Exception:
            log.error("could not retry playback", exc_info=True)
            self._starting = None
            self.set_status(_("Playback could not be started."))
            self.invalidate()

    def _cancel_failed_playback(self):
        """Give up on the failed item and take the window back.

        enter_browse (not a bare _browsing flip) because it also re-takes the
        window from the player and, in headless, lands on the cast screen —
        the only page that exists there.
        """
        self._load_error = None
        self._starting = None
        self.enter_browse()

    def _play_async(self, work):
        """Start playback off the loop thread.

        ``work()`` receives the controller and runs on a pool worker.

        Starting playback is seconds of work: the controller builds a
        ``Media``, asks the server for PlaybackInfo, then loads the file into
        mpv under the player's own lock. Called straight from a click handler
        that ran on the loop thread, so the UI dispatched no events and drew
        no frames until playback began — click a movie and the browser froze.
        The episode path was worse: it ran inside a ``run_async`` ``on_done``,
        which holds ``_lock``, so every other worker's callback and any
        ``navigate()`` queued up behind it too.

        Deliberately NOT epoch-gated: the user pressed Play, and navigating
        elsewhere while it starts is not a reason to cancel it.
        """
        if self.controller is None:
            return

        def failed(_exc):
            self.set_status(_("Playback could not be started."))
            self.invalidate()

        self.run_async(lambda: work(self.controller), lambda _r: None,
                       self._epoch, on_error=failed)

    def _play(self, item, server, offset_ticks=None, srcid=None, aid=None,
              sid=None):
        """Yield/keep-browse and start a single ``item``. Episodes queue the
        rest of the season so autoplay-next chains them (like the Tk browser)."""
        self._start(audio=item.get("Type") == "Audio",
                    title=item.get("Name") or "")
        if self.controller is None:
            return
        if item.get("Type") == "Episode" and item.get("SeriesId"):
            srv, iid, series = server, item.get("Id"), item.get("SeriesId")

            # Queue fetch AND playback start on the same worker: the fetch
            # decides the queue the start consumes, and splitting them put
            # the start back on the loop thread via on_done.
            def work(ctl):
                try:
                    q = self.source.get_series_queue(
                        srv, series, start_item_id=iid)
                    ids = [e.get("Id") for e in q if e.get("Id")] or [iid]
                except Exception:
                    log.debug("series queue fetch failed", exc_info=True)
                    ids = [iid]
                ctl.play_list(ids, srv, 0, offset_ticks=offset_ticks,
                              srcid=srcid, aid=aid, sid=sid)

            self._play_async(work)
        else:
            self._play_async(
                lambda ctl: ctl.play(item, server, offset_ticks=offset_ticks,
                                     srcid=srcid, aid=aid, sid=sid))

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
        title = ""
        if items is not None and 0 <= start_index < len(items):
            offset = ((items[start_index].get("UserData") or {})
                      .get("PlaybackPositionTicks")) or None
            # Names the spinner before the queue is even resolved.
            title = items[start_index].get("Name") or ""
        ids = [i for i in ids if i]
        if not ids:
            return
        try:
            pos = ids.index(start_id)
        except ValueError:
            pos = 0
        self._start(audio=audio, title=title)
        self._play_async(
            lambda ctl: ctl.play_list(ids, server, pos, offset_ticks=offset))

    # ------------------------------------------------- browse <-> playback

    def start_background_work(self):
        """Kick off the pollers that keep the chrome honest (download status)
        and the one-shot startup update check. Called once the browser is
        live; separate from __init__ so tests don't spawn threads."""
        self._poll_download_status()
        if self.controller is not None:
            self._pool.submit(lambda: self._safe(lambda c: c.check_updates()))

    def enter_browse(self):
        if self.headless and self.route.get("kind") not in self.HEADLESS_ROUTES:
            # Playback ended and something is putting us back on a library
            # page. In headless the only page to come back to is the cast
            # screen.
            self.show_cast()
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
        # Playback is reporting in, so the load resolved: drop the loading
        # screen (and any stale error) before anything else reads them. A
        # "stopped" state does NOT clear the error screen — stop() is exactly
        # what a failed load does on its way out, and clearing here would
        # erase the error before its first frame.
        if not (state or {}).get("stopped"):
            self._clear_load_state()
        self._sync_queue_highlight(state)
        # A pending seek-drag belongs to the track it started on. The
        # renderer fires no cancel when a dragged slider simply leaves the
        # scene (the queue ended, or we yielded the window), so the pending
        # value stuck and pinned the elapsed clock to it for every later
        # track while the slider itself kept moving.
        #
        # Keyed on the track CHANGING, not on any playstate: the now-playing
        # ticker pushes one every second, and clearing on those would cancel
        # the drag a second after it began.
        now_id = (state or {}).get("id")
        track_changed = now_id != (self._now_playing or {}).get("id")
        if not state or state.get("stopped") or track_changed:
            self._np_scrub = None
        # Headless: the cast screen is the backdrop behind the now-playing
        # bar, so it has to follow what is PLAYING. It kept showing whatever
        # a phone last cast, so starting a playlist left an unrelated film
        # on screen for the whole album.
        if self.headless:
            self._follow_cast_to_playback(state, track_changed)
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
            if self._load_error is not None:
                # A failed start owns the window and is explaining why. stop()
                # is part of that failure path, so returning to browse here
                # would bounce the user back to the library over the error
                # they have not read yet.
                self.invalidate()
                return
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

    def _start_daemon(self, attr, name, body, restartable=False):
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

        ``restartable=True`` closes a gap that bit the logs tail. A poller
        decides to exit by noticing the route it was started for is no longer
        current, but it only notices on its next tick — up to a full poll
        interval later. Leave the tab and come straight back inside that
        window and the sequence is: the view starts a poller for the new
        route, this returns False because the old thread is still registered,
        then the old thread wakes, sees a stale route, exits and clears the
        slot. Nobody is left polling, and since only the render path starts
        one, the panel is frozen until something else rebuilds it.

        ``restartable`` makes the departing thread ``invalidate()`` once it
        has released the slot. That re-runs the view, which starts a poller
        iff it still wants one — no queued body to re-arm, so a request that
        has itself gone stale simply isn't honoured. Opt-in because
        ``_arm_toast_clear`` releases its slot early by design and would
        invalidate on a timer that is still live.
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
                    released = False
                    with self._poller_lock:
                        if getattr(self, attr) is thread:
                            setattr(self, attr, None)
                            released = True
                    if released and restartable:
                        # Wake the loop now that the slot is free: a request
                        # refused while we were on our way out would
                        # otherwise leave nobody polling. The rebuild starts
                        # a fresh poller only if the view still wants one.
                        self.invalidate()

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

    def set_source(self, source, server_uuid=None, keep_place=False):
        """Swap in a live data source once servers connect (the browser opens
        immediately on a spinner and populates when the network settles).

        A catalog-backed source raises the offline banner: every path that
        can land offline goes through here, so deriving the banner from the
        source is what keeps the two from drifting apart.

        ``keep_place=True`` refreshes in place instead of resetting to Home.
        Use it for anything that is not a deliberate user action. A *reconnect*
        arrives from the websocket redial loop, the cast-recovery path and the
        periodic health check — i.e. at arbitrary moments mid-session — and
        resetting the nav stack there threw the user out of whatever they were
        reading, with no interaction on their part, every time a flaky server
        bounced.
        """
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
        server = self._pick_server(servers, server_uuid)
        # Keeping your place only makes sense if the page you are on still
        # belongs to a server this source has. Otherwise fall back to Home.
        known = {s.get("uuid") for s in servers}
        stay = (keep_place and self.nav_stack
                and self.server == server
                and all(r.get("server") in known or r.get("server") is None
                        for r in self.nav_stack))
        self.server = server
        if not stay:
            self.nav_stack = [self._default_route()]
        self._bump_epoch()
        self._load_route(self.route)
        self._refresh_downloaded()
        # The idle cast backdrop is picked from a random library item, so it
        # needs a reachable server. At startup the cast screen composites
        # before the connect finishes, finds no clients, and caches "no
        # backdrop" — permanently, because that cache is what stops the
        # picture re-rolling on every window resize. Re-roll now that there
        # is something to ask. Only when it is actually showing the idle
        # screen: a DisplayContent item must not be thrown away.
        if (self.route.get("kind") == "cast"
                and (self._cast or {}).get("idle")):
            self.show_cast_idle()
        self.invalidate()

    def _follow_cast_to_playback(self, state, track_changed):
        """Keep the headless cast screen showing the current track.

        Stopping goes back to "Ready to cast" rather than leaving the last
        thing played on screen, which reads as though it is still playing.
        """
        if not state or state.get("stopped"):
            if not (self._cast or {}).get("idle"):
                self.show_cast_idle()
            return
        if not (state.get("is_audio") and track_changed and now_id_of(state)):
            # Video takes the whole window, so the cast screen is not
            # visible and there is nothing to update.
            return
        server = state.get("server_uuid") or self.server
        if server is not None:
            self.display_cast_item(server, now_id_of(state))

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
        # Load feedback outranks everything, including the yield to video:
        # that yield is exactly what left a blank window during a load, and a
        # failed start has no video to show through.
        if self._load_error is not None:
            return self._load_error_scene(size)
        if (self._starting is not None and not self._browsing
                and self._spinner_due()):
            return self._loading_scene(size)
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
        # After content: _render_route (above) ran _ensure_page and set the
        # page count this bar reads. Sits above the now-playing bar.
        pbar = self._pagination_bar(route, w)
        if pbar is not None:
            children.append(pbar)
        if (self._now_playing is not None
                and route["kind"] not in NO_NOW_PLAYING):
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
        #
        # The probe and the real bar are built from one snapshot of the
        # servers and users. Building the bar twice per frame asked the
        # source and the controller for both lists twice, on the loop
        # thread — cheap today, but it is a list_users() and a servers()
        # round the render path that nothing forced to be cheap.
        servers, users = self._chrome_lists()
        probe = self._chrome_bar(compact=False, probe=True,
                                 servers=servers, users=users)
        compact = natural_size(probe)[0] + self.TITLE_MIN_W > w
        return self._chrome_bar(compact=compact, servers=servers, users=users)

    def _chrome_lists(self):
        try:
            servers = self.source.servers()
        except Exception:
            servers = []
        return servers, self._users()

    def _chrome_bar(self, compact, probe=False, servers=None,
                    users=None):
        title = "" if probe else (self.route.get("title") or _("Home"))

        def nav_button(label, node_id, icon, cb):
            # Icon-only when compact — the icons are the same ones the
            # labels sit next to, so nothing new has to be learned. The
            # tooltip carries the label in that state: compact is exactly
            # when the button stops saying what it does, and it was the
            # only mode with neither a label nor a tip.
            return Button("" if compact else label, id=node_id, icon=icon,
                          on_click=cb, tip=label if compact else None,
                          bg=theme.BUTTON_BG, border=theme.ACCENT, border_w=1,
                          radius=9, hover={"fill": theme.BUTTON_ACTIVE})

        left = []
        if len(self.nav_stack) > 1:
            left.append(nav_button(_("Back"), "nav-back", "arrow_back",
                                   self.go_back))
        left.append(nav_button(
            _("Home"), "nav-home", "home",
            lambda: self.navigate({"kind": "home", "server": self.server},
                                  reset=True)))

        right = []
        if servers is None:
            servers = self._chrome_lists()[0]
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
        if users is None:
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
                   tip=_("Search"), bg=theme.BUTTON_BG, border=theme.ACCENT,
                   border_w=1, radius=9, hover={"fill": theme.BUTTON_ACTIVE},
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

    def _pick_server(self, servers, server_uuid=None):
        """Choose which server the library opens on.

        An explicit request wins; then the server this user last browsed, if
        it is still in the list (it may have been removed, or be down this
        launch); then the first one. That fallback is not a preference —
        server order is connection order, which sorts by network locality, so
        without the remembered value the default silently changes between
        launches on a multi-server setup.
        """
        if not servers:
            return None
        known = {s.get("uuid") for s in servers}
        if server_uuid:
            if server_uuid in known:
                return server_uuid
            # Asked for a server this source does not have. The reconnect path
            # passes the CURRENT selection back in, and offline that selection
            # is the "offline" sentinel from OfflineLibrarySource.servers() —
            # handing it to a live source made every subsequent call blow up
            # with KeyError: 'offline' until a restart. A removed or
            # not-yet-connected server lands here too.
            log.info("server %r is not in this source; picking another",
                     server_uuid)
        # getattr, not a direct call: the browser is unit-tested with stub
        # controllers (and runs with controller=None offline).
        getter = getattr(self.controller, "get_last_server", None)
        if getter is not None:
            try:
                last = getter()
            except Exception:
                log.debug("could not read last server", exc_info=True)
            else:
                if last and last in known:
                    return last
        return servers[0]["uuid"]

    def _remember_server(self, uuid):
        """Persist the browsed server. Best-effort — losing a preference must
        never break navigation."""
        setter = getattr(self.controller, "set_last_server", None)
        if setter is None:
            return
        try:
            setter(uuid)
        except Exception:
            log.debug("could not persist last server", exc_info=True)

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
        self._remember_server(uuid)
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
        (paging). Only re-renders when the offset has moved a window's worth
        SINCE THE LAST RE-RENDER -- measured from the last rendered position,
        not the previous event. Continuous (sub-row) scrolling arrives in many
        small steps; comparing adjacent events would let a slow scroll drift a
        whole window without ever crossing the gap, and the virtualized rows
        would fall out of the built window as blank spacers until a bigger
        (coalesced) jump finally tripped it."""
        self._scroll_off[scroll_id] = offset
        if then is not None:
            then(offset, maximum)
        base = self._scroll_rendered.get(scroll_id)
        if base is None or abs(offset - base) >= self.SCROLL_STEP:
            self._scroll_rendered[scroll_id] = offset
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

    def shutdown(self, free_bitmaps=True):
        """Stop background work.

        ``free_bitmaps=False`` keeps the composited tile buffers alive. On
        libmpv those are read BY ADDRESS by mpv every frame it composites, so
        they may only be released once mpv is genuinely dead — the caller
        knows that, this does not. See mpvtk_browser.ui.stop().
        """
        self._shutdown_evt.set()   # also stops the downloads poller
        self._pool.shutdown(wait=False, cancel_futures=True)
        # Relocating the download store copies the whole thing and has no
        # cancellation check, so a quit mid-move would kill it partway
        # through. Give it a bounded chance to finish rather than yanking
        # the interpreter out from under a half-copied library.
        long_thread = self._long_thread
        if long_thread is not None and long_thread.is_alive():
            log.info("waiting for a long job to finish before shutdown")
            long_thread.join(timeout=self.LONG_JOB_SHUTDOWN_WAIT)
            if long_thread.is_alive():
                log.warning("long job still running at shutdown; "
                            "it may be left incomplete")
        if self.thumbs is not None:
            self.thumbs.shutdown()
        # Stop the compositor pool before touching its cache either way: a
        # worker must not insert a buffer into a cache we're about to free
        # (free_bitmaps) or leave one composing into a dead handle.
        self.strips.shutdown()
        if free_bitmaps:
            self.strips.clear()
