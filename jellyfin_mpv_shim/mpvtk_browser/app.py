"""MpvtkBrowser -- the app shell: route stack, async data loading, and the
``build(size)`` that turns the current route into an mpvtk widget tree.

Runs in the main process next to ``playerManager`` and attaches its UI to the
player's mpv window via ``mpvtk.MpvtkApp.attach`` (see ``mpvtk/GUIDE.md``).

This module is the *core*: ``__init__``, the nav stack, the epoch and
``run_async``, ``_load_route``, ``build``/``_render_route``, the chrome, the
browse<->playback lifecycle and HUD glue, and ``shutdown``. Around it are the
Pages (``pages/``, where a route should go) and the mixins (what has not been
converted yet, plus the app-wide surfaces that are not routes). The class
composition, the Page/ROUTES fallback and the mixin partition rule are in
docs/browser-shell.md section 1.

**Adding a view means adding a Page**: subclass ``pages.base.Page``, give it a
``kind``, register it in ``pages/PAGES``.

Three invariants hold the whole thing together, and each has a test:

**The thread contract.** Renderer event handlers and ``build()`` run on the loop
thread. ``on_playstate``, ``notify_update``, ``set_download_status``,
``display_item`` and ``on_downloads_changed`` are called from foreign threads, as
are the pool workers behind ``run_async`` -- everything they touch must be
write-then-``invalidate()``, never a direct scene change.

**Epoch discipline.** ``_epoch`` and ``_lock`` live *only* here. Dispatchers read
``ep = self._epoch`` on the loop thread and hand it to ``run_async``, which drops
the result if navigation has moved on. Caching an ``ep`` and passing it across a
module boundary reads fine and is subtly wrong.

**``_lock`` protects writers from each other, not from the reader.** ``build()``
reads route data unlocked. That is safe only because every writer ends with
``invalidate()``, so a torn read is a one-frame glitch the next build heals.
Don't "fix" it by locking ``build()``.

A widget tree is a snapshot, so a handler must read mutable state *inside*
itself, and a handler that changes state must ask for a repaint -- a Checkbox
cannot update itself. Both have shipped as bugs; see docs/browser-shell.md
section 3.
"""

import logging
import threading
import time
from types import SimpleNamespace

from ..i18n import _
from ..mpvtk.layout import natural_size
from ..mpvtk.rawimage import cache_dir
from ..utils import memory_is_tight
from ..mpvtk.widgets import (
    Box,
    Busy,
    Button,
    Column,
    Dropdown,
    Float,
    Gradient,
    Icon,
    Progress,
    Row,
    Spacer,
    Stack,
    Text,
    TextBox,
)
from . import theme
from . import navigator
from .async_runner import AsyncRunner
from .navigator import Navigator
from .scroll_state import ScrollState
from .tile_renderer import TileRenderer
from .item_actions import ItemActions
from .hud_control import HudController
from .load_feedback import LoadFeedback
from . import window_chrome
from . import pagination
from .pagination import Paginator
from .pages import PAGES
from .pages.base import PageContext
from .hud import build_hud
from ..books import AUDIOBOOK_TYPE, BOOK_TYPE
from .repository import (BOOKS_COLLECTION, FOLDER_TYPES, LIVE_TV_COLLECTION,
                         LIVE_TYPES, PHOTO_TYPE, PLAYABLE_TYPES, SERIES_TYPES)
from .strips import (BANNER_GEOM, LANDSCAPE_GEOM, POSTER_GEOM, SQUARE_GEOM,
                     StripStore, TileGeom)
from .dialogs import DialogsMixin
from .livetv_dialogs import LiveTvDialogsMixin
from .auth import AuthMixin
from .settings import SettingsMixin
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
# The reader is here for the same reason the cast screen is: it is a
# full-bleed page with its own bar, and the library chrome above it would be
# a second back button and a search box over a book.
CHROME_FREE = {"login", "locked", "connecting", "cast", "reader", "comic"}

# Where the now-playing bar must NOT appear. Deliberately not CHROME_FREE:
# the cast screen is chrome-free but IS where audio playback lives in
# headless mode, and suppressing the bar there would leave a cast-target box
# playing music with no transport controls at all — worse than the library
# access it was meant to deny. The other three are pre-library screens where
# nothing can be playing.
NO_NOW_PLAYING = {"login", "locked", "connecting"}

# Route kinds whose data goes stale on its own. Everything else in the
# library changes only when this user changes it, so it is loaded once per
# navigation and left alone; Live TV is the exception in both directions —
# a programme ends, a recording starts, and another client can set a timer
# on the screen you are looking at. The server pushes the timer half (see
# EventHandler.LIVE_TV_EVENTS) and has no message at all for the rest, which
# is why these also poll.
LIVE_KINDS = {"livetv", "channel", "program"}

#: How often a Live TV route re-reads itself. jellyfin-web's own staleness
#: guard is five minutes, but it re-renders on every tab change and this
#: screen is often left sitting on the Guide — a two-minute floor keeps "on
#: now" meaning now without making the guide fetch a background job.
LIVE_POLL_SECS = 120


class MpvtkBrowser(DialogsMixin, LiveTvDialogsMixin, AuthMixin, SettingsMixin,
                   MusicMixin, ViewsMixin, TilesMixin, CastMixin):

    # Horizontal padding of ordinary page content.
    CONTENT_PAD = 16

    # Pagination (settings.paginated). A page is one screenful of tiles with a
    # bottom bar instead of a scrollbar. Heights below are the fixed chrome/bar
    # heights the page-size math subtracts so a page fits without scrolling;
    # they mirror the real widgets (_chrome h=60, _banner/_download_bar, the
    # now-playing bar) and only need to be close — the row count rounds down, so
    # an over-estimate just shows one fewer row rather than clipping.
    PAGINATION_BAR_H = pagination.PAGINATION_BAR_H
    CHROME_H = 60
    BANNER_H = 48
    DLBAR_H = 44
    # Cap a page's tile count so a huge window can't blow the 63-overlay budget
    # (a non-scrolling page composites every tile at once).
    PAGE_MAX = pagination.PAGE_MAX
    # Route kinds that paginate. The music songs list and genre grids stay
    # scrolling (a list, and an unpaged single request).
    #
    # "list" is here because ``ListPage`` subclasses ``GridPage`` and so
    # inherits its render -- it draws a *paged* grid whenever the setting is
    # on, whether or not this set names it. Leaving it out did not make the
    # Networks screen or a "see all" listing scroll; it drew one page of
    # tiles and no bar to leave it with, which is #638 and #639.
    PAGEABLE_KINDS = {"grid", "list", "person", "music"}

    # How long shutdown() waits for a long job (a download-store move) to
    # finish before giving up on it. Long enough to cover a same-drive move,
    # short enough not to hang a quit.
    LONG_JOB_SHUTDOWN_WAIT = 20.0

    def __init__(self, app, source, strips=None, thumbs=None,
                 server_uuid=None, geom=None, controller=None, config=None):
        # Before anything is built: apply the user's chosen theme. Widgets
        # read the palette when they are constructed, so this has to be in
        # place before the first build.
        from ..conf import settings as _settings
        self._apply_theme(getattr(_settings, "theme", "default"))
        self.app = app            # mpvtk.MpvtkApp (attached or spawned)
        self.source = source
        # Settings accessor (settings_schema/get_settings/set_setting). None ->
        # the real in-process config module; tests inject a fake.
        self._config_obj = config
        # Optional bridge to the player (playback + browse/play window state).
        # None in tests -> playable clicks just report status; the window/OSC
        # handoff is a no-op. See mpvtk_browser.player_gateway.PlayerGateway.
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
        # Playback HUD state and events (hud_control.py); the renderer owns
        # the summon/auto-hide lifecycle and reports it there. Built before
        # set_app below, which wires the renderer's callbacks onto it.
        self.load = LoadFeedback(
            get_controller=lambda: self.controller,
            invalidate=lambda: self.invalidate(),
            status=lambda msg: self.set_status(msg),
            is_browsing=lambda: self._browsing,
            enter_browse=lambda: self.enter_browse(),
            hand_off=lambda: self._yield(),
            arm=lambda: self._arm_spinner())
        self.hud = HudController(
            get_app=lambda: self.app,
            get_controller=lambda: self.controller,
            invalidate=lambda: self.invalidate(),
            ctl=lambda fn: self._ctl(fn),
            start_ticker=lambda: self._start_np_ticker())
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
        # See navigate() and tests/test_mpvtk_headless.py for what this
        # does and does not protect against. (That test is the live
        # substitute; the mpvtk/HEADLESS.md this used to cite has never
        # existed in any commit.)
        self.headless = bool(self._cfg_headless())
        # Wires on_hud/on_hud_skip (and re-wires on_nav) on the app —
        # shared with mpv re-creation, which attaches a fresh app.
        self.set_app(app)
        # Poller that refreshes the downloads view while transfers run.
        self._dl_thread = None
        # Tail poller for the logs tab — see SettingsMixin._poll_logs.
        self._log_thread = None
        # Debounce slot for UserDataChanged — see refresh_home.
        self._userdata_thread = None
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
        # Settings changed in this session that need a restart before they
        # do anything (config.RESTART_REQUIRED). Keys, not labels, so the
        # banner can translate them at draw time -- and a set, so changing
        # the same one twice is still one entry.
        #
        # Session state on purpose, not persisted: a restart clears it by
        # definition, and a quit that is not a restart still applies
        # everything at the next launch. Carrying it across launches would
        # mean the banner outlived the thing it was about.
        self._restart_keys = set()
        #: Whether this launch can restart itself, asked once. See
        #: can_restart.
        self._can_restart = None
        # Client-side decorations: draw our own title bar because the desktop
        # is not drawing one. Pushed by refresh_window_controls rather than
        # read per frame -- the answer is an mpv property, and on the jsonipc
        # backend a property read is an IPC round trip.
        # Screens left, and the count the caches were last shed at. Bumped
        # by the navigation itself rather than derived from the async epoch;
        # see _shed_caches_on_screen_change.
        self._screen_seq = 0
        self._shed_seq = 0
        #: The Page currently on screen, so the one before it can be told
        #: it is not. See _retire_page.
        self._live_page = None
        self._csd = False
        self._maximized = False
        # Modal dialog: a builder callable -> Dialog node, or None.
        self._dialog = None
        # Download dialog state {"server","item","est","watched"} or None.
        self._dl = None
        # Live TV modals (livetv_dialogs.py): the recording editor and the
        # guide's view settings. Same single-slot convention as _dl.
        self._timer_dlg = None
        self._guide_dlg = None
        # Login form field values (renderer holds the live text; we mirror it
        # here via on_change so Connect can read all three fields at once).
        self._login = {"server": "", "user": "", "pass": ""}
        self._login_error = None
        # Live text of the chrome search box (the renderer owns the widget; we
        # mirror it so the search *button* can read it).
        self._search_box = {"term": ""}
        # Live text of the "new user" box in Settings → Servers & Users.
        self._newuser = {"name": ""}
        # Startup-PIN lock screen state. _locked is True while the gate is
        # actually gating: tray commands that would navigate (Configure
        # Servers, Show Console) are swallowed while it is set, so they
        # can't reveal content from behind the lock.
        self._pin = {"pin": ""}
        self._pin_error = None
        self._locked = False
        # The four tile shapes, from the theme and the Cover Size setting.
        # A fixed ``geom`` (the integration harness passes one) pins the
        # poster shape and opts out of later re-derivation.
        self._geom_pinned = geom
        self._derive_cover_size()
        # Downloaded id sets (for the tile badge), refreshed from the sync db.
        # Default to a file-backed store (works on both backends / headless);
        # the libmpv integration passes a MemoryStore-backed one.
        self.strips = strips or StripStore(
            cache_dir=cache_dir("mpvtk-browser-"), geom=self.geom)
        # Wake our loop when an async row composite lands (see StripStore.strip).
        # self.invalidate reads self.app at call time, so this survives mpv
        # re-creation without re-wiring in set_app.
        self.strips.set_notify(self.invalidate_art)
        self.thumbs = thumbs      # ThumbnailStore (optional; None -> no art)
        if self.thumbs is not None:
            # Wake our loop when a decoded poster lands, so build() can pump it.
            self.thumbs.set_notify(self.invalidate_art)

        servers = []
        try:
            servers = source.servers()
        except Exception:
            log.warning("could not enumerate servers", exc_info=True)
        self.server = self._pick_server(servers, server_uuid)

        # Epoch + lock + worker pool, one mechanism with one owner. The
        # properties below keep `self._epoch` / `self._pool` reading exactly
        # as before for the mixins and tests that use them.
        self._async = AsyncRunner(invalidate=self.invalidate)
        # Late-bound on purpose. Passing the bound method captures whatever
        # `invalidate` is at construction, so anything that later replaces it
        # -- the tests do, and set_app-style rewiring could -- would be
        # ignored and the view would stop repainting on scroll.
        self._scroll = ScrollState(lambda: self.invalidate())
        # Every callback is late-bound for the same reason as the scroll
        # state's: `source`/`server` are swapped by set_source at arbitrary
        # moments, and `app` is replaced when mpv is re-created.
        self.tiles = TileRenderer(
            art=self, scroll=self._scroll,
            on_open=lambda item: self._open_item(item),
            nav_mode=lambda: self._nav_mode,
            get_app=lambda: self.app,
            on_play=lambda item: self._play_tile(item),
            can_play=lambda item: self._tile_playable(item))
        self.tiles.on_context = lambda *a, **k: self._open_tile_menu(*a, **k)
        # The action counterpart to TileRenderer. `services=self` is a live
        # provider (source/controller/offline/invalidate/set_status), read
        # through and never snapshotted -- see item_actions.py. `dialogs` is
        # a namespace rather than self so the only shell surface it can
        # reach is the two dialogs it actually opens.
        self._pages = Paginator(
            run=self._async,
            content_h=lambda route, size: self._content_h(route, size),
            is_current=lambda route: route is self.route,
            status=lambda msg: self.set_status(msg),
            invalidate=lambda: self.invalidate(),
            enabled=pagination.enabled_from_settings,
            cols=lambda w, geom: self.tiles.cols(w, geom),
            set_enabled=lambda v: self._config().set_setting("paginated", v),
            forget=lambda *ids: self._scroll.forget(*ids))
        self._dialogs = SimpleNamespace(
            confirm=lambda *a, **k: self._confirm(*a, **k),
            message=lambda *a, **k: self._message(*a, **k),
            add_to=lambda item, server=None: self._open_add_to(item, server),
            open_download=lambda item: self._open_download(item),
            timer_editor=lambda server, timer, series=False, on_change=None:
                self._open_timer_editor(server, timer, series, on_change),
            guide_settings=lambda server, prefs, categories, on_save=None:
                self._open_guide_settings(server, prefs, categories, on_save),
            view_settings=lambda current, on_set, paginated=None:
                self.view_settings(current, on_set, paginated),
            open_book_progress=lambda item, server=None:
                self._open_book_progress(item, server),
            media_info=lambda item, server=None:
                self._open_media_info(item, server),
            filter_panel=lambda get_vals, get_filters, get_count, on_set,
                on_toggle, on_clear, collection_type=None: self.filter_panel(
                    get_vals, get_filters, get_count, on_set, on_toggle,
                    on_clear, collection_type=collection_type))
        self._actions = ItemActions(
            services=self, run=self._async,
            dialogs=self._dialogs,
            on_launch=lambda audio, title: self._start(audio=audio,
                                                       title=title),
            on_downloads_changed=lambda: self._refresh_downloaded())
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
        # Book reading-position dialog state + its rebuilder (see dialogs).
        self._bkprog = None
        self._bkprog_build = None
        # transient status message + when it was set (see _toast_node)
        self._status_at = 0.0
        self._toast_timer = None
        # Repaints once a load has been slow enough to deserve the spinner.
        self._spinner_timer = None
        # Re-read loop for the Live TV routes; see _poll_live_tv.
        self._livetv_poll = None
        # live text of the download-folder field
        self._sync_path = {}
        self.status = ""
        self._size = None         # last window size seen by build()

        self._nav = Navigator(self._default_route,
                              is_headless=lambda: self.headless)
        self._load_route(self.route)

    # ------------------------------------------------------------ routing

    def _default_route(self):
        """Where the browser lands when it has nowhere specific to go.

        Handed to the Navigator as a callback rather than a value, because
        every stack-emptying path backfills through it and it must reflect
        the headless flag *at that moment*. That is what makes
        ``Navigator.replace`` safe to leave unfiltered.

        This used to carry a warning that every direct ``nav_stack``
        assignment must come through here — a successful connect once put a
        headless box on the library because ``set_source`` reset the stack
        itself and the refusal never ran. The stack is now private to the
        Navigator, so the warning is an invariant instead
        (``tests/test_source_invariants.py``).
        """
        if self.headless:
            return {"kind": "cast"}
        return {"kind": "home", "server": self.server}

    @property
    def route(self):
        return self._nav.route

    @property
    def nav_stack(self):
        """The live route stack. Read freely; every WRITE goes through the
        Navigator, which is what makes the headless lockdown hold."""
        return self._nav.stack

    @nav_stack.setter
    def nav_stack(self, routes):
        # Kept as a settable attribute because tests and the snapshot harness
        # place the browser on a screen directly. It routes through
        # Navigator.replace rather than rebinding a list, so there is still
        # exactly one owner.
        self._nav.replace(routes)

    #: Kept as a class attribute: tests/test_mpvtk_headless.py reads it, and
    #: it is the published name for "what headless still allows".
    HEADLESS_ROUTES = navigator.HEADLESS_ROUTES

    def navigate(self, route, reset=False, force=False):
        """Go to ``route``, then load and repaint it.

        Every way into the library ends up here — a tile click, the tray's
        "Show Library Browser", a remote's GoHome, the now-playing bar's
        Queue button, a DisplayContent from a phone — which is what lets the
        headless lockdown mean something rather than hiding one entry point
        and leaving five others open. The refusal itself is the Navigator's
        (see navigator.py); this method's job is that a refusal skips the
        load and the repaint too.

        ``force`` is for the screens headless itself needs to reach.
        """
        # Park before the push: `self.route` is still the screen being left,
        # and _reset_scroll below is about to forget where everything was.
        self._park_scroll()
        if not self._nav.push(route, reset=reset, force=force):
            return          # refused by the headless lockdown
        # The tile under the pointer is not on the next screen. The renderer
        # would only say so on the next pointer MOVE, and a navigation
        # triggered by a click leaves the pointer exactly where it was.
        self.tiles.set_hover(None)
        self._reset_scroll()
        self._screen_seq += 1
        self._bump_epoch()
        self._load_route(route)
        # Ask the renderer to land focus on the page's own default action —
        # a movie's Play. Asked for every navigation and answered by very
        # few: only a page that nominates a node has one, and the renderer
        # ignores the request outright while the pointer is driving, so
        # clicking a tile with a mouse is unaffected. Parked until the page
        # stops being a spinner (see MpvtkApp.focus).
        focus = getattr(self.app, "focus", None)
        if focus is not None:
            try:
                focus()
            except Exception:
                log.debug("autofocus request failed", exc_info=True)
        self.invalidate()

    def _poll_live_tv(self, route):
        """Re-read a Live TV route every ``LIVE_POLL_SECS`` while it is up.

        Started from the render path, like the logs tail and the downloads
        list, and ``restartable`` for the same reason: the thread only
        notices the route has changed on its next tick, so leaving and
        coming straight back would otherwise find the slot still taken and
        leave nobody polling.
        """
        def tick():
            while not self._shutdown_evt.wait(LIVE_POLL_SECS):
                if self.route is not route or not self._browsing:
                    break
                self.refresh_live_tv()

        self._start_daemon("_livetv_poll", "mpvtk-livetv", tick,
                           restartable=True)

    def refresh_live_tv(self, _client=None):
        """Re-fetch the Live TV screen, if that is what is showing.

        Reached from the websocket thread (a timer created or cancelled,
        possibly by another client entirely) and from the poller above, so it
        must be safe off the loop thread and cheap when it does not apply.

        A **load, not a reload**: the epoch stays put, nothing in flight is
        cancelled, and every Live TV loader writes in place -- a refresh nobody
        asked for must not blink a spinner over what they are reading.

        **Deferred, never forced, while the user is mid-interaction**, and it
        marks the route ``_loading`` while it runs so a scroll cannot page in
        against a list this is about to replace. ``_route_async`` clears it
        however the load ends. Both halves, and why scroll survives:
        docs/browser-shell.md section 4.
        """
        route = self.route
        if route.get("kind") not in LIVE_KINDS or self.source is None:
            return
        if self.server is None:
            return          # _load_route would not dispatch, so nothing would
            #                 clear the marker below
        if self._menu is not None or self._dialog is not None:
            return
        if route.get("_loading") or route.get("_refreshing"):
            return
        route["_refreshing"] = True
        self._load_route(route)

    #: How long a UserDataChanged burst settles before Home re-reads.
    #:
    #: Not as large a burst as it once said here. That comment claimed the
    #: server sends one per progress report, including for our own playback,
    #: so watching a film would refetch Home every few seconds; measured
    #: against 10.11.11 and 12.0.0, progress saves are dropped before the
    #: message is built and three progress reports produce zero events. What
    #: is left is a start, a stop, and whatever anyone marks by hand -- the
    #: server already coalescing 500 ms of those into one message. So this
    #: is settling a handful of events, not a stream, and it stays because
    #: a handful still arrives together and Home is several requests.
    USERDATA_DEBOUNCE = 3.0

    def refresh_home(self, _client=None, now=False):
        """Re-read the home screen, if that is what is showing.

        Continue Watching and Next Up are the only library rows a *third party*
        changes while you are looking at them (#560), and a stale one is not
        cosmetic -- it offers to resume something already watched, and pressing
        it starts it over.

        A **load, not a reload**, exactly like refresh_live_tv. Reached from the
        websocket thread, so it must be safe off the loop thread.

        ``now`` skips the debounce, for the caller that is not a burst: coming
        back from playback (see enter_browse). See docs/browser-shell.md
        section 4.
        """
        if self.route.get("kind") != "home" or not self._browsing:
            return
        if now:
            if self._menu is None and self._dialog is None \
                    and not self.route.get("_loading"):
                self._load_route(self.route)
            return

        def tick():
            # _start_daemon keeps one thread per slot, so a burst of events
            # schedules exactly one re-read: the first arrival starts the
            # wait and the rest land while the slot is taken.
            self._shutdown_evt.wait(self.USERDATA_DEBOUNCE)
            route = self.route
            if route.get("kind") != "home" or not self._browsing:
                return
            # Deferred, never forced, while the user is mid-interaction --
            # see refresh_live_tv for the full reasoning. Skipping costs
            # nothing here: the next event is seconds away, and returning to
            # Home re-reads anyway.
            if self._menu is not None or self._dialog is not None:
                return
            if route.get("_loading"):
                return
            self._load_route(route)

        self._start_daemon("_userdata_thread", "mpvtk-userdata", tick)

    def _reload_route(self, route):
        """Re-run a route's loader in place: the data changed, the screen did
        not. A sort or filter change, the collections toggle. Distinct from
        navigate(), which pushes a new screen, and from go_back()."""
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


    def go_home(self):
        """Go to the home screen, reusing the one the stack already holds.

        The route dict *is* the page cache, so pushing a fresh one means
        ``chrome.busy()`` until a whole home fetch lands -- on the screen the
        user almost always already has. Reuse is safe rather than stale because
        Home re-reads itself in place. Scroll comes back with the route.

        Pressing Home while already on Home still goes the whole way round: the
        epoch and ``_screen_seq`` both move, so the caches shed and the rows
        re-read. That is deliberate -- it is the one gesture that means "reload
        this". **Do not "fix" it by skipping the bump when the route is
        unchanged.**

        The server is part of the match: switching servers pushes its own home
        (``_switch_server``) and must not land on the previous one's rows. A
        stack with no usable home falls through to a fresh route, which is also
        what keeps headless refusing. See docs/browser-shell.md section 5.
        """
        for route in reversed(self.nav_stack):
            if (route.get("kind") == "home"
                    and route.get("server") == self.server):
                self.navigate(route, reset=True)
                return
        self.navigate({"kind": "home", "server": self.server}, reset=True)

    def go_back(self):
        self._park_scroll()
        left = self._nav.pop()
        if left is not None:
            self._land_back([left])

    def _land_back(self, left):
        """Settle on whatever a back move landed on. ``left`` is the routes
        it left, nearest first — one for a Back press, several for a jump
        through the history menu.

        Shared so the two cannot diverge: the menu's jump used to reload
        only Home, so jumping past the playlist editor showed the pre-edit
        membership as fresh while pressing Back the same number of times
        refetched it.
        """
        self._reset_scroll()
        self._screen_seq += 1
        self._bump_epoch()
        # Stale-while-revalidate: refresh Home on return (watched/resume
        # state may have changed) while showing the cached view meanwhile.
        if self.route.get("kind") == "home":
            self._load_route(self.route)
        # Coming out of the playlist editor, whatever is underneath is
        # showing the order and membership from before the edits. Asked of
        # every route left, not just the nearest: a jump can step over the
        # editor from further in.
        elif (any((r or {}).get("kind") == "playlist_edit" for r in left)
              and self.route.get("kind") in ("playlist", "grid")):
            self.route.pop("_data", None)
            self.route.pop("_items", None)
            self.route.pop("_loading", None)
            self._load_route(self.route)
        # The page we left was DELETED out from under itself. Whatever is
        # underneath still lists it, so it draws a tile pointing at nothing
        # — which invites a second press, and the second press 404s.
        #
        # Flagged on the route rather than detected here, because only the
        # page that deleted knows: a detail screen is left for a dozen
        # reasons, and re-reading the list on all of them would refetch a
        # grid every time somebody looked at a film and came back. Asked of
        # every route left, like the playlist-editor case above, so a jump
        # through the history menu behaves the same as one Back press.
        elif any((r or {}).get("_deleted") for r in left):
            self.route.pop("_data", None)
            self.route.pop("_items", None)
            self.route.pop("_loading", None)
            self._load_route(self.route)
        # Coming out of a reader: the position moved while it was open, and
        # it moved on the READER's copy of the DTO. The book page below
        # holds its own dict, fetched before any of that, so without this
        # it goes on showing the figure it was loaded with — "42% read"
        # under a Resume button that resumes at 61%.
        elif (any((r or {}).get("kind") in ("reader", "comic") for r in left)
              and self.route.get("kind") == "book"):
            self.route.pop("_data", None)
            self._load_route(self.route)
        self.invalidate()

    def go_forward(self):
        """Return to a page ``go_back`` left. Mouse-only (the thumb button),
        by design: it exists so an accidental Back is cheap to undo, which
        does not earn a permanent arrow in the top bar. The history menu on
        the Back button is where it becomes visible.

        No stale-while-revalidate counterpart to go_back's Home reload: a
        page reached by going forward was on screen moments ago, and the one
        case that reloads on the way back — leaving the playlist editor —
        cannot be ahead of you, because editing pushes rather than pops.
        """
        self._park_scroll()
        self._land_forward(self._nav.unpop())

    def go_forward_to(self, depth):
        """Jump forward to the page ``depth`` deep — the history menu's
        pick on the other side of the current page."""
        self._park_scroll()
        self._land_forward(self._nav.fast_forward(depth))

    def _land_forward(self, route):
        """Settle on a route the forward stack gave back. None means
        nothing moved: an empty stack, or the headless lockdown."""
        if route is None:
            return
        self._reset_scroll()
        self._screen_seq += 1
        self._bump_epoch()
        # A page can be left before its fetch ever landed — Back bumps the
        # epoch, which drops the result on the floor. Going *back* to such
        # a page is impossible (it was never below you), going forward to
        # one is a press away, and nothing else re-issues the load: the
        # render path spins on a route with no data and no error. Same
        # shape as _retry_route, including clearing the paging guard,
        # which would otherwise cap the list for the rest of the session.
        if route.get("_data") is None and not route.get("_items"):
            route.pop("_loading", None)
            self._load_route(route)
        self.invalidate()

    def go_back_to(self, depth):
        """Jump back to the page ``depth`` deep in the stack (1 is the root)
        — the history menu's pick. Everything skipped goes onto the forward
        stack, and lands exactly as pressing Back that many times would."""
        self._park_scroll()
        left = self._nav.rewind_to(depth)
        if not left:
            return
        self._land_back(left)

    def after_playlist_deleted(self, playlist_id):
        """Drop every route pointing at a now-deleted playlist and reload
        whatever is left showing.

        A playlist page keys its id as ``item_id``; only ``parent_id`` was
        checked, so nothing was ever pruned and deleting a playlist left the
        user sitting on its now-dead page. The route we land on also has to
        re-fetch, or the grid we came from still lists the playlist."""
        self._nav.prune(
            lambda r: playlist_id not in (r.get("item_id"), r.get("parent_id")))
        route = self.route
        route.pop("_data", None)
        route.pop("_items", None)
        route.pop("_loading", None)
        self._pages.reset(route)
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
        GoToSettings), or onto the chrome (GoToSearch). Returns True when
        handled; the OSD menu has none of them, so for every other path
        settings still just opens the menu."""
        if self.headless:
            # A remote is input like any other. Declining here lets the
            # player fall back to its own OSD menu, which is transport-only.
            return False
        if name == "search":
            # jellyfin-web opens a search page; the search box lives in our
            # top bar on every screen, so putting the cursor in it is the
            # same gesture with one less screen. Nothing to focus unless
            # the library is actually up.
            if not self._browsing:
                return False
            focus = getattr(self.app, "focus", None)
            if focus is None:
                return False
            try:
                focus("nav-search")
            except Exception:
                log.debug("search focus failed", exc_info=True)
                return False
            return True
        if name == "settings":
            self.open_settings()
            return True
        if name == "home":
            # Home means the home screen, from anywhere — including over a
            # playing video, where it used to fall through to the legacy
            # OSD menu (the player declined the command while a video was
            # up, and the OSD menu was the fallback for everything it
            # declined). So stop first: "go home" cannot mean "go home
            # behind this film".
            #
            # Navigate before stopping. The browser is not on screen yet,
            # so the home route loads while the video is still up, and
            # stopping hands the window to a screen that has already
            # arrived rather than to a spinner. Same reason the HUD's own
            # Back button just stops: what is underneath is already right.
            self.go_home()
            # Audio and video are tracked in different places: `_now_playing`
            # is the now-playing BAR's state and is None for video, whose
            # playstate lives in hud.state. Asking either one alone stops
            # music and not films, or the reverse.
            if self._now_playing is not None or self.hud.state is not None:
                self._ctl(lambda c: c.stop())
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
        if self._nav.can_go_back:
            self.go_back()
            return True
        return False

    def _on_mouse_forward(self):
        """The mouse's forward button, from the renderer.

        Guarded where ESC is *layered*. Back peels one layer at a time
        because ESC means "out of this"; forward means "the page I left",
        and there is no sense in which a dialog or a context menu is
        between you and it — navigating underneath one would leave it
        floating over a page it was never opened from. So an open overlay
        makes this a no-op rather than something to close first.

        The renderer's mouse group is also live while the playback HUD is
        summoned, where the library is not on screen at all.
        """
        if not self._browsing or self._dialog is not None \
                or self._menu is not None:
            return
        self.go_forward()

    def _on_nav_mode(self, active):
        """Renderer 'nav' event: keyboard/remote engaged or the mouse
        took over. Repaint so modality-dependent chrome (carousel
        arrows) follows."""
        if active != self._nav_mode:
            self._nav_mode = active
            self.invalidate()

    # -- the async machinery, owned by AsyncRunner ------------------------
    #
    # These delegate rather than reimplement. `_epoch` and `_pool` stay
    # readable as attributes because ~30 dispatchers across the mixins do
    # `ep = self._epoch` on the loop thread, and several tests substitute a
    # synchronous pool; changing those is churn, not clarity.

    @property
    def _epoch(self):
        return self._async.epoch

    @property
    def _pool(self):
        return self._async.pool

    @_pool.setter
    def _pool(self, value):
        self._async.pool = value

    def _on_scene_pushed(self):
        """A scene reached the renderer (MpvtkApp.on_scene_pushed).

        The strip cache frees bitmaps mpv may be compositing, so it needs to
        count *pushes* and not builds -- a build can run twice for one push
        and can raise without pushing at all. See StripStore.on_scene_pushed.
        """
        if self.strips is not None:
            self.strips.on_scene_pushed()

    def _bump_epoch(self):
        """Invalidate every in-flight async result. Returns the new epoch."""
        return self._async.bump()

    def _shed_caches_on_screen_change(self):
        """Drop the decoded artwork of the screen just left.

        Decoded images are the most expensive thing held per picture -- a 4K
        backdrop is 33 MB decoded against ~400 KB on the wire -- and they exist
        only to composite tile strips. Once the screen is behind you they have
        nothing left to do.

        **Observed here, not done in navigate().** navigate() is reachable from
        mpv's event thread and the websocket, and this cache has no lock; a
        counter bumped there and read here turns a cross-thread call into a
        loop-thread observation.

        **Keyed on _screen_seq, not the async epoch** -- four things bump the
        epoch without leaving the screen, and shedding on those re-decoded the
        page the user was still looking at. See docs/browser-shell.md
        section 6.
        """
        seq = self._screen_seq
        if seq == self._shed_seq:
            return
        self._shed_seq = seq
        if self.thumbs is not None:
            self.thumbs.trim_memory()
        # The composited rows are a different trade and normally not worth
        # making: they are what makes going BACK instant, and back is the
        # most common move there is. Recompositing a screenful is 20-140ms
        # per row on a two-worker pool, behind placeholders, on a page whose
        # scroll position was just restored -- paid to reclaim memory that a
        # roomy machine was never short of. The 128 MiB LRU already sheds
        # the screens you do not return to.
        #
        # It IS worth making on a machine that is short, which is the whole
        # of the difference. Asked per screen change rather than once at
        # startup because "busy" is a state, not a property: the answer on a
        # laptop changes when something else wakes up. One small file read
        # on Linux, one syscall on Windows, and only on a navigation.
        if self.strips is not None:
            tight = memory_is_tight()
            # The composited rows are memory on both backends -- buffers in
            # this process on libmpv, files in a RAM-backed scratch dir on
            # mpv_ext -- so a short machine gets a smaller cache as well as
            # a shed. One probe answers both.
            self.strips.set_memory_pressure(tight)
            if tight:
                self.strips.trim_soon()

    # -------------------------------------------------------- async model

    def _apply_theme(self, name):
        """Make a theme current: palette, toolkit tokens, mpv's browse
        background. Everything here is idempotent and safe to repeat, which
        is what :meth:`set_theme` relies on."""
        self._theme_cfg = theme.apply(name)
        try:
            # player_window, not player: the constant lives there and is read
            # there, and `player` only re-exports the mixin. Assigning it on
            # `player` set an attribute nothing reads -- see set_browse_bg.
            from .. import player_window as _pw
            _pw.set_browse_bg(self._theme_cfg["browse_bg"])
        except Exception:
            # No player module (tests): the palette still applies, there is
            # just no mpv window whose background to set.
            log.debug("could not set the browse background", exc_info=True)
        # Glow is theme-driven; the toolkit forwards it to the renderer
        # alongside the tokens.
        theme.apply_to_toolkit(glow=self._theme_cfg.get("glow", False))
        log.info("theme: %s (accent %s, glow %s)",
                 self._theme_cfg.get("name", "?"), theme.ACCENT,
                 self._theme_cfg.get("glow", False))
        return self._theme_cfg

    def set_theme(self, name):
        """Change theme without restarting.

        Three things happen beyond re-applying the palette: the renderer needs
        the new tokens pushed (it draws text fields, dropdowns, scrollbars and
        tooltips itself), mpv's own ``background-color`` is a property rather
        than something the scene paints, and every composited strip has the old
        colours baked in, so the strip store is **retagged, not cleared** (see
        StripStore.tag).

        Tile *geometry* is deliberately not re-derived here -- see
        apply_cover_size and docs/browser-shell.md section 9.
        """
        cfg = self._apply_theme(name)
        if self.app is not None:
            try:
                self.app.push_theme()
            except Exception:
                log.debug("could not push the theme to the renderer",
                          exc_info=True)
        if self.strips is not None:
            self.strips.set_theme_tag(cfg.get("name", name))
        try:
            from .. import player as _player
            player = getattr(_player, "playerManager", None)
            if player is not None:
                player.refresh_browse_bg()
        except Exception:
            log.debug("could not repaint the mpv background", exc_info=True)
        self.invalidate()
        return cfg

    def _derive_cover_size(self):
        """(Re)compute the four tile geometries from the theme and the Cover
        Size setting.

        Split out of __init__ so the setting can be changed without a
        restart: everything downstream reads ``art.geom*`` at render time,
        and strip bitmaps are keyed on the geometry's own dimensions
        (StripStore._key), so nothing cached at the old size can be served
        at the new one.
        """
        import dataclasses

        from ..conf import settings as _settings

        # Cover size: the theme's default, overridden by the Cover Size
        # setting when it is set. Posters/square scale; a theme may also
        # override the landscape (library) tile's shape outright.
        cs = (getattr(_settings, "poster_scale", None)
              or self._theme_cfg.get("poster_scale", 1.0))
        lw, lh = self._theme_cfg.get("tile_landscape",
                                     (LANDSCAPE_GEOM.tile_w,
                                      LANDSCAPE_GEOM.tile_h))
        self.geom = self._geom_pinned or POSTER_GEOM.scaled(cs)
        # The stock shape stays the module singleton rather than an equal
        # copy, so identity comparisons against LANDSCAPE_GEOM keep working.
        # `scaled` returns `self` at 1.0, so that identity survives the
        # scaling below for as long as it is the stock shape at stock size.
        if (lw, lh) == (LANDSCAPE_GEOM.tile_w, LANDSCAPE_GEOM.tile_h):
            wide = LANDSCAPE_GEOM                     # 16:9 (episodes / video)
        else:
            wide = TileGeom(tile_w=lw, tile_h=lh,
                            caption_h=LANDSCAPE_GEOM.caption_h)
        # Scaled like the other three. This was the one shape Cover Size did
        # not reach, and it is not a rare one -- episodes, home videos, Live
        # TV listings and every library whose median artwork is landscape
        # come out of `auto_geom` in it. So turning covers up grew the film
        # posters and left every 16:9 row at stock size, which reads as the
        # setting half-working rather than as a deliberate exemption.
        #
        # The theme's own `tile_landscape` override is scaled too: it states
        # the tile's SHAPE (240x135 vs something wider), and cover size is
        # how big that shape is drawn -- the same relationship the poster has
        # with POSTER_GEOM.
        self.geom_wide = wide.scaled(cs)
        self.geom_square = SQUARE_GEOM.scaled(cs)     # 1:1 (music)
        # ~5.4:1. Only a user asking for the Banner image type reaches this.
        self.geom_banner = BANNER_GEOM.scaled(cs)
        # A theme may also pin the tile caption font outright -- smaller than
        # stock lets a long title show more of itself before it is
        # ellipsized, larger buys legibility at the cost of that. Section
        # headings are separate (heading_size) and unaffected.
        #
        # It no longer has anything to do with cover size: nothing under a
        # bigger cover is bigger any more. See TileGeom.scaled.
        tts = self._theme_cfg.get("tile_title_size")
        tss = self._theme_cfg.get("tile_sub_size")
        if tts or tss:
            def caption(g):
                return dataclasses.replace(
                    g, title_size=tts or g.title_size,
                    sub_size=tss or g.sub_size)
            self.geom = caption(self.geom)
            self.geom_wide = caption(self.geom_wide)
            self.geom_square = caption(self.geom_square)
        # ...and the user's text preference on top of whatever the theme
        # settled on. Last, so a theme that pins a caption size still gets
        # scaled rather than escaping the setting entirely -- the pin is an
        # opinion about proportion, not about legibility.
        # Imported here, not at module scope, like the other conf reads in
        # this file (import cycle: conf -> ... -> app).
        from ..conf import settings

        try:
            factor = float(getattr(settings, "ui_text_scale", 1.0) or 1.0)
        except (TypeError, ValueError):
            factor = 1.0
        try:
            floor = int(getattr(settings, "ui_text_min", 0) or 0)
        except (TypeError, ValueError):
            floor = 0
        if factor != 1.0 or floor:
            for name in ("geom", "geom_wide", "geom_square", "geom_banner"):
                setattr(self, name, getattr(self, name).with_text_scale(
                    factor if factor > 0 else 1.0, floor))

    def apply_cover_size(self):
        """Adopt a changed Cover Size without a restart.

        Theme-driven geometry stays restart-only (see set_theme); a control
        LABELLED Cover Size is the opposite case -- seeing it happen is the
        whole point, which is why it sat behind a restart and nobody could tell
        what the values meant (#616).

        Two things must be cleared for the **whole stack**, not just the current
        route, or going back lands on a stale one: parked scroll offsets (pixel
        positions into a list whose row pitch just changed) and every route's
        parked ``_grid_shape``, which GridPage resolves once per route on
        purpose. See docs/browser-shell.md section 9.
        """
        self._derive_cover_size()
        if self.strips is not None:
            self.strips.geom = self.geom
        for route in self.nav_stack:
            route.pop("_grid_shape", None)
            route.pop(self._scroll.PARK_KEY, None)
        self._scroll.reset()
        self.invalidate()

    def apply_logo_legibility(self):
        """Adopt a changed "Make logos more legible" without a restart.

        The plate behind a transparent logo is baked into the composited
        strip, so retag the store exactly as a theme change does -- the rows
        recomposite as they are next drawn, and the old bitmaps age out
        through the LRU rather than being freed under a running compositor.

        Nothing else moves: the geometry, the scroll offsets and the grid
        shapes are all unaffected by which colour goes behind a logo.
        """
        if self.strips is not None:
            self.strips.retag()
        self.invalidate()

    def invalidate(self):
        if self.app is not None:
            self.app.invalidate()

    def invalidate_art(self):
        """Repaint because a picture arrived.

        Throttled and coalesced by the app loop (MpvtkApp.ART_RENDER_INTERVAL)
        rather than drawn on the spot: a grid asks for a hundred thumbnails at
        once and the decode pool answers one at a time, so this is called in
        hundreds and each call would otherwise lay out the whole screen to
        change the picture on one tile."""
        if self.app is not None:
            self.app.invalidate(soon=True)

    def run_async(self, work, on_done, epoch, on_error=None, always=None):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if the epoch still matches (the user hasn't navigated away).

        The contract — why ``on_error`` is not epoch-gated, why ``always``
        exists, why every callback is individually guarded — lives with the
        implementation in ``async_runner.py``. Two callers rely on the
        "``on_error`` runs regardless of epoch" clause and guard the live
        screen themselves by testing ``route is self.route``: ``_route_async``
        before the offline fallback (``set_source`` discards the nav stack)
        and ``_page_more`` before its toast. ``_edit_call``'s toast is
        deliberately unguarded — the user pressed a button and the server
        refused, so they should be told wherever they now are.
        """
        self._async.run(work, on_done, epoch,
                        on_error=on_error, always=always)

    #: Epoch of the newest load dispatched for a route, stamped on its dict.
    LOAD_EP_KEY = "_load_ep"

    def _route_async(self, route, work, on_done, ep):
        """run_async for a route's data, recording a failure on the route so
        the view can say so and offer a retry instead of spinning."""
        # Which load owns this route's outcome. `failed` is deliberately not
        # epoch-gated (it is a rollback, and a route you have navigated off
        # must still be holding its error when you come back to it), and that
        # was only ever safe because a superseded load's route dict was one
        # the user had navigated AWAY from. The Home button now re-navigates
        # the dict it finds in the stack (see go_home), so a stale load can
        # be holding the route that is the screen again.
        route[self.LOAD_EP_KEY] = ep

        def failed(exc):
            # Paging guards must not survive the failure or the view stops
            # requesting anything for the rest of the session. Unconditional,
            # like before: the guard belongs to the request, not the screen.
            route.pop("_loading", None)
            log.info("route %r failed to load: %s", route.get("kind"), exc)
            if route.get(self.LOAD_EP_KEY) != ep:
                # A newer load owns this route, and the screen should reflect
                # that one's outcome. Without this, a hung server's request
                # timing out half a minute later writes an error over a home
                # screen that has since loaded fine -- and, for anyone with
                # downloads, drops them onto the offline catalog from a
                # working screen, because the identity test below is true
                # again.
                return
            # A rollback on this route's own dict, so it runs whether or not
            # the route is on screen: navigate off a view that then fails and
            # come back, and it must be holding the error and a Retry rather
            # than spinning.
            route["_error"] = _("Failed to load. Check the connection.")
            # The fallback is not a rollback: set_source throws the nav stack
            # away and drops the user on the offline home. Only do that while
            # this route is still the screen —
            # against a server that hangs rather than refuses, the failure can
            # arrive tens of seconds after the user has moved on, and yanking
            # them out of Settings mid-edit is worse than the error they
            # never saw.
            if route is self.route:
                self._offline_fallback(route)

        def settled():
            # `always`, so a background refresh (refresh_live_tv) releases its
            # marker however this ends — including the epoch-superseded case,
            # which runs neither callback. A marker left set would stop the
            # screen refreshing for the rest of its life.
            route.pop("_refreshing", None)
        self.run_async(work, on_done, ep, on_error=failed, always=settled)

    # Paging moved to pagination.Paginator (step 6c prep 3). These stay as
    # thin forwarders while unconverted routes still call them as methods.
    PAGE_SLOP = pagination.PAGE_SLOP

    def _paginated(self):
        return self._pages.enabled()

    def _content_h(self, route, size):
        """Vertical space the route content actually gets — the window minus
        the chrome and bars that sit above/below it in ``build``. Mirrors
        build()'s own conditions so a paginated page can size itself to fit.

        Stays here: only the shell knows which of its bars are up."""
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
            from .music import now_playing_bar_h
            # The bar is two rows for an audiobook and on a narrow window,
            # so its height is a function of what is playing -- subtracting
            # a constant lays the page out against the wrong remainder and
            # the bottom of it disappears behind the bar.
            h -= now_playing_bar_h(self._now_playing, size[0])
        return max(1, h)

    def _page_count(self, route, ps):
        return self._pages.page_count(route, ps)

    def _ensure_page(self, route, ps, fetch, seed=None):
        return self._pages.ensure(route, ps, fetch, seed)

    def _reset_pagination(self, route):
        self._pages.reset(route)

    def _page_go(self, route, page):
        self._pages.go(route, page)

    def _page_jump(self, route, text):
        self._pages.jump(route, text)

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
            Text(_("Page"), size="small", color=theme.SUBTLE_FG),
            # force: the box tracks the current page, so paging with the
            # buttons updates the number rather than leaving a stale edit.
            # on_commit as well as on_submit: ENTER jumps, and so does clicking
            # (or tabbing) out of the box. on_commit only fires when the value
            # actually changed from focus-time, and ENTER marks it agreed, so
            # the two never double-fire for one edit.
            TextBox("pg-jump", text=str(cur + 1), w=64, force=True,
                    on_submit=lambda s: self._page_jump(route, s),
                    on_commit=lambda s: self._page_jump(route, s)),
            Text(_("of %d") % npages, size="small", color=theme.SUBTLE_FG),
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
        # The page cache goes with the items. `ensure` only rebuilds when the
        # page SIZE changes, so pages left from before the failure would be
        # served straight back -- and `_npages` left set draws the bottom bar
        # over the spinner of a route that is loading again from nothing.
        self._pages.reset(route)
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


    # -- scroll state, owned by ScrollState -------------------------------
    #
    # Thin forwarders: ~10 call sites across the mixins pass `self._on_scroll`
    # as a callback, and rewriting those is the page conversion's job, not
    # this extraction's.

    SCROLL_STEP = ScrollState.STEP

    def _offset(self, scroll_id):
        return self._scroll.offset(scroll_id)

    def _on_scroll(self, scroll_id, offset, maximum, then=None):
        self._scroll.on_scroll(scroll_id, offset, maximum, then)

    def _reset_scroll(self):
        self._scroll.reset()

    def _park_scroll(self):
        """Stash the current screen's scroll offsets on its route dict, so
        coming back to it lands where it was left. No-op with no route (the
        first navigate of the session).

        **Refuses to park while the browser is not on screen**, and that is not
        an optimisation: a yielded scene holds no containers, so the live read
        answers None and ``park`` falls through to a fallback that holds only
        watched containers -- writing that *partial* snapshot over the complete
        one ``_yield`` saved on the way into playback. Reachable via a remote's
        GoHome, and on every mpv < 0.36. See docs/browser-shell.md section 7.

        ``_park_on_leaving_browse`` is the one caller that runs at the boundary,
        which is why it is invoked before ``_yield`` clears the flag.
        """
        route = self.route
        if route is not None and self._browsing:
            self._scroll.park(route, self.app)

    # ------------------------------------------------------------- pages

    def _art_context(self):
        """Render resources a page may use. A namespace rather than the
        browser, so a page cannot reach past it into the shell's state.

        ``tiles`` and ``scroll`` are the two services step 6b extracted, and
        they are here rather than as their own ``PageContext`` fields because
        both answer questions about *what the renderer is drawing* — tile
        geometry and where a container is scrolled to. Reaching them through
        ``art`` is also what keeps the context at nine fields, which the
        contract test caps.
        """
        from types import SimpleNamespace

        return SimpleNamespace(
            strips=self.strips, thumbs=self.thumbs,
            geom=self.geom, geom_wide=self.geom_wide,
            geom_square=self.geom_square,
            geom_banner=self.geom_banner,
            tiles=self.tiles, scroll=self._scroll, pages=self._pages,
            # A node's geometry from the LAST PUSHED scene (mpvtk GUIDE §2),
            # for content that has to be *rasterized* at the size layout
            # gives it rather than merely placed there: an Image cannot
            # flex, so the reader measures its hole on one frame and fills
            # it on the next. Here for the same reason `tiles` and `scroll`
            # are — it answers a question about what the renderer drew — and
            # it is not a field of its own because the contract test caps
            # those, correctly.
            # How the renderer should drive a picture mpv is displaying:
            # the clamp and the pan unit for a comic page. Here rather
            # than as a PageContext field for the same reason node_rect
            # is -- it is a fact about what the renderer is drawing, and
            # the context is capped.
            set_picture_pan=(lambda cfg=None:
                             self._set_picture_pan(cfg)),
            node_rect=(lambda node_id: self.app.node_rect(node_id)
                       if self.app is not None else None))

    def _page_context(self):
        """Build the dependency bundle handed to every page.

        Rebuilt per call rather than cached: `source` and `server` are swapped
        by set_source at arbitrary moments (a reconnect, a user switch), and a
        page holding a stale source would browse a server the user has left.
        """
        return PageContext(
            source=self.source,
            server=self.server,
            # A facade, NOT the raw Navigator. navigate() here is the headless
            # choke point -- it also resets scroll, bumps the epoch, loads and
            # repaints -- and Navigator.push does none of that. Step 6a wired
            # the Navigator itself by mistake, which made every page's
            # navigate() an AttributeError waiting for a click;
            # tests/test_late_bound_calls.py now resolves these statically.
            nav=SimpleNamespace(
                navigate=lambda route, **kw: self.navigate(route, **kw),
                go_back=lambda: self.go_back(),
                reload=lambda route: self._reload_route(route),
                # Re-run a loader WITHOUT bumping the epoch or repainting:
                # for an error path putting back what the server really has,
                # where a bump would cancel unrelated in-flight work.
                load=lambda route: self._load_route(route),
                # Is this route still the screen? An error path that lands
                # after the user navigated away must not repaint or reload
                # over whatever they are looking at now.
                is_current=lambda route: route is self.route,
                after_playlist_deleted=lambda pid:
                    self.after_playlist_deleted(pid)),
            run=self._async,
            art=self._art_context(),
            player=self.controller,
            actions=self._actions,
            dialogs=self._dialogs,
            status=self.set_status,
            invalidate=self.invalidate,
            # Shrinking escape hatch -- see pages/base.py. Counted by
            # tests/test_page_contract.py; it can only go down.
            shell=self,
        )

    #: Route-dict key holding the cached Page instance.
    #:
    #: NOT "_page". That key has meant "which page NUMBER of a paginated
    #: grid" since long before the Page framework existed, and step 6a
    #: claimed it for the object -- so ticking Paginated on a library made
    #: Paginator.ensure compare an int against a GridPage and raise. The
    #: renderer keeps the previous frame when build() throws, so the symptom
    #: was the whole browser silently freezing, with no error anywhere.
    PAGE_OBJ_KEY = "_page_obj"

    def _release_page_grabs(self):
        """Give back everything a page took hold of outside its own scene.

        Called when the browser hands the window over — to playback, or to
        being minimized. ``build()`` returns before ``_retire_page`` once
        ``_browsing`` is False, so a page that yields is never retired and
        none of this would be dropped otherwise.

        All three outlive the page in a way the user feels. A key claim
        keeps SPACE bound to the reader, so pause is dead and instead turns
        the page and writes the new position to the server. A pan model
        makes a wheel notch pan the *playing video* inside the comic's
        clamp. And the picture's zoom and pan are global mpv options, so
        the film plays at whatever the last comic page was set to.
        """
        claim = getattr(self.app, "claim_keys", None)
        if claim is not None:
            try:
                claim(())
            except Exception:
                log.debug("could not drop the key claim", exc_info=True)
        self._set_picture_pan(None)
        # The window is being handed over, so whatever picture was in it is
        # not on screen any more. The page checks this on its way back in;
        # without it the bars repaint over an empty window, because the
        # route was never retired and so never re-opened.
        route = self.route
        if route.get("_showing"):
            route["_showing"] = False
        if self.controller is not None:
            try:
                self.controller.reset_picture_view()
            except Exception:
                log.debug("could not reset the picture view", exc_info=True)

    def _retire_page(self, route):
        """Tell the page we just stopped drawing that it is off screen.

        On the loop thread, from build(), rather than in navigate(): that
        is reachable from mpv's event thread and from the websocket (a
        remote's GoHome, a phone's DisplayContent), and what a page does
        here may touch the player. Observed rather than pushed, the same
        way _shed_caches_on_screen_change is, and for the same reason.

        The comparison is by page *object*, so a route re-entered later
        gets its close() and its next load() in the right order.
        """
        page = self._page_for(route)
        previous = getattr(self, "_live_page", None)
        if previous is page:
            return
        self._live_page = page
        if getattr(page, "kind", None) != "comic":
            # Dropped by whoever is NOT the picture page, the same way a
            # key claim is: a gesture model that outlives its page pans a
            # picture nobody can see.
            self._set_picture_pan(None)
        if previous is None:
            return
        try:
            previous.close()
        except Exception:
            log.warning("page close failed", exc_info=True)

    def _claim_page_keys(self, route):
        """Push this route's key claim to the renderer (see
        ``MpvtkApp.claim_keys``).

        Driven by a ``claimed_keys`` attribute on the Page rather than by
        the shell knowing which kinds want keys — and read every frame, so
        a claim cannot outlive the page that made it. Almost every page
        claims nothing, and ``claim_keys`` is a no-op when the set has not
        changed.
        """
        claim = getattr(self.app, "claim_keys", None)
        if claim is None:
            return
        page = self._page_for(route)
        try:
            claim(getattr(page, "claimed_keys", ()) or ())
        except Exception:
            log.debug("key claim failed", exc_info=True)

    def _set_picture_pan(self, config=None):
        """Push (or drop) the renderer's gesture model for a picture."""
        setter = getattr(self.app, "set_picture_pan", None)
        if setter is None:
            return
        try:
            setter(config)
        except Exception:
            log.debug("could not set the pan model", exc_info=True)

    def _on_picture_gesture(self, kind, evt):
        """A wheel or drag gesture over a displayed picture. Loop thread.

        Node-less, so it cannot go through the handler registry: the
        picture is mpv's video, not something in the scene. Handed to
        whichever page put it there, which is the only thing that knows
        what "past the bottom" means.
        """
        page = self._page_for(self.route)
        handler = getattr(page, "on_picture_gesture", None)
        if handler is None:
            return
        try:
            handler(kind, evt)
        except Exception:
            log.warning("page picture gesture failed", exc_info=True)

    def _on_claimed_key(self, key):
        """A key this route claimed. Handed to the page, on the loop thread."""
        page = self._page_for(self.route)
        handler = getattr(page, "on_key", None)
        if handler is None:
            return
        try:
            handler(key)
        except Exception:
            log.warning("page key handler failed", exc_info=True)

    def _page_for(self, route):
        """The Page serving ``route``, or None if its kind is still a mixin.

        Cached on the route dict so load() and render() share one instance and
        a page can keep state on itself rather than in the route.
        """
        cls = PAGES.get(route.get("kind"))
        if cls is None:
            return None
        page = route.get(self.PAGE_OBJ_KEY)
        if page is None or type(page) is not cls:
            page = cls(self._page_context(), route)
            route[self.PAGE_OBJ_KEY] = page
        else:
            # Refresh the context: see _page_context on why it is not cached.
            page.ctx = self._page_context()
        return page

    def _load_route(self, route, epoch=None):
        """Dispatch to the route kind's loader, if it has one.

        Kinds are declared in each mixin's ROUTES table alongside their
        renderer, so adding a view is one edit in one place.

        The epoch is re-read here rather than threaded down from the
        ``_bump_epoch()`` every caller performs immediately above. **That is
        deliberate and it is not the race it looks like**: re-reading yields the
        newest epoch, so a loader can never capture one already superseded,
        whereas threading the value down lets an interloping bump strand the
        view spinning with no retry. ``TestNavigationSurvivesAConcurrentBump``
        pins it; the reasoning is in docs/browser-shell.md section 2.

        ``epoch`` therefore exists only for callers that genuinely have their
        own (none today). Leave it None.
        """
        if self.server is None:
            return
        route.pop("_error", None)
        # ep is read here, on the loop thread, and handed down: a loader
        # that read it later would be racing the navigation it guards.
        ep = self._epoch if epoch is None else epoch
        page = self._page_for(route)
        if page is not None:
            page.load(ep)
            return
        loader = (self._routes().get(route["kind"]) or (None, None))[0]
        if loader is not None:
            getattr(self, loader)(route, ep)

    def _edit_call(self, fn, on_ok=None, on_error=None, error=None):
        """A mutating edit whose failure the user must see.

        _client_call swallows: an "Add to playlist" the server rejected
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
        elif t == AUDIOBOOK_TYPE:
            # A page, NOT immediate playback -- unlike the Audio above it.
            # An AudioBook plays like a track, but it is a *book*: it has a
            # description, a length, chapters and a place you got to, and a
            # tile that started it left every one of those unreachable.
            # A song has none of them, which is why the split is here and
            # not one line up. The hover play chip still starts it, exactly
            # as it does for a film.
            self.navigate(dict(base, kind="audiobook"))
        elif t == BOOK_TYPE:
            self.navigate(dict(base, kind="book"))
        elif t == "Studio":
            # A studio spans films and shows, so it opens as a row per kind
            # rather than one grid of everything sorted by name.
            self.navigate(dict(base, kind="byname",
                               list={"type": "items",
                                     "studio_ids": item.get("Id")}))
        elif t == "Genre":
            # A video genre, from a search result or a detail page's Genres
            # line. It spans films, shows and albums, so it opens as a row
            # per kind -- jellyfin-web's ItemsByName. The Genres *screen*
            # links its headings straight to a single-type listing instead,
            # because there the type is already known.
            self.navigate(dict(base, kind="byname",
                               list={"type": "items",
                                     "genre_ids": item.get("Id")}))
        elif t == PHOTO_TYPE:
            # Straight to the picture, like Audio and unlike everything in
            # PLAYABLE_TYPES -- a detail page for a photo would be a
            # heading, a date and no reason to be there.
            #
            # The rest of the album rides along as the queue, starting at
            # this one, so next/prev walk the folder and unpausing plays it
            # through at mpv's --image-display-duration. That is the whole
            # slideshow, and it costs one already-loaded list.
            self._play_photo(item, server)
        elif item.get("CollectionType") == LIVE_TV_COLLECTION:
            # The Live TV view is a destination, not a folder: its children
            # are channels, and browsing them as a grid loses the guide, the
            # recordings and the schedule. Checked before FOLDER_TYPES, which
            # a UserView would otherwise match.
            self.navigate(dict(base, kind="livetv", parent_id=item.get("Id"),
                               title=item.get("Name") or _("Live TV")))
        elif t == "Program":
            # A program page, not immediate playback: it is where Record
            # lives, and it is one click from there to Watch. (Playing the
            # channel outright is still what a TvChannel tile does.)
            self.navigate(dict(base, kind="program",
                               channel_id=item.get("ChannelId"), _seed=item))
        elif t in LIVE_TYPES:
            # A channel page, not immediate playback — the same split
            # jellyfin-web makes (its channel card is data-action="link",
            # and a TvChannel routes to the item details page, whose whole
            # content is that channel's upcoming programmes). Tuning in used
            # to be the ONLY thing a channel tile could do, which left no way
            # to see what was on later without going back to the guide. Watch
            # is the first button on the page, and the tile's context menu
            # still tunes straight in.
            #
            # ChannelId first: a channel's own id is in Id, but LIVE_TYPES
            # also holds Program, and for one of those the channel is what
            # this page would be about.
            self.navigate(dict(base, kind="channel",
                               item_id=item.get("ChannelId") or item.get("Id"),
                               _seed=item))
        elif item.get("CollectionType") == "music":
            self.navigate(dict(base, kind="music", parent_id=item.get("Id")))
        elif t in SERIES_TYPES:
            self.navigate(dict(base, kind="series"))
        elif t == "Season":
            # bar_title: the top bar says which *show* this is. Without it
            # every part of the screen says "Season 1" and none of them
            # says what it is a season of. Absent SeriesName it stays
            # unset and the bar falls back to the season name.
            self.navigate(dict(base, kind="season",
                               series_id=item.get("SeriesId"),
                               bar_title=item.get("SeriesName") or None))
        elif t in PLAYABLE_TYPES:
            self.navigate(dict(base, kind="detail"))
        elif t in ("Person", "Actor", "Director", "Writer"):
            self.navigate(dict(base, kind="person", person_id=item.get("Id")))
        elif t in FOLDER_TYPES or item.get("CollectionType"):
            # collection_type rides along so the grid knows whether to offer
            # the Collections toggle (movies libraries only).
            # parent_type as well as collection_type: inside a BoxSet the
            # tile menu can offer "Remove from Collection".
            #
            # Books are the one library whose collection type is INHERITED
            # down the tree. A folder's own DTO does not say which library it
            # is in, and for books that answer changes the screen: a folder
            # of audiobook chapters is drawn as an album, not a grid. So it
            # is carried on the route. Only for books -- propagating any
            # other type would make a folder inside a movies library run the
            # library's typed, recursive query and list the whole library
            # again (LIBRARY_ITEM_TYPES); "books" has no entry there, so
            # carrying it changes nothing about the request.
            ctype = item.get("CollectionType")
            if ctype is None and self.route.get(
                    "collection_type") == BOOKS_COLLECTION:
                ctype = BOOKS_COLLECTION
            self.navigate(dict(base,
                               kind=("books" if ctype == BOOKS_COLLECTION
                                     else "grid"),
                               parent_id=item.get("Id"),
                               parent_type=t,
                               collection_type=ctype))
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
        self.hud.reset()
        if app is None:
            return
        if hasattr(app, "on_nav"):
            app.on_nav = self._on_nav_mode
        if hasattr(app, "on_hud"):
            app.on_hud = self.hud.on_hud
        if hasattr(app, "on_hud_skip"):
            app.on_hud_skip = self.hud.on_skip
        if hasattr(app, "on_pause"):
            # The renderer's own pause paths (click-to-pause, the summon
            # key, right-click in mpv modality) hand over while a SyncPlay
            # group is on, because a local `cycle pause` is not a pause
            # there -- it is a desync the group then corrects.
            app.on_pause = lambda: self._ctl(lambda c: c.toggle_pause())
        if hasattr(app, "on_clipboard_error"):
            app.on_clipboard_error = self._on_clipboard_error
        if hasattr(app, "on_forward"):
            app.on_forward = self._on_mouse_forward
        if hasattr(app, "on_key"):
            app.on_key = self._on_claimed_key
        if hasattr(app, "on_gamepad_seek"):
            # The right stick. `kb_seek` and not a distance of ours: it
            # reads the amount off the user's own input.conf arrow binding,
            # applies use_web_seek, and seeks in a way a SyncPlay group
            # hears about.
            app.on_gamepad_seek = lambda d: self._ctl(
                lambda c: c.kb_seek(d))
        if hasattr(app, "on_gamepad_nav"):
            # A pad button whose meaning differs between the library and a
            # playing video, handed to the remote control's own ladder
            # rather than to a second copy of it.
            app.on_gamepad_nav = lambda a: self._ctl(
                lambda c: c.remote_action(a))
        app.on_picture_gesture = self._on_picture_gesture
        if hasattr(app, "on_scene_pushed"):
            # The strip cache's clock. Not build(): see StripStore.
            app.on_scene_pushed = self._on_scene_pushed

    def reassert_window_state(self):
        """Re-assert window ownership on a FRESH renderer (which starts
        active): browse takes the window back; a video in flight
        re-enters attached-but-idle HUD mode; otherwise get fully out
        of the way (lua OSC / minimized)."""
        if self._browsing:
            self._set_renderer_active(True)
        elif self.hud.available() and self.hud.state is not None:
            try:
                self.hud.engage()
            except Exception:
                log.debug("set_hud failed", exc_info=True)
        else:
            self._set_renderer_active(False)

    def _tell_controller(self, name):
        """Fire one of the browse/playback transition callbacks, and survive
        it raising.

        These three run in the MIDDLE of a transition -- ``_yield`` has
        already cleared ``_browsing`` and still has to engage the HUD;
        ``enter_browse`` still has to refresh Home and re-activate the
        renderer. An exception out of the controller therefore did not fail
        the callback, it abandoned the transition half-applied, and the
        browser stayed in a state nothing would put right until the next one.

        Found the hard way: gateway.on_browse_leave read a setting #615 had
        retired, so every single browse -> video transition raised
        AttributeError and skipped the HUD engage that follows it.
        """
        if self.controller is None:
            return
        fn = getattr(self.controller, name, None)
        if fn is None:
            return
        try:
            fn()
        except Exception:
            log.exception("controller.%s failed", name)

    def _park_on_leaving_browse(self):
        """Park the scroll offsets on the way out of the browser.

        Yielding to playback pushes an EMPTY scene, and the renderer drops
        the state of every container that left the scene. Unlike a
        navigation, coming back is a rebuild of the *same* route — nothing
        pushes or pops — so ``navigate``'s park never runs and there was
        nothing left for ``Page.parked_scroll`` to restore. Press play on
        something near the end of a library and the library came back at the
        top; whether it came back *blank* on the way depended on whether the
        live read still had the old offsets when the window was built.

        The ``_browsing`` guard this needs lives in ``_park_scroll`` now, so
        every caller gets it: a video start parks here and then reaches
        ``_yield`` once playback reports in, by which time the containers
        have left the scene and a park would write a partial snapshot over
        this one.
        """
        self._park_scroll()

    def _yield(self):
        self._park_on_leaving_browse()
        self._browsing = False
        self._release_page_grabs()
        self._tell_controller("on_browse_leave")
        if self.hud.available():
            # keep the renderer attached: blank scene + summon bindings
            try:
                self.hud.engage()
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
        self.load.error = None
        if audio:
            self._now_playing = self._now_playing or {"title": _("Loading…")}
            self.invalidate()
        else:
            # Deliberately NOT _yield() yet. HUD mode is attached-but-idle
            # "with a blank scene" (mpvtk/app.py set_hud), and the non-HUD
            # branch detaches the renderer outright — so yielding here threw
            # our own scene away, which is why the load showed nothing at all
            # and the UI appeared to flash. Keep the window, draw the spinner,
            # and hand off in load.clear() once playback reports in.
            #
            # _browsing first, then begin(): begin() arms the spinner timer,
            # and the original armed it last. Keeping that order means the
            # timer can never observe a half-applied start.
            #
            # Parked here rather than only in _yield: the loading screen
            # replaces the content as soon as the spinner is due, so the
            # scroll container can leave the scene well before playback
            # reports in and the yield happens.
            self._park_on_leaving_browse()
            self._browsing = False
            self.load.begin(title)
            self.invalidate()

    # ------------------------------------------------------ load feedback

    SPINNER_DELAY = LoadFeedback.SPINNER_DELAY   # see load_feedback.py

    def _arm_spinner(self):
        """Repaint once the grace period is up — nothing else would.

        The load holds no ticker: if it finishes first, load.clear() repaints
        and this wakes to find nothing to do. The waiting policy (why a loop
        rather than a flat sleep) is LoadFeedback.due_in; the thread slot and
        the shutdown event are the shell's, which is why this stayed here.
        """
        def show():
            while not self._shutdown_evt.is_set():
                due_in = self.load.due_in()
                if due_in is None:
                    break               # resolved while we waited
                if due_in <= 0:
                    break
                self._shutdown_evt.wait(due_in)
            with self._poller_lock:
                self._spinner_timer = None
            self.invalidate()

        self._start_daemon("_spinner_timer", "mpvtk-spinner", show)

    def open_hud_menu(self):
        """kb_menu during playback. Forwarded: playerManager holds a
        reference to this bound method (see ui.py)."""
        return self.hud.open_menu()

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
        self._actions.play(item, server, offset_ticks=offset_ticks,
                           srcid=srcid, aid=aid, sid=sid)

    def _play_list(self, ids, server, start_index=0, audio=False, items=None):
        self._actions.play_list(ids, server, start_index, audio, items)

    def _play_photo(self, item, server):
        """Open a photo, with the rest of its album queued behind it.

        The album is whatever the grid this was clicked in has LOADED -- no
        fetch, and it matches what the user can see. Since #617 that is a
        window rather than everything walked past, so a photo opened deep in
        a large album queues its neighbourhood rather than the whole folder;
        the holes are skipped like any other consumer skips them. Falls back
        to the one photo when the route has no list (a search result, say),
        which still opens the picture.
        """
        items = [i for i in (self.route.get("_items") or [])
                 if i and i.get("Type") == PHOTO_TYPE and i.get("Id")]
        ids = [i["Id"] for i in items]
        try:
            start = ids.index(item.get("Id"))
        except ValueError:
            ids, items, start = [item.get("Id")], [item], 0
        self._play_list(ids, server, start, items=items)

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
        if self.headless and self.route.get("kind") not in self.HEADLESS_ROUTES:
            # Playback ended and something is putting us back on a library
            # page. In headless the only page to come back to is the cast
            # screen.
            self.show_cast()
        self._browsing = True
        self._minimized = False
        self.hud.shown = False
        self._tell_controller("on_browse_enter")
        # Whatever just finished may have moved the resume rows, and this is
        # the moment they are about to be looked at. go_back has re-read Home
        # for this reason all along; coming back from PLAYBACK does not go
        # through it, which is why watching something in the shim itself left
        # its own Continue Watching row stale (#560).
        self.refresh_home(now=True)
        self._set_renderer_active(True)
        self.invalidate()

    def minimize(self):
        """Release the window entirely — the app keeps running in the tray as
        a cast target. This is the player's "playback_abort yes, force_window
        no" state; there is no separate window to hide, so minimizing *is*
        dropping force_window with nothing playing."""
        self._park_on_leaving_browse()
        self._minimized = True
        self._browsing = False
        self._release_page_grabs()
        self.hud.shown = False
        self._set_renderer_active(False)
        self._tell_controller("on_minimize")

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
            self.load.clear()
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
            self.hud.state = None
            self.hud.shown = False
            self.hud.menu = None
            if self.load.error is not None:
                # A failed start owns the window and is explaining why. stop()
                # is part of that failure path, so returning to browse here
                # would bounce the user back to the library over the error
                # they have not read yet.
                self.invalidate()
                return
            if self.load.starting is not None:
                # ...and a start still IN FLIGHT owns it for the same reason:
                # the loading screen is on the window and the yield to video
                # has not happened yet. The player suppresses the incidental
                # stopped pushes a load produces (player_reporting), so this
                # is the second lock -- a remote stop, a websocket, anything
                # that reports one from outside that path. Cancelling and
                # failing both clear `starting` before they arrive here, so
                # nothing can be stranded on the loading screen by it.
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
            self.hud.state = state   # feeds the playback HUD bar
            if self._browsing:
                self._yield()         # video: yield the window + the OSC
            else:
                self.invalidate()     # HUD/bar repaint (clock, pause icon)
            if not self._browsing and self.hud.available():
                try:
                    # Idempotent HUD-mode engage: covers playback that
                    # starts while minimized/already-yielded and a fresh
                    # renderer after mpv re-creation (a plain _yield only
                    # happens on the browsing -> video transition).
                    self.hud.engage()
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

        **The check and the assignment have to be atomic.** Callers are
        reachable from the loop thread *and* from foreign ones
        (``on_playstate``, ``on_downloads_changed``), so two could both see None
        and both start a thread -- and a doubled poller is only a wasted refresh
        today, which is exactly why it would go unnoticed.

        ``attr`` is cleared when the thread exits. Returns True if this call
        started the thread, False if one was already running -- callers driven
        by a *user action* should say so rather than appear to do nothing.

        ``restartable=True`` makes a departing thread ``invalidate()`` once it
        has released the slot, closing the window in which a poller exits just
        after a re-entering view was refused one and leaves the panel frozen.
        Opt-in, because ``_arm_toast_clear`` releases its slot early by design.
        See docs/browser-shell.md section 8.
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
                if not bar and not self.hud.shown:
                    break
                try:
                    self.controller.refresh_playstate()
                except Exception:
                    log.debug("playstate refresh failed", exc_info=True)

        self._start_daemon("_np_thread", "mpvtk-np-tick", tick)

    def _publish_auth_origins(self):
        """Tell the thumbnail store which token belongs to which server.

        Images are fetched on the store's own session, so this is how they
        authenticate by header rather than by query string. Called on every
        source swap, which is also what revokes a signed-out server's token
        -- the store replaces its map wholesale.
        """
        origins = {}
        get = getattr(self.source, "auth_origins", None)
        if get is not None:
            try:
                origins = get()
            except Exception:
                log.debug("could not publish auth origins", exc_info=True)
        try:
            self.thumbs.set_auth(origins)
        except Exception:
            log.debug("thumbnail store would not take auth origins",
                      exc_info=True)

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
        self._publish_auth_origins()
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
            self._nav.reset_to_default()
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
                self.tiles.set_downloaded(*self.controller.downloaded_ids())
            except Exception:
                # Guarded, NOT returned from. This used to bail out of the
                # whole function, so a badge read that failed silently
                # skipped everything below it -- the pending book opens and
                # the page's own state re-read, neither of which has
                # anything to do with the badge sets. Three independent
                # consumers of one notification; one failing must not take
                # the others with it.
                log.debug("downloaded-id refresh failed", exc_info=True)
            # A book download is the one whose *completion* something is
            # waiting on: Read starts the fetch and opens the file when it
            # lands (ItemActions.read_book). This is where the catalog says
            # it landed, so this is where that wait is resolved -- rather
            # than in a poller per press, which would outlive the press.
            try:
                self._actions.flush_pending_reads()
            except Exception:
                log.debug("pending book reads failed", exc_info=True)
            # And the screen showing that download has to be told. The
            # badge sets above cover every TILE, but a page that resolved
            # something richer than "is it downloaded" at load time -- the
            # book page, which distinguishes queued from in-flight from on
            # disk -- has to re-read it. Asked for generically so the shell
            # does not have to know which kinds care.
            try:
                page = self._page_for(self.route)
                refresh = getattr(page, "refresh_download_state", None)
                if refresh is not None:
                    refresh()
            except Exception:
                log.debug("page download-state refresh failed", exc_info=True)
            self.invalidate()
        self._pool.submit(work)

    # --------------------------------------------------------------- build

    def build(self, size):
        w, h = size
        self._size = size
        # One synchronous read per frame: the renderer's live scroll offsets,
        # which virtualization windows against (see _offset). The route goes
        # with it for the offsets parked on it — on the frame a screen comes
        # back, those are what its containers are about to be restored to,
        # and so what its window has to be built around.
        self._scroll.refresh(self.app, self.route)
        # Load feedback outranks everything, including the yield to video:
        # that yield is exactly what left a blank window during a load, and a
        # failed start has no video to show through.
        if self.load.error is not None:
            return self.load.error_scene(size)
        if (self.load.starting is not None and not self._browsing
                and self.load.spinner_due()):
            return self.load.loading_scene(size)
        if not self._browsing:
            if self.hud.shown:
                # Summoned playback HUD over the video (see hud.py; the
                # renderer owns the summon/auto-hide lifecycle).
                return build_hud(self, size)
            # Yielded to playback: an empty scene clears our overlays so the
            # video + OSC show through.
            return Column([], w=w, h=h)
        # Deliver any decoded posters before composing strips this frame.
        if self.thumbs is not None:
            self.thumbs.pump()
        # Outside that guard: an owner with strips but no thumbs (a test
        # double, an embedder) still has composited rows to shed.
        self._shed_caches_on_screen_change()
        route = self.route
        if route["kind"] in LIVE_KINDS:
            self._poll_live_tv(route)
        content = self._render_route(route, size)
        # After render, so a page can decide what it claims from what it
        # drew, and unconditional so that leaving the page drops the claim.
        self._claim_page_keys(route)
        self._retire_page(route)
        children = []
        if route["kind"] not in CHROME_FREE:
            children.append(window_chrome.chrome(self, w))
            banner = window_chrome.banner(self)
            if banner is not None:
                children.append(banner)
            dlbar = window_chrome.download_bar(self)
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
            # One slot, two kinds of context menu: a tile's actions, or the
            # Back button's history. Only one can be open — they are opened
            # by the same gesture on different targets — so they share the
            # state, the ESC handling and the "defer a Live TV refresh
            # while a menu is up" rule.
            menu = (window_chrome.history_menu_node(self)
                    if self._menu.get("kind") == "history"
                    else self._tile_menu_node())
            if menu is not None:
                children.append(menu)
        if self._dialog is not None:
            children.append(self._dialog())
        toast = window_chrome.toast_node(self, w, h)
        if toast is not None:
            children.append(toast)
        # Last: the window's own corner has to be over the page's, and over
        # the now-playing bar that ends at the same pixel.
        grip = window_chrome.resize_grip(self, w, h)
        if grip is not None:
            children.append(grip)
        page = Column(children, w=w, h=h, align="stretch")
        # A page background, when something needs one. mpv's own
        # background-color is a flat colour and stays as the base -- it is
        # what shows before the first scene lands -- so this paints over it
        # rather than replacing it. Bottom of the Stack, so every bit of
        # chrome still draws on top.
        stops = theme.window_gradient()
        if stops:
            back = Gradient(stops=stops, axis="y", w=w, h=h)
        elif self._wants_opaque_backdrop():
            back = Box(bg=theme.WINDOW_BG, w=w, h=h)
        else:
            return page
        return Stack([back, page], w=w, h=h)

    def _wants_opaque_backdrop(self):
        """Whether the library has to paint its own window background.

        Normally it does not: mpv's ``background-color`` is the window, and
        one flat fill costs nothing to composite. Under "Custom OSC" that is
        not enough. The controls are then a script we never see, it draws an
        idle screen while the browse window sits there with nothing loaded,
        and that screen is OSD *over* the VO background -- so no colour we
        give mpv can hide it. Ours is a bitmap, which composites above OSD,
        and is the only thing that can. (Asking the script to stop drawing
        works too and we do ask, but only a fork that honours
        ``osc-idlescreen`` hears it, and one that has already drawn may have
        no path left that wipes it.) docs/mpv-backends.md section 12.

        Through `resolve_osc_style`, never the raw setting, because that can
        hold a legacy alias. Not through the player, which would cost this
        module an import of `player` -- and importing that builds its
        singleton and opens an mpv window.
        """
        from ..mpv_options import resolve_osc_style
        try:
            return resolve_osc_style() == "custom"
        except Exception:
            return False

    # How long a status message stays on screen.
    TOAST_SECS = 6.0

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

    def clear_status_if(self, text):
        """Take the toast down early, but only if it is still ours.

        For a message that *reports something in progress*: TOAST_SECS is
        six, and on a local server "Downloading X…" outlives the download
        it is about and sits over the first page of the book (#2).

        Conditional on the text, because six seconds is long enough for
        something else to have replaced it -- an error, a finished
        download, a queued item -- and a reader clearing that would be
        worse than the stale toast it was fixing.
        """
        # `text and` only avoids a needless repaint when both are empty;
        # what makes this safe is the equality, not the guard.
        if text and self.status == text:
            self.set_status("")

    # Minimum room the page title keeps in the top bar before the
    # buttons drop their labels (~a "Continue Watching" at 22px bold).
    TITLE_MIN_W = 260

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
        page = self._page_for(route)
        renderer = (self._routes().get(route["kind"]) or (None, None))[1]
        if page is None and renderer is None:
            return self._busy()
        # A load that failed with nothing to show says so and offers a
        # retry. Without this the route's data stayed None and the view
        # spun forever, so an unreachable server read as a hang.
        if (route.get("_error")
                and route.get("_data") is None
                and not route.get("_items")):
            return self._error_retry(route)
        if page is not None:
            return page.render(size)
        return getattr(self, renderer)(route, size)

    def _error_retry(self, route):
        return Box([
            Spacer(),
            Row([Spacer(),
                 Text(route["_error"], size="large", color=theme.SUBTLE_FG),
                 Spacer()]),
            Row([Spacer(),
                 Button(_("Retry"), id="route-retry", icon="refresh",
                        on_click=lambda: self._retry_route(route)),
                 Spacer()]),
            Spacer(),
        ], flex=1, direction="column", align="stretch", gap=14)


    def notify_update(self, version, url):
        """Registered as playerManager.notify_update: show the update notice
        as a browser banner (mirrors the Tk browser / CLI-OSD split)."""
        self._update = {"version": version, "url": url}
        self.invalidate()

    def resend_hud_config(self):
        """Re-send the renderer's HUD token. Called when SyncPlay is joined
        or left, because the token carries whether the renderer's own pause
        paths hand over to Python -- a group joined mid-playback would
        otherwise keep the local `cycle pause`, which in a group is a desync
        rather than a pause.

        Called directly from whichever thread SyncPlay is on, as
        ``notify_syncplay`` beside it is: ``engage`` ends in an mpv command,
        which is thread-safe, and deferring it would let a group be joined
        and a click land before the renderer had been told.

        **Guarded exactly as every other engage() call site is.** engage()
        is not a re-send, it is set_hud(True), and the renderer treats that
        as a MODE CHANGE when the HUD is not already up: ui_suspend() drops
        the nav keys, the mouse and the wheel bindings. Ungated, leaving a
        group from the library -- which is what pressing stop in a group
        does -- froze the library with no way back. And for a user on a lua
        OSC it switched the mpvtk summon layer on over their OSC.
        Un-engaged, the flag does not matter: none of the renderer's pause
        paths are live, and the next engage() sends the current value."""
        try:
            if (self.hud is not None and not self._browsing
                    and self.hud.available() and self.hud.state is not None):
                self.hud.engage()
        except Exception:
            log.debug("could not re-send the HUD config", exc_info=True)

    def notify_syncplay(self, message):
        """Registered as playerManager.notify_syncplay: SyncPlay's messages
        on the status line rather than the MPV OSD.

        Dropped while video owns the window: the browser is yielded, so there
        is nothing on screen to draw a status line on, and a toast queued now
        would appear minutes later when the library came back. The log has
        them either way."""
        if not self._browsing:
            return
        self.set_status(message)

    def syncplay_menu_reachable(self):
        """Registered as playerManager.syncplay_menu_reachable: whether
        stopping playback lands somewhere the SyncPlay menu can be opened,
        which is what decides between halting the group and leaving it.

        Headless has one page and it is chrome-free; ``_minimized`` is the
        cast that never opened the library, and stopping it puts the window
        away rather than showing the browser. In both the SyncPlay button in
        the top bar is unreachable, so a halted group would be one nobody
        could get out of."""
        return not self.headless and not self._minimized

    # -- client-side decorations -------------------------------------------

    @property
    def window_controls(self):
        """Whether the top bar is standing in for a title bar this frame."""
        return self._csd

    @property
    def maximized(self):
        """Live only while ``window_controls`` is on — it is read to pick the
        maximize button's glyph and nothing else consults it."""
        return self._maximized

    def refresh_window_controls(self):
        """Re-take the window-chrome snapshot.

        Called at startup and from ``playerManager.on_decorations_changed``.
        It has to be a push, not a poll: on Wayland the answer arrives in a
        compositor configure event, so a session's first frames can be drawn
        before mpv knows, and fullscreen/maximize change it again later.
        """
        ask = getattr(self.controller, "window_chrome_state", None)
        if ask is None:
            return          # no player behind us (tests, offline stand-ins)
        try:
            state = ask() or {}
        except Exception:
            log.debug("could not ask about window decorations", exc_info=True)
            return
        wanted = bool(state.get("controls"))
        maximized = bool(state.get("maximized"))
        if (wanted, maximized) != (self._csd, self._maximized):
            self._csd = wanted
            self._maximized = maximized
            self.invalidate()

    def close_window(self):
        """The title bar's close button. Deliberately the same path as mpv's
        own close (CLOSE_WIN), so ``close_to_tray`` and the no-tray safeguard
        decide what closing means here exactly as they do there -- a second
        close button with its own idea of "close" is how the two drift."""
        self._tell_controller("close_window")

    def minimize_window(self):
        """Iconify. Not ``minimize()``, which is this app's own tray-minimize
        (it tears the window down and keeps running headless) -- the title
        bar's button has to mean what it means in every other window."""
        self._tell_controller("minimize_window")

    def toggle_maximized(self):
        self._tell_controller("toggle_window_maximized")

    @property
    def offline(self):
        """Whether the browser is on the offline source. Read-only;
        ``set_offline`` is the single writer, and services read it live
        through here rather than reaching for ``_offline``."""
        return self._offline

    def set_offline(self, offline):
        offline = bool(offline)
        if offline != self._offline:
            self._offline = offline
            self.invalidate()

    # -- download status bar ----------------------------------------------

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

    def note_restart_needed(self, key):
        """Remember that ``key`` will not do anything until a restart."""
        self._restart_keys.add(key)
        self.invalidate()

    def _dismiss_restart(self):
        """Put the banner away without restarting.

        The settings are still saved and still pending -- this only stops
        saying so, which is why the button is "Later" rather than
        "Dismiss". Changing another restart-required setting afterwards
        raises it again, naming that one: the user is being told about a
        new decision, not nagged about the one they answered.
        """
        self._restart_keys = set()
        self.invalidate()

    def can_restart(self):
        """Whether to offer a Restart button rather than only a notice.

        Asked at draw time and answered by the controller, so a stand-in
        without the method (and a machine where the launch cannot be
        reconstructed) both fall back to the notice. False is the safe
        answer: a button that takes the app away and does not bring it back
        is worse than no button.
        """
        # Cached for the life of this browser. The banner is part of the
        # scene, so without it this ran on every repaint -- scrolling,
        # hovering, playback progress -- and the answer costs a stat of
        # `sys.argv[0]`. It cannot change within a session: it asks whether
        # this launch can be reconstructed at all.
        if self._can_restart is not None:
            return self._can_restart
        can = getattr(self.controller, "can_restart", None) if self.controller else None
        if can is None:
            return False
        try:
            self._can_restart = bool(can())
        except Exception:
            log.debug("could not ask whether a restart is possible",
                      exc_info=True)
            self._can_restart = False
        return self._can_restart

    def _restart_now(self):
        """Restart the app. The banner's button.

        The keys are NOT cleared here. If the restart fails to start, the
        settings are still pending and the banner still has something true
        to say -- clearing first would have left the user with a working app,
        an unapplied setting and nothing on screen about either.
        """
        ok = False
        if self.controller is not None:
            restart = getattr(self.controller, "restart_app", None)
            if restart is not None:
                try:
                    # The pending keys go with the request: a command-line
                    # override naming one of them has to be dropped from the
                    # relaunch, or it lands on top of the value the user just
                    # saved and the restart appears to do nothing. Passed
                    # positionally through a getattr'd method, so a gateway
                    # that predates the argument raises TypeError rather than
                    # silently ignoring it -- caught below, reported as a
                    # failed restart, which is the honest answer.
                    ok = bool(restart(set(self._restart_keys)))
                except Exception:
                    log.exception("could not restart")
        if not ok:
            self.set_status(_("Could not restart automatically — quit and "
                              "start the app again to apply your changes."))
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
        """Stop background work, and let the live page go.

        The page that is on screen has never been retired — retirement is
        observed from ``build()``, which stops running — so anything it
        took hold of is still held. For the comic reader that is a
        directory of extracted pages, which nothing else will delete:
        quitting inside a comic left them behind every time.

        ``free_bitmaps=False`` keeps the composited tile buffers alive. On
        libmpv those are read BY ADDRESS by mpv every frame it composites, so
        they may only be released once mpv is genuinely dead — the caller
        knows that, this does not. See mpvtk_browser.ui.stop().
        """
        page, self._live_page = self._live_page, None
        if page is not None:
            try:
                page.close()
            except Exception:
                log.debug("page close on shutdown failed", exc_info=True)
        self._shutdown_evt.set()   # also stops the downloads poller
        self._async.shutdown(wait=False, cancel_futures=True)
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
