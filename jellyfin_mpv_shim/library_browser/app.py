"""The browser window: top-bar chrome, navigation stack, and IPC pumps.

Runs in its own process. Imports of tkinter are deferred to construction time so
this module stays importable from the main process (e.g. for smoke tests).
"""

import logging
import os
import queue
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from ..constants import USER_APP_NAME, APP_NAME
from ..conffile import confdir
from ..utils import get_resource
from ..i18n import _
from .repository import (LibrarySource, OfflineLibrarySource, PLAYABLE_TYPES,
                         SERIES_TYPES, FOLDER_TYPES)
from .thumbnails import ThumbnailStore
from .views import VIEW_TYPES
from .theme import (apply_dark_theme, WINDOW_BG, CARD_BG, PANEL_BG, SUBTLE_FG,
                    TEXT_FG, ACCENT)

log = logging.getLogger("library_browser.app")


class BrowserApp:
    def __init__(self, cmd_queue, r_queue, servers, options):
        import tkinter as tk
        from tkinter import ttk

        self.tk = tk
        self.ttk = ttk
        self.cmd_queue = cmd_queue
        self.r_queue = r_queue
        self.options = options or {}
        self.page_size = self.options.get("page_size", 100)
        self.image_width = self.options.get("image_width", 280)
        self.clean_exit = False
        self._closing = False

        self.root = tk.Tk()
        self.root.title(USER_APP_NAME)
        self.root.geometry("1180x760")
        self.root.minsize(720, 480)
        self.root.configure(bg=WINDOW_BG)
        apply_dark_theme(self.root, ttk)
        # Route mouse-wheel events to whichever scrollable region is under the
        # pointer (works over tiles, not just the scrollbar).
        self.root.bind_all("<MouseWheel>", self._on_wheel)
        self.root.bind_all("<Button-4>", self._on_wheel)
        self.root.bind_all("<Button-5>", self._on_wheel)
        try:
            icon_img = tk.PhotoImage(file=get_resource("logo.png"))
            self.root.iconphoto(True, icon_img)
            self._icon_ref = icon_img
        except Exception:
            pass
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        verify_ssl = self.options.get("verify_ssl", True)
        cache_dir = os.path.join(confdir(APP_NAME), "image_cache")
        # library_image_cache_mb bounds BOTH tiers: the on-disk encoded cache
        # and the in-memory decoded Tk images. It previously only reached the
        # disk tier, leaving RAM at the hardcoded default no matter what the
        # user configured. The RAM tier is capped: someone raising the knob
        # to keep gigabytes of offline artwork on disk shouldn't silently
        # authorize a multi-GB decoded-image cache.
        image_cache_mb = self.options.get("image_cache_mb", 256)
        self.thumbs = ThumbnailStore(
            cache_dir, verify_ssl=verify_ssl,
            max_mem_mb=min(image_cache_mb, 512),
            max_disk_mb=image_cache_mb)
        self._api_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lib-api")
        self._ui_queue = queue.Queue()
        self.nav_stack = []
        self.current_view = None
        self._switcher_servers = []  # index-aligned with the server combobox
        self.home_cache = {}  # server_uuid -> (libraries, rows); stale-while-revalidate
        self.server_list = list(self.options.get("server_list") or [])  # all creds

        # Local users ("fast user switching"). users: [{id,name,locked,default}].
        users_state = self.options.get("users") or {}
        self.users = list(users_state.get("users") or [])
        self.active_user_id = users_state.get("active")
        self._startup_locked = bool(users_state.get("startup_locked"))
        # Whether showing/re-showing the window should re-lock behind the PIN.
        # Same condition as the startup gate (active user locked AND opted into
        # a startup PIN), so closing to tray and reopening re-prompts — but a
        # locked user without the startup option is not gated on reopen.
        self._lock_on_show = self._startup_locked
        # True while the locked gate is actively gating content. IPC "navigate"
        # commands (e.g. the tray's Configure Servers / Show Console) are
        # swallowed while set, so they can't reveal content behind the lock.
        self._locked_active = False
        # Server addresses already used by any user, offered as one-click fill /
        # Quick Connect targets so a new user isn't retyped from scratch.
        self.known_servers = list(users_state.get("known_servers") or [])
        self._switcher_users = []  # index-aligned with the user combobox
        self._pin_dialog = None    # open PIN dialog awaiting a switch_result
        self._pending_switch = None  # user dict of an in-flight switch
        self.log_lines = deque(maxlen=2000)
        self.settings_values = dict(self.options.get("settings") or {})
        self.settings_schema = dict(self.options.get("settings_schema") or {})
        sync_state = self.options.get("sync_state") or {}
        self.sync_items = set(sync_state.get("items") or [])
        self.sync_series = set(sync_state.get("series") or [])
        self.sync_total = sync_state.get("total_bytes", 0)
        self.sync_active = sync_state.get("active", 0)
        self.sync_downloading = sync_state.get("downloading")
        self._dl_percent = None
        self.catalog_path = self.options.get("catalog_path")
        self._download_dialog = None
        self._close_dialog = None  # first-close preference prompt, if open
        self._syncplay_dialog = None  # SyncPlay join dialog, if open
        self._add_to_dialog = None    # add-to-playlist/collection picker
        # Playlist/collection editing needs apiclient >= 1.15; the edit
        # affordances hide entirely when it's older.
        self.edit_apis = bool(self.options.get("edit_apis"))

        self._live_servers = servers
        self._verify_ssl = verify_ssl
        self.is_offline = False
        # True while the main process is still attempting to connect; failed is
        # set once an attempt settles with no reachable server.
        self._connecting = bool(self.options.get("connecting"))
        self._connect_failed = False
        # Set when the user explicitly asks to go back online from the offline
        # banner: on a successful reconnect we clear the work_offline setting so
        # the next launch isn't silently offline again (see _on_banner_retry).
        self._clear_offline_on_reconnect = False
        self.source = LibrarySource(
            servers, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), verify_ssl)
        self.current_server = self._initial_server()

        self._build_chrome()
        if self.settings_values.get("work_offline"):
            self._enter_offline()
        self._refresh_user_switcher()
        self._refresh_server_switcher()
        self._show_initial()
        self._update_statusbar()

        if self.options.get("start_hidden"):
            self.root.withdraw()

        self.root.after(30, self._pump)
        r_queue.put(("ready_browser", None))

    # -- chrome ------------------------------------------------------------

    def _build_chrome(self):
        tk, ttk = self.tk, self.ttk
        bar = tk.Frame(self.root, bg=WINDOW_BG)
        bar.pack(fill="x", side="top")

        self.back_btn = ttk.Button(bar, text=_("◀ Back"), command=self.go_back,
                                   width=8)
        self.back_btn.pack(side="left", padx=(8, 2), pady=6)
        ttk.Button(bar, text=_("🏠 Home"), width=8,
                   command=lambda: self.navigate({"kind": "home"}, reset=True)).pack(
            side="left", padx=2, pady=6)
        ttk.Button(bar, text=_("⚙ Settings"), width=10,
                   command=lambda: self.navigate({"kind": "settings"})).pack(
            side="left", padx=2, pady=6)
        ttk.Button(bar, text=_("SyncPlay"), width=9,
                   command=self.open_syncplay).pack(side="left", padx=2, pady=6)

        # User + server switchers share a frame so the user selector always sits
        # to the left of the server selector. Each is hidden when there's only
        # one option (one user / one server).
        self.switch_frame = tk.Frame(bar, bg=WINDOW_BG)
        self.switch_frame.pack(side="left")

        self.user_var = tk.StringVar()
        self.user_box = ttk.Combobox(self.switch_frame, textvariable=self.user_var,
                                     state="readonly", width=16)
        self.user_box.bind("<<ComboboxSelected>>", self._on_user_change)

        self.server_var = tk.StringVar()
        self.server_box = ttk.Combobox(self.switch_frame, textvariable=self.server_var,
                                       state="readonly", width=22)
        self.server_box.bind("<<ComboboxSelected>>", self._on_server_change)

        # Search on the right.
        search_frame = tk.Frame(bar, bg=WINDOW_BG)
        search_frame.pack(side="right", padx=8, pady=6)
        self.search_var = tk.StringVar()
        entry = ttk.Entry(search_frame, textvariable=self.search_var, width=28)
        entry.pack(side="left")
        entry.bind("<Return>", lambda _e: self._do_search())
        ttk.Button(search_frame, text=_("Search"), command=self._do_search).pack(
            side="left", padx=(4, 0))

        self.topbar = bar

        # Offline banner (shown when work_offline or the server is unreachable).
        self.banner = tk.Frame(self.root, bg="#5a3a00")
        self.banner_label = tk.Label(self.banner, text="", bg="#5a3a00",
                                     fg="#ffd479", anchor="w")
        self.banner_label.pack(side="left", padx=12, pady=4)
        # "Configure Servers" goes straight to server management (add/fix a
        # server) since a failed retry with downloads present would otherwise
        # bounce back to offline and never surface the login form.
        ttk.Button(self.banner, text=_("Configure Servers"),
                   command=lambda: self.navigate(
                       {"kind": "settings", "tab": "servers"})).pack(
            side="right", padx=(4, 8), pady=2)
        ttk.Button(self.banner, text=_("Retry"),
                   command=self._on_banner_retry).pack(
            side="right", padx=4, pady=2)

        self.content = tk.Frame(self.root, bg=CARD_BG)
        self.content.pack(fill="both", expand=True)

        # Persistent download status bar (shown only while downloads are active).
        self.statusbar = tk.Frame(self.root, bg=PANEL_BG)
        self.status_label = tk.Label(self.statusbar, text="", bg=PANEL_BG,
                                     fg=TEXT_FG, anchor="w")
        self.status_label.pack(side="left", padx=12, pady=5)
        ttk.Button(self.statusbar, text=_("View Downloads"),
                   command=lambda: self.navigate(
                       {"kind": "settings", "tab": "downloads"})).pack(
            side="right", padx=8, pady=4)

        self._build_playbar()

    # -- now-playing music bar --------------------------------------------

    def _build_playbar(self):
        """A persistent bottom bar shown while AUDIO is playing (mirrors the
        download statusbar's show/hide). State arrives via the ("playstate", …)
        push from the main process; controls go back over r_queue."""
        tk, ttk = self.tk, self.ttk
        self._pb_state = None
        self._pb_pos = 0.0
        self._pb_dur = 0.0
        self._pb_playing = False
        self._pb_dragging = False   # user is scrubbing the seek slider
        self._pb_sync = False       # we're setting a slider programmatically

        bar = tk.Frame(self.root, bg=PANEL_BG)
        self.playbar = bar

        transport = tk.Frame(bar, bg=PANEL_BG)
        transport.pack(side="left", padx=(10, 6), pady=4)
        prev_btn = ttk.Button(transport, text="⏮", width=3,
                              style="Playbar.TButton",
                              command=lambda: self._send_r("play_prev"))
        prev_btn.pack(side="left", padx=2)
        self._pb_playpause = ttk.Button(
            transport, text="⏸", width=3, style="Playbar.TButton",
            command=lambda: self._send_r("playpause"))
        self._pb_playpause.pack(side="left", padx=2)
        next_btn = ttk.Button(transport, text="⏭", width=3,
                              style="Playbar.TButton",
                              command=lambda: self._send_r("play_next"))
        next_btn.pack(side="left", padx=2)

        # Right-side controls (packed right-to-left).
        self._pb_repeat = ttk.Button(bar, text="🔁", width=3,
                                     style="Playbar.TButton",
                                     command=self._cycle_repeat)
        self._pb_repeat.pack(side="right", padx=(2, 10))
        self._pb_fav = ttk.Button(bar, text="♡", width=3,
                                  style="Playbar.TButton",
                                  command=lambda: self._send_r("toggle_favorite"))
        self._pb_fav.pack(side="right", padx=2)
        self._pb_vol = ttk.Scale(bar, from_=0, to=100, length=90,
                                 orient="horizontal")
        self._pb_vol.pack(side="right", padx=(2, 2))
        # Click anywhere on the volume track jumps to that level (like seek);
        # dragging previews, and only the release commits — so a drag doesn't
        # flood the main process with a set_volume per pixel.
        self._pb_vol.bind("<Button-1>", self._on_vol_scrub)
        self._pb_vol.bind("<B1-Motion>", self._on_vol_scrub)
        self._pb_vol.bind("<ButtonRelease-1>", self._on_volume_release)
        tk.Label(bar, text="🔊", bg=PANEL_BG, fg=SUBTLE_FG).pack(
            side="right", padx=(8, 0))

        # Center: title + scrubber + times fill the remaining width.
        self._pb_title = tk.Label(bar, text="", bg=PANEL_BG, fg=TEXT_FG,
                                  anchor="w")
        self._pb_title.pack(side="left", padx=(4, 8))
        self._pb_dur_lbl = tk.Label(bar, text="0:00", bg=PANEL_BG, fg=SUBTLE_FG,
                                    width=5)
        self._pb_dur_lbl.pack(side="right", padx=(2, 8))
        self._pb_seek = ttk.Scale(bar, from_=0, to=1000, orient="horizontal")
        self._pb_seek.pack(side="left", fill="x", expand=True, padx=4)
        # Click anywhere on the track jumps there (default trough-click only
        # pages a few seconds); drag scrubs; release commits the seek.
        self._pb_seek.bind("<Button-1>", self._on_seek_press)
        self._pb_seek.bind("<B1-Motion>", self._on_seek_drag)
        self._pb_seek.bind("<ButtonRelease-1>", self._on_seek_release)
        self._pb_pos_lbl = tk.Label(bar, text="0:00", bg=PANEL_BG, fg=SUBTLE_FG,
                                    width=5)
        self._pb_pos_lbl.pack(side="left")

        for widget, tip in ((prev_btn, _("Previous")),
                            (self._pb_playpause, _("Play / Pause")),
                            (next_btn, _("Next")),
                            (self._pb_vol, _("Volume")),
                            (self._pb_fav, _("Favorite")),
                            (self._pb_repeat, _("Repeat"))):
            self._attach_tooltip(widget, tip)

        self.root.after(1000, self._tick_playbar)

    def _attach_tooltip(self, widget, text):
        """A minimal hover tooltip (no external deps). Shows a small label just
        above the widget on <Enter>, hides on <Leave>/click."""
        state = {"win": None}

        def show(_e=None):
            if state["win"] is not None or not text:
                return
            win = self.tk.Toplevel(widget)
            win.wm_overrideredirect(True)
            self.tk.Label(win, text=text, bg="#0b0c0e", fg=TEXT_FG,
                          padx=6, pady=2, font=("TkDefaultFont", 8)).pack()
            win.update_idletasks()
            x = widget.winfo_rootx() + (widget.winfo_width()
                                        - win.winfo_width()) // 2
            y = widget.winfo_rooty() - win.winfo_height() - 3
            win.wm_geometry("+%d+%d" % (max(0, x), max(0, y)))
            state["win"] = win

        def hide(_e=None):
            if state["win"] is not None:
                try:
                    state["win"].destroy()
                except Exception:
                    pass
                state["win"] = None

        widget.bind("<Enter>", show, add="+")
        widget.bind("<Leave>", hide, add="+")
        widget.bind("<ButtonPress>", hide, add="+")

    @staticmethod
    def _scale_value_from_x(scale, x):
        w = scale.winfo_width()
        if w <= 1:
            return None
        frac = min(max(x / w, 0.0), 1.0)
        return frac * float(scale.cget("to"))

    def _seek_value_from_x(self, x):
        return self._scale_value_from_x(self._pb_seek, x)

    def _on_vol_scrub(self, e):
        # Move the grip to the click/drag position; don't send yet.
        val = self._scale_value_from_x(self._pb_vol, e.x)
        if val is None:
            return "break"
        self._pb_sync = True
        try:
            self._pb_vol.set(val)
        finally:
            self._pb_sync = False
        return "break"

    @staticmethod
    def _fmt_time(seconds):
        seconds = int(max(0, seconds or 0))
        return "%d:%02d" % (seconds // 60, seconds % 60)

    def _send_r(self, action, param=None):
        try:
            self.r_queue.put((action, param))
        except Exception:
            log.debug("Failed to send %s to main process", action, exc_info=True)

    def _on_playstate(self, state):
        state = state or {}
        # Bar is music-only: hide for video and when nothing is playing.
        if state.get("stopped") or not state.get("is_audio"):
            if self.playbar.winfo_ismapped():
                self.playbar.pack_forget()
            self._pb_state = None
            self._pb_playing = False
            return
        self._pb_state = state
        self._pb_pos = float(state.get("position") or 0.0)
        self._pb_dur = float(state.get("duration") or 0.0)
        self._pb_playing = not state.get("paused")
        if not self.playbar.winfo_ismapped():
            self.playbar.pack(side="bottom", fill="x")
        self._render_playbar()

    def _render_playbar(self):
        st = self._pb_state
        if not st:
            return
        title, artist = st.get("title", ""), st.get("artist", "")
        self._pb_title.config(text=("%s — %s" % (title, artist)) if artist
                              else title)
        self._pb_playpause.config(text="▶" if st.get("paused") else "⏸")
        self._pb_fav.config(text="♥" if st.get("favorite") else "♡")
        self._pb_repeat.config(
            text="🔂" if st.get("repeat") == "one" else "🔁",
            style=("PlaybarOn.TButton" if st.get("repeat") in ("all", "one")
                   else "Playbar.TButton"))
        self._pb_dur_lbl.config(text=self._fmt_time(self._pb_dur))
        self._pb_sync = True
        try:
            self._pb_vol.set(st.get("volume") or 0)
            self._pb_seek.configure(to=max(1.0, self._pb_dur))
            if not self._pb_dragging:
                self._pb_seek.set(self._pb_pos)
                self._pb_pos_lbl.config(text=self._fmt_time(self._pb_pos))
        finally:
            self._pb_sync = False

    def _tick_playbar(self):
        # Interpolate the scrubber between the 5s state pushes so it moves
        # smoothly while playing.
        try:
            if (self._pb_state and self._pb_playing and not self._pb_dragging
                    and self.playbar.winfo_ismapped()):
                self._pb_pos = min(self._pb_pos + 1.0, self._pb_dur or self._pb_pos + 1.0)
                self._pb_sync = True
                try:
                    self._pb_seek.set(self._pb_pos)
                    self._pb_pos_lbl.config(text=self._fmt_time(self._pb_pos))
                finally:
                    self._pb_sync = False
        finally:
            self.root.after(1000, self._tick_playbar)

    def _on_seek_press(self, e):
        self._pb_dragging = True
        self._seek_scrub_to(e.x)
        return "break"  # replace the default trough paging with jump-to-click

    def _on_seek_drag(self, e):
        self._seek_scrub_to(e.x)
        return "break"

    def _seek_scrub_to(self, x):
        val = self._seek_value_from_x(x)
        if val is None:
            return
        self._pb_sync = True
        try:
            self._pb_seek.set(val)
            self._pb_pos_lbl.config(text=self._fmt_time(val))
        finally:
            self._pb_sync = False

    def _on_seek_release(self, _e):
        if not self._pb_dragging:
            return
        self._pb_dragging = False
        if self._pb_state:
            pos = float(self._pb_seek.get())
            self._pb_pos = pos
            self._pb_pos_lbl.config(text=self._fmt_time(pos))
            self._send_r("seek_to", pos)

    def _on_volume_release(self, _e):
        if not self._pb_sync:
            self._send_r("set_volume", float(self._pb_vol.get()))

    def _cycle_repeat(self):
        order = ("none", "all", "one")
        cur = (self._pb_state or {}).get("repeat", "none")
        nxt = order[(order.index(cur) + 1) % 3] if cur in order else "all"
        self._send_r("set_repeat", nxt)

    # -- offline mode ------------------------------------------------------

    def _show_banner(self, text):
        self.banner_label.config(text=text)
        if not self.banner.winfo_ismapped():
            self.banner.pack(fill="x", side="top", before=self.content)

    def _hide_banner(self):
        if self.banner.winfo_ismapped():
            self.banner.pack_forget()

    def _enter_offline(self, message=None):
        if self.catalog_path is None:
            return
        try:
            if not isinstance(self.source, OfflineLibrarySource):
                self.source.stop()
        except Exception:
            pass
        self.source = OfflineLibrarySource(self.catalog_path)
        self.is_offline = True
        self.current_server = "offline"
        self.home_cache = {}
        self._refresh_server_switcher()
        self._refresh_user_switcher()
        self._show_banner(message or _("Offline — showing downloaded content."))

    def _exit_offline(self):
        self.source = LibrarySource(
            self._live_servers, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), self._verify_ssl)
        self.is_offline = False
        self.current_server = self._initial_server()
        self.home_cache = {}
        self._refresh_server_switcher()
        self._refresh_user_switcher()
        self._hide_banner()

    def set_offline(self, offline):
        if offline:
            # Deliberately going offline supersedes any pending banner-retry
            # clear, so a later reconnect doesn't wipe a freshly-set preference.
            self._clear_offline_on_reconnect = False
            self._enter_offline()
            self.navigate({"kind": "home"}, reset=True)
            return
        # Going online.
        self._exit_offline()
        if self._clear_offline_on_reconnect and self.server_list:
            # We're about to persist work_offline=False, so we must confirm the
            # server is actually reachable first. current_server here is derived
            # from the possibly-stale _live_servers snapshot (not refreshed on
            # entering offline), so trust the main process's fresh connection
            # result via _on_connection_settled instead of clearing eagerly.
            self.retry_connect()
        elif self.current_server:
            # A live connection and nothing to persist — go straight home.
            self._maybe_clear_offline_setting()
            self.navigate({"kind": "home"}, reset=True)
        elif self.server_list:
            # We have accounts but no live connection yet — (re)connect and wait
            # on the connecting screen instead of flashing the login form. The
            # pending clear is resolved in _on_connection_settled.
            self.retry_connect()
        else:
            # No connection and nothing to reconnect to: not a success.
            self._clear_offline_on_reconnect = False
            self.navigate({"kind": "login"}, reset=True)

    def _on_banner_retry(self):
        """Offline-banner Retry: the user is explicitly asking to go back
        online. If offline was forced by the work_offline setting, clear it on a
        successful reconnect so the next launch isn't silently offline again."""
        self._clear_offline_on_reconnect = True
        self.set_offline(False)

    def _maybe_clear_offline_setting(self):
        """After a successful, user-requested return to online, stop forcing
        offline mode by persisting work_offline=False (only if it was set)."""
        if not self._clear_offline_on_reconnect:
            return
        self._clear_offline_on_reconnect = False
        if self.settings_values.get("work_offline"):
            self.settings_values["work_offline"] = False
            self.save_settings({"work_offline": False})

    def retry_connect(self):
        """Ask the main process to (re)attempt the connection and show the
        connecting screen until it settles."""
        self._connecting = True
        self._connect_failed = False
        self.r_queue.put(("retry_connect", None))
        self.navigate({"kind": "connecting"}, reset=True)

    def _on_connection_settled(self, payload):
        """Main finished a connection attempt: update state and land the user on
        the right screen (home / downloads / login)."""
        self.server_list = list(payload.get("all") or [])
        connected = payload.get("connected") or []
        if payload.get("device_id"):
            self.options["device_id"] = payload["device_id"]
        self._live_servers = connected
        self._connecting = False
        self._connect_failed = bool(self.server_list) and not connected
        if self.is_offline:
            # Browsing downloads by choice/fallback — don't pull the user out;
            # Go Online uses the refreshed connected list when they choose to.
            return
        self._rebuild_live_source(connected)
        self._refresh_server_switcher()
        self._refresh_user_switcher()
        kind = self.nav_stack[-1]["kind"] if self.nav_stack else None
        if self.current_server:
            # Reconnect succeeded — honour a pending "clear offline setting".
            self._maybe_clear_offline_setting()
            if kind in (None, "connecting", "login"):
                self.navigate({"kind": "home"}, reset=True)
        elif kind in (None, "connecting"):
            self._clear_offline_on_reconnect = False  # reconnect failed
            self._show_disconnected()
        elif kind == "login":
            self._clear_offline_on_reconnect = False  # reconnect failed
            self._render_top()  # refresh the retry / failed-connection UI

    def _rebuild_live_source(self, connected):
        try:
            self.source.stop()
        except Exception:
            pass
        self.source = LibrarySource(
            connected, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), self._verify_ssl)
        self._live_servers = connected
        self.home_cache = {}
        # Keep the current selection if it's still connected, otherwise restore
        # the last-used server (falling back to the first) — don't just grab
        # whichever server resolved first.
        if self.current_server not in {s["uuid"] for s in self.source.servers()}:
            self.current_server = self._initial_server()

    def offline_fallback(self):
        """Auto-switch to the catalog when the live server is unreachable."""
        if self.is_offline or self.catalog_path is None or not self.sync_items:
            return
        self._enter_offline(_("Server unreachable — showing downloads."))
        self.navigate({"kind": "home"}, reset=True)

    def _update_statusbar(self):
        if self.sync_active and self.sync_active > 0:
            name = self.sync_downloading or _("Preparing…")
            pct = (" %d%%" % self._dl_percent) if self._dl_percent is not None else ""
            self.status_label.config(text=_("⬇ Downloading %(name)s%(pct)s "
                                            "— %(n)d remaining") % {
                "name": name, "pct": pct, "n": self.sync_active})
            if not self.statusbar.winfo_ismapped():
                self.statusbar.pack(side="bottom", fill="x")
        elif self.statusbar.winfo_ismapped():
            self.statusbar.pack_forget()

    def _show_initial(self):
        if self._startup_locked and not self.is_offline:
            # The active user opted into a startup PIN — gate everything behind
            # it until unlocked (or another, unlocked user is chosen).
            self._show_locked_gate()
            return
        if self.is_offline or self.current_server:
            self.navigate({"kind": "home"}, reset=True)
        elif self._connecting:
            # Connection is still in flight — show the window now and wait.
            self.navigate({"kind": "connecting"}, reset=True)
        else:
            self._show_disconnected()

    def _show_disconnected(self):
        """No live connection: browse downloads if we have any, otherwise the
        login screen (which surfaces a retry when accounts exist)."""
        if self.catalog_path and self.sync_items:
            self._enter_offline(_("Server unreachable — showing downloads."))
            self.navigate({"kind": "home"}, reset=True)
        else:
            self.navigate({"kind": "login"}, reset=True)

    def _refresh_server_switcher(self):
        # Key by combobox index, not display name: two servers can share a name
        # and keying by name would collapse them into one entry.
        servers = self.source.servers()
        self._switcher_servers = servers  # index-aligned with the combobox values
        names = [s["name"] for s in servers]
        self.server_box.config(values=names)
        if len(servers) > 1:
            cur_idx = next((i for i, s in enumerate(servers)
                            if s["uuid"] == self.current_server), 0)
            self.server_box.current(cur_idx)
            self.server_box.pack(side="left", padx=8, pady=6)
        else:
            self.server_box.pack_forget()

    def _on_server_change(self, _e):
        idx = self.server_box.current()
        if not (0 <= idx < len(self._switcher_servers)):
            return
        uuid = self._switcher_servers[idx]["uuid"]
        if uuid and uuid != self.current_server:
            self.current_server = uuid
            self._persist_server(uuid)
            self.navigate({"kind": "home"}, reset=True)

    # -- user switching ----------------------------------------------------

    def _refresh_user_switcher(self):
        users = self.users
        self._switcher_users = users
        def label(u):
            return ("\U0001F512 " if u.get("locked") else "") + u.get("name", "?")
        self.user_box.config(values=[label(u) for u in users])
        # Only meaningful with more than one user and while we're online (a
        # switch reconnects servers).
        if len(users) > 1 and not self.is_offline:
            cur_idx = next((i for i, u in enumerate(users)
                            if u["id"] == self.active_user_id), 0)
            self.user_box.current(cur_idx)
            try:
                self.user_box.selection_clear()
            except Exception:
                pass
            if not self.user_box.winfo_ismapped():
                if self.server_box.winfo_ismapped():
                    self.user_box.pack(side="left", padx=8, pady=6,
                                       before=self.server_box)
                else:
                    self.user_box.pack(side="left", padx=8, pady=6)
        else:
            self.user_box.pack_forget()

    def _select_active_user_in_box(self):
        idx = next((i for i, u in enumerate(self._switcher_users)
                    if u["id"] == self.active_user_id), None)
        if idx is not None:
            self.user_box.current(idx)

    def _on_user_change(self, _e):
        idx = self.user_box.current()
        if not (0 <= idx < len(self._switcher_users)):
            return
        user = self._switcher_users[idx]
        if user["id"] == self.active_user_id:
            return
        # Don't leave the dropdown showing the target until the switch is
        # actually confirmed — a wrong PIN or failed connect must revert it.
        self._select_active_user_in_box()
        self.request_switch_user(user)

    def request_switch_user(self, user):
        """Kick off a switch to ``user`` (a {id,name,locked,...} dict). A locked
        user is gated behind a PIN dialog first."""
        if user["id"] == self.active_user_id:
            return
        if user.get("locked"):
            self._prompt_pin(
                _("Enter PIN"),
                _("Enter the PIN for %s.") % user.get("name", ""),
                lambda pin: self._send_switch(user, pin))
        else:
            self._send_switch(user, None)

    def _send_switch(self, user, pin):
        self._pending_switch = user
        payload = {"user_id": user["id"]}
        if pin is not None:
            payload["pin"] = pin
        self.r_queue.put(("switch_user", payload))
        if pin is None:
            # No dialog to hold the UI; show the connecting screen right away.
            self._enter_switching()

    def _enter_switching(self):
        # A switch/unlock is proceeding — the lock no longer gates content.
        self._locked_active = False
        self._connecting = True
        self._connect_failed = False
        self.navigate({"kind": "connecting"}, reset=True)

    def _prompt_pin(self, title, prompt, on_pin):
        from .views import PinDialog
        if self._pin_dialog is not None:
            return
        self._pin_dialog = PinDialog(self, title, prompt, on_pin)

    def _close_pin_dialog(self):
        if self._pin_dialog is not None:
            self._pin_dialog.close()
            self._pin_dialog = None

    def on_switch_result(self, result):
        result = result or {}
        if result.get("ok"):
            self._close_pin_dialog()
            self._enter_switching()
            return
        error = result.get("error") or _("Could not switch user.")
        self._pending_switch = None
        if self._pin_dialog is not None:
            self._pin_dialog.set_error(error)
            return
        # Let a view (e.g. the startup locked gate) show the error inline;
        # otherwise fall back to a message box.
        view = self.current_view
        if view is not None and hasattr(view, "on_switch_result"):
            view.on_switch_result(result)
        else:
            self._message(error)
        # A failure after _enter_switching leaves the connecting spinner up
        # with no connection_settled on the way (main started no connect
        # worker) — land back on a real screen. "busy" means another switch
        # IS still running and will push connection_settled; stay put then.
        kind = self.nav_stack[-1]["kind"] if self.nav_stack else None
        if kind == "connecting" and not result.get("busy"):
            self._connecting = False
            if self.current_server:
                self.navigate({"kind": "home"}, reset=True)
            else:
                self._show_disconnected()

    def _show_locked_gate(self):
        self._locked_active = True
        self.navigate({"kind": "locked"}, reset=True)

    def _maybe_relock(self):
        """Re-show the locked gate if the active user must re-enter their PIN on
        reopen. No-op if not applicable or already on the gate."""
        if not self._lock_on_show:
            return
        if self.nav_stack and self.nav_stack[-1].get("kind") == "locked":
            self._locked_active = True
            return
        self._show_locked_gate()

    def _on_users(self, param):
        if not isinstance(param, dict):
            return
        self.users = list(param.get("users") or [])
        self.active_user_id = param.get("active")
        self.known_servers = list(param.get("known_servers") or [])
        self._lock_on_show = bool(param.get("startup_locked"))
        self._refresh_user_switcher()
        self._dispatch_view("on_users_changed", self.users)

    def _message(self, text, title=None):
        try:
            from tkinter import messagebox
            messagebox.showinfo(title or USER_APP_NAME, text, parent=self.root)
        except Exception:
            log.info("%s", text)

    # user-management passthroughs used by the Servers panel
    def add_user(self, name):
        self.r_queue.put(("add_user", {"name": name}))

    def rename_user(self, user_id, name):
        self.r_queue.put(("rename_user", {"user_id": user_id, "name": name}))

    def delete_user(self, user_id):
        self.r_queue.put(("delete_user", {"user_id": user_id}))

    def set_user_pin(self, user_id, pin, require_startup, current_pin=None):
        self.r_queue.put(("set_user_pin", {
            "user_id": user_id, "pin": pin,
            "require_startup": require_startup, "current_pin": current_pin}))

    def _initial_server(self):
        server_list = self.source.servers()
        if not server_list:
            return None
        uuids = {s["uuid"] for s in server_list}
        preferred = self.options.get("last_server")
        if preferred in uuids:
            return preferred
        return server_list[0]["uuid"]

    def _persist_server(self, uuid):
        if uuid:
            self.r_queue.put(("set_last_server", uuid))

    def _on_wheel(self, event):
        if getattr(event, "num", None) == 4:
            units = -1
        elif getattr(event, "num", None) == 5:
            units = 1
        elif getattr(event, "delta", 0):
            units = -1 if event.delta > 0 else 1
        else:
            return
        try:
            w = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            w = None
        while w is not None:
            scroll = getattr(w, "_wheel_scroll", None)
            if scroll is not None:
                try:
                    scroll(units)
                except Exception:
                    pass
                return
            w = getattr(w, "master", None)

    def _do_search(self):
        term = self.search_var.get().strip()
        if term and self.current_server:
            self.navigate({"kind": "search", "term": term})

    def _show_message(self, text):
        for child in self.content.winfo_children():
            child.destroy()
        self.tk.Label(self.content, text=text, bg=CARD_BG, fg=SUBTLE_FG,
                      justify="center").pack(expand=True)

    # -- navigation --------------------------------------------------------

    def navigate(self, route, reset=False):
        if reset:
            self.nav_stack = []
        self.nav_stack.append(route)
        self._render_top()

    def go_back(self):
        if len(self.nav_stack) > 1:
            self.nav_stack.pop()
            self._render_top()

    def after_playlist_deleted(self, playlist_id):
        """The playlist is gone server-side: drop its detail + editor routes
        from the stack (both carry ``playlist_id``) and re-render whatever's
        underneath — the playlist list, which re-fetches without it."""
        root = self.nav_stack[0] if self.nav_stack else {"kind": "home"}
        kept = [r for r in self.nav_stack
                if r.get("playlist_id") != playlist_id]
        self.nav_stack = kept or [root]
        self._render_top()

    def _render_top(self):
        route = self.nav_stack[-1]
        # The login and locked screens are full-window chrome-free; everything
        # else shows the top bar.
        if route["kind"] in ("login", "locked"):
            self.topbar.pack_forget()
        elif not self.topbar.winfo_ismapped():
            self.topbar.pack(fill="x", side="top", before=self.content)

        for child in self.content.winfo_children():
            child.destroy()
        self.current_view = None
        view_cls = VIEW_TYPES.get(route["kind"])
        if view_cls is None:
            log.error("Unknown view kind %s", route["kind"])
            return
        try:
            view = view_cls(self, route)
            frame = view.build(self.content)
            frame.pack(fill="both", expand=True)
            self.current_view = view
        except Exception:
            log.error("Failed to build view %s", route["kind"], exc_info=True)
            self._show_message(_("Something went wrong rendering this screen."))
        self.back_btn.config(
            state="normal" if len(self.nav_stack) > 1 else "disabled")

    def open_item(self, item):
        itype = item.get("Type")
        title = item.get("Name", "")
        if itype == "Playlist":
            self.navigate({"kind": "playlist", "playlist_id": item["Id"],
                           "title": title})
        elif itype in SERIES_TYPES:
            self.navigate({"kind": "series", "series_id": item["Id"], "title": title})
        elif itype in FOLDER_TYPES:
            self.navigate({"kind": "grid", "parent_id": item["Id"],
                           "title": title, "parent_type": itype,
                           # Lets the grid offer the Collections toggle on Movie
                           # libraries (only libraries carry a CollectionType).
                           "collection_type": item.get("CollectionType")})
        elif itype in PLAYABLE_TYPES:
            self.navigate({"kind": "detail", "item_id": item["Id"], "title": title})
        else:
            log.debug("No navigation for item type %s", itype)

    # -- async + playback --------------------------------------------------

    def run_async(self, work, done, on_error=None):
        def task():
            try:
                result = work()
            except Exception as exc:
                log.warning("Background task failed", exc_info=True)
                if on_error:
                    # Bind exc now: Python clears the `as exc` name when the
                    # except block exits, so a bare closure would NameError.
                    self._ui_queue.put(lambda exc=exc: on_error(exc))
                return
            self._ui_queue.put(lambda: done(result))
        self._api_pool.submit(task)

    def play(self, payload):
        log.info("Requesting playback: %s", payload.get("item_ids"))
        self.r_queue.put(("play", payload))

    def play_episode(self, episode, offset_ticks=None, resume_auto=False,
                     aid=None, sid=None, srcid=None):
        """Play an episode, queueing the following episodes (across seasons) so
        the player's autoplay-next chains through them, like the web app.

        Falls back to a single-item play if the series can't be resolved.
        ``resume_auto`` derives the resume position from the episode's UserData
        when ``offset_ticks`` is not given (used by the Play-Next-Up button).
        """
        server = self.current_server
        ep_id = episode.get("Id")
        series_id = episode.get("SeriesId")
        if resume_auto and offset_ticks is None:
            offset_ticks = (episode.get("UserData") or {}).get(
                "PlaybackPositionTicks") or None

        def send(item_ids, start_index):
            self.play({
                "server_uuid": server,
                "item_ids": item_ids,
                "start_index": start_index,
                "offset_ticks": offset_ticks,
                "media_source_id": srcid,
                "audio_index": aid,
                "subtitle_index": sid,
            })

        if not (ep_id and series_id):
            send([ep_id], 0)
            return

        def work():
            # Episodes from this one onward, spanning seasons.
            return self.source.get_series_queue(server, series_id, ep_id)

        def done(eps):
            ids = [e.get("Id") for e in eps if e.get("Id")]
            if ids and ep_id in ids:
                send(ids, ids.index(ep_id))
            else:
                send([ep_id], 0)

        self.run_async(work, done, lambda _e: send([ep_id], 0))

    def play_next_up(self, series_id):
        """Play a series' next-up episode (or its first episode if unwatched),
        queueing the following episodes across seasons."""
        server = self.current_server

        def work():
            episode = self.source.get_next_up(server, series_id)
            if episode is None:
                # Unwatched series: NextUp may be empty — start from episode 1.
                first = self.source.get_series_queue(server, series_id, limit=1)
                episode = first[0] if first else None
            return episode

        def done(episode):
            if episode:
                self.play_episode(episode, resume_auto=True)

        self.run_async(work, done, lambda _e: None)

    def set_favorite(self, server_uuid, item_id, favorite):
        """Toggle an item's favorite heart (server-side; views update
        optimistically like the watched toggle)."""
        self.r_queue.put(("set_favorite", {
            "server_uuid": server_uuid, "item_id": item_id,
            "favorite": bool(favorite)}))

    # -- syncplay ------------------------------------------------------------

    def open_syncplay(self):
        from .views import SyncPlayDialog
        if self.is_offline:
            self._message(_("SyncPlay needs a server connection."))
            return
        if self._syncplay_dialog is not None:
            try:
                self._syncplay_dialog.win.lift()
                return
            except Exception:
                self._syncplay_dialog = None
        self._syncplay_dialog = SyncPlayDialog(self)

    def request_syncplay_groups(self):
        self.r_queue.put(("syncplay_groups", None))

    def syncplay_join(self, server_uuid, group_id):
        self.r_queue.put(("syncplay_join", {
            "server_uuid": server_uuid, "group_id": group_id}))

    def syncplay_leave(self):
        self.r_queue.put(("syncplay_leave", None))

    # -- playlist / collection editing --------------------------------------

    def playlist_edit(self, payload):
        self.r_queue.put(("playlist_edit", payload))

    def collection_edit(self, payload):
        self.r_queue.put(("collection_edit", payload))

    def open_add_to_dialog(self, item, kind):
        """Open the add-to-playlist / add-to-collection picker for an item."""
        from .views import AddToDialog
        if self._add_to_dialog is not None:
            try:
                self._add_to_dialog.win.lift()
                return
            except Exception:
                self._add_to_dialog = None
        self._add_to_dialog = AddToDialog(self, item, kind)

    def set_watched(self, server_uuid, item_id, watched, refresh=False):
        """Mark an item (movie/episode, or a whole series/season) played or
        unplayed. ``refresh`` asks the main process to confirm so the current
        view re-fetches (used for bulk marks that change other items); per-item
        toggles update optimistically instead."""
        self.r_queue.put(("set_watched", {
            "server_uuid": server_uuid, "item_id": item_id,
            "watched": bool(watched), "refresh": refresh}))

    def add_server(self, payload):
        self.r_queue.put(("add_server", payload))

    def quick_connect(self, server):
        self.r_queue.put(("quick_connect", {"server": server}))

    def quick_connect_cancel(self):
        self.r_queue.put(("quick_connect_cancel", None))

    def remove_server(self, uuid):
        if uuid:
            self.r_queue.put(("remove_server", uuid))

    def request_logs(self):
        self.r_queue.put(("request_logs", None))

    def save_settings(self, changes):
        self.r_queue.put(("save_settings", changes))

    # -- downloads ---------------------------------------------------------

    def is_downloaded(self, item):
        if item.get("Id") in self.sync_items:
            return True
        if item.get("Type") == "Series" and item.get("Id") in self.sync_series:
            return True
        return False

    def open_download_dialog(self, server_uuid, item_id, item_type, title):
        from .views import DownloadDialog
        if self._download_dialog is not None:
            return
        self._download_dialog = DownloadDialog(self, server_uuid, item_id,
                                               item_type, title)

    def estimate_download(self, server_uuid, item_id, item_type):
        self.r_queue.put(("estimate_download", {
            "server_uuid": server_uuid, "item_id": item_id,
            "item_type": item_type}))

    def download(self, server_uuid, item_id, item_type, include_watched):
        self.r_queue.put(("download", {
            "server_uuid": server_uuid, "item_id": item_id,
            "item_type": item_type, "include_watched": include_watched}))

    def delete_download(self, item_id=None, series_id=None, season_id=None,
                        watched_only=False, playlist_id=None):
        self.r_queue.put(("delete_download", {
            "item_id": item_id, "series_id": series_id, "season_id": season_id,
            "playlist_id": playlist_id, "watched_only": watched_only}))

    # -- IPC pump ----------------------------------------------------------

    def _pump(self):
        if self._closing:
            return
        try:
            self.thumbs.pump()
        except Exception:
            log.debug("thumbnail pump error", exc_info=True)

        while True:
            try:
                cb = self._ui_queue.get_nowait()
            except queue.Empty:
                break
            try:
                cb()
            except Exception:
                log.debug("UI callback error", exc_info=True)

        try:
            while True:
                try:
                    cmd, param = self.cmd_queue.get_nowait()
                except queue.Empty:
                    break
                try:
                    self._handle_cmd(cmd, param)
                except Exception:
                    # A throwing handler must never kill the pump — that would
                    # freeze all further IPC (show/hide, progress, even die).
                    log.error("IPC command %r failed", cmd, exc_info=True)
        finally:
            if not self._closing:
                self.root.after(30, self._pump)

    def _handle_cmd(self, cmd, param):
        if cmd == "show":
            # Re-lock a locked user on reopen (if they require the startup PIN)
            # before revealing anything.
            self._maybe_relock()
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.focus_force()
            except Exception:
                pass
        elif cmd == "hide":
            # Gate the content now, while hidden, so it isn't briefly visible
            # when the window is reopened.
            self._maybe_relock()
            self.root.withdraw()
        elif cmd == "servers":
            self._reload_servers(param)
        elif cmd == "server_status":
            # Status-only refresh (e.g. cast-session badge). Update the server
            # metadata and re-render the settings panel if it's open; don't
            # rebuild the live source.
            if isinstance(param, list):
                self.server_list = list(param)
                self._dispatch_view("on_servers_changed", self.server_list)
        elif cmd == "server_result":
            self._dispatch_view("on_server_result", param or {})
        elif cmd == "quick_connect_code":
            self._dispatch_view("on_quick_connect_code", param or {})
        elif cmd == "navigate":
            # Don't let an external navigation (e.g. the tray's Configure
            # Servers / Show Console) reveal content behind the locked gate.
            if param and not self._locked_active:
                self.navigate(param)
        elif cmd == "log_init":
            self.log_lines.clear()
            self.log_lines.extend(param or [])
            self._dispatch_view("on_log_init", list(self.log_lines))
        elif cmd == "log_line":
            if param is not None:
                self.log_lines.append(param)
                self._dispatch_view("on_log_line", param)
        elif cmd == "settings_data":
            if isinstance(param, dict):
                was_offline = bool(self.settings_values.get("work_offline"))
                self.settings_values = param
                now_offline = bool(param.get("work_offline"))
                if now_offline != was_offline:
                    self.set_offline(now_offline)
            self._dispatch_view("on_settings_data", param)
        elif cmd == "catalog_path":
            # The download folder moved (main process re-pointed the catalog).
            # Point future offline reads at the new location; reopen now if we
            # are currently showing offline content.
            self.catalog_path = param
            if self.is_offline:
                self._enter_offline()
        elif cmd == "settings_status":
            self._dispatch_view("on_settings_status", param or {})
        elif cmd == "download_folder_progress":
            self._dispatch_view("on_folder_progress", param or {})
        elif cmd == "sync_state":
            ss = param or {}
            self.sync_items = set(ss.get("items") or [])
            self.sync_series = set(ss.get("series") or [])
            self.sync_total = ss.get("total_bytes", 0)
            self.sync_active = ss.get("active", 0)
            self.sync_downloading = ss.get("downloading")
            self._dl_percent = None  # new item / state change; await fresh progress
            self._update_statusbar()
            # Let the current view update its download affordance in place. A
            # full _render_top() here would reset the Detail track pickers and
            # scroll on every queue change during a season download.
            self._dispatch_view("on_sync_state", ss)
        elif cmd == "download_estimate":
            est = param or {}
            dlg = self._download_dialog
            # Only apply the estimate if it belongs to the dialog currently
            # open: a slow estimate for a dismissed dialog must not overwrite
            # the numbers of the one the user opened next.
            if dlg is not None and (
                est.get("item_id") is None or est.get("item_id") == dlg.item_id
            ):
                dlg.on_estimate(est)
        elif cmd == "download_progress":
            payload = param or {}
            total = payload.get("total", 0)
            self._dl_percent = (int(payload.get("downloaded", 0) * 100 / total)
                                if total else None)
            if payload.get("name"):
                self.sync_downloading = payload["name"]
            self._update_statusbar()
            self._dispatch_view("on_download_progress", payload)
        elif cmd == "playstate":
            self._on_playstate(param or {})
        elif cmd == "connection_settled":
            self._on_connection_settled(param or {})
        elif cmd == "users":
            self._on_users(param)
        elif cmd == "switch_result":
            self.on_switch_result(param or {})
        elif cmd == "user_action_error":
            self._message((param or {}).get("error")
                          or _("The action could not be completed."))
        elif cmd == "syncplay_groups":
            if self._syncplay_dialog is not None:
                self._syncplay_dialog.on_groups(param or {})
        elif cmd == "edit_result":
            res = param or {}
            if not res.get("ok") and res.get("error"):
                self._message(res["error"])
            self._dispatch_view("on_edit_result", res)
        elif cmd == "watched_changed":
            # A bulk watched/unwatched mark was applied server-side; re-fetch the
            # current view so cascaded child state shows correctly.
            self._render_top()
        elif cmd == "die":
            self._shutdown()

    def _dispatch_view(self, method, arg):
        view = self.current_view
        handler = getattr(view, method, None) if view is not None else None
        if callable(handler):
            try:
                handler(arg)
            except Exception:
                log.debug("View %s handler failed", method, exc_info=True)

    def _reload_servers(self, payload):
        if isinstance(payload, dict):
            connected = payload.get("connected") or []
            self.server_list = list(payload.get("all") or [])
            if payload.get("device_id"):
                self.options["device_id"] = payload["device_id"]
        else:  # backwards-compatible: a bare connected list
            connected = payload or []
        self._live_servers = connected
        kind = self.nav_stack[-1]["kind"] if self.nav_stack else None
        if self.is_offline:
            # Don't replace the offline catalog source; just note the new creds.
            if kind == "settings":
                self._dispatch_view("on_servers_changed", self.server_list)
            return
        self._rebuild_live_source(connected)
        self._refresh_server_switcher()
        self._refresh_user_switcher()

        if kind == "settings":
            self._dispatch_view("on_servers_changed", self.server_list)
        elif self.current_server:
            if kind in (None, "login", "connecting"):
                self.navigate({"kind": "home"}, reset=True)
            # otherwise leave the current screen alone (just updated the switcher)
        else:
            self.navigate({"kind": "login"}, reset=True)

    # -- lifecycle ---------------------------------------------------------

    def _on_close(self):
        # First time (and only if a tray exists to minimize to), ask whether
        # closing should minimize to tray or exit, then remember the answer.
        if (not self.settings_values.get("close_prompt_shown")
                and self.options.get("tray_available")):
            self._prompt_close_preference()
            return
        # Otherwise the main process owns hide-vs-quit (it knows the tray state
        # and the persisted close_to_tray preference).
        self.r_queue.put(("window_closed", None))

    def _prompt_close_preference(self):
        from .views import ClosePreferenceDialog
        if getattr(self, "_close_dialog", None) is not None:
            return
        self._close_dialog = ClosePreferenceDialog(self, self._resolve_close)

    def _resolve_close(self, minimize):
        """Called with the user's first-close choice: persist it and act."""
        self._close_dialog = None
        self.settings_values["close_to_tray"] = bool(minimize)
        self.settings_values["close_prompt_shown"] = True
        self.save_settings({"close_to_tray": bool(minimize),
                            "close_prompt_shown": True})
        self.r_queue.put(("window_closed", {"minimize": bool(minimize)}))

    def _shutdown(self):
        if self._closing:
            return
        self._closing = True
        try:
            self.thumbs.shutdown()
        except Exception:
            pass
        try:
            self.source.stop()
        except Exception:
            pass
        self._api_pool.shutdown(wait=False)
        self.clean_exit = True
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self.root.mainloop()


def run_browser(cmd_queue, r_queue, servers, options):
    """Process entry point. Returns when the window is closed."""
    app = None
    try:
        app = BrowserApp(cmd_queue, r_queue, servers, options)
        app.run()
    except Exception:
        log.error("Library browser crashed", exc_info=True)
    finally:
        if app is None or not app.clean_exit:
            try:
                # Echo the launch epoch so the main process can tell this
                # death notice apart from one belonging to a replacement
                # browser it already started.
                r_queue.put(("browser_died",
                             {"epoch": (options or {}).get("epoch")}))
            except Exception:
                pass
