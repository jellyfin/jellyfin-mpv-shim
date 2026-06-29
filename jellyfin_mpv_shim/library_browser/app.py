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
                    TEXT_FG)

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
        self.thumbs = ThumbnailStore(
            cache_dir, verify_ssl=verify_ssl,
            max_disk_mb=self.options.get("image_cache_mb", 256))
        self._api_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="lib-api")
        self._ui_queue = queue.Queue()
        self.nav_stack = []
        self.current_view = None
        self.home_cache = {}  # server_uuid -> (libraries, rows); stale-while-revalidate
        self.server_list = list(self.options.get("server_list") or [])  # all creds
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

        self._live_servers = servers
        self._verify_ssl = verify_ssl
        self.is_offline = False
        # True while the main process is still attempting to connect; failed is
        # set once an attempt settles with no reachable server.
        self._connecting = bool(self.options.get("connecting"))
        self._connect_failed = False
        self.source = LibrarySource(
            servers, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), verify_ssl)
        self.current_server = self._initial_server()

        self._build_chrome()
        if self.settings_values.get("work_offline"):
            self._enter_offline()
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

        # Server switcher (hidden when only one server).
        self.server_var = tk.StringVar()
        self.server_box = ttk.Combobox(bar, textvariable=self.server_var,
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
                   command=lambda: self.set_offline(False)).pack(
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
        self._show_banner(message or _("Offline — showing downloaded content."))

    def _exit_offline(self):
        self.source = LibrarySource(
            self._live_servers, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), self._verify_ssl)
        self.is_offline = False
        self.current_server = self._initial_server()
        self.home_cache = {}
        self._refresh_server_switcher()
        self._hide_banner()

    def set_offline(self, offline):
        if offline:
            self._enter_offline()
            self.navigate({"kind": "home"}, reset=True)
            return
        # Going online.
        self._exit_offline()
        if self.current_server:
            self.navigate({"kind": "home"}, reset=True)
        elif self.server_list:
            # We have accounts but no live connection yet — (re)connect and wait
            # on the connecting screen instead of flashing the login form.
            self.retry_connect()
        else:
            self.navigate({"kind": "login"}, reset=True)

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
        self._live_servers = connected
        self._connecting = False
        self._connect_failed = bool(self.server_list) and not connected
        if self.is_offline:
            # Browsing downloads by choice/fallback — don't pull the user out;
            # Go Online uses the refreshed connected list when they choose to.
            return
        self._rebuild_live_source(connected)
        self._refresh_server_switcher()
        kind = self.nav_stack[-1]["kind"] if self.nav_stack else None
        if self.current_server:
            if kind in (None, "connecting", "login"):
                self.navigate({"kind": "home"}, reset=True)
        elif kind in (None, "connecting"):
            self._show_disconnected()
        elif kind == "login":
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
        servers = self.source.servers()
        self._server_by_name = {s["name"]: s["uuid"] for s in servers}
        names = [s["name"] for s in servers]
        self.server_box.config(values=names)
        if len(servers) > 1:
            current_name = next((s["name"] for s in servers
                                 if s["uuid"] == self.current_server), names[0])
            self.server_var.set(current_name)
            self.server_box.pack(side="left", padx=8, pady=6)
        else:
            self.server_box.pack_forget()

    def _on_server_change(self, _e):
        name = self.server_var.get()
        uuid = self._server_by_name.get(name)
        if uuid and uuid != self.current_server:
            self.current_server = uuid
            self._persist_server(uuid)
            self.navigate({"kind": "home"}, reset=True)

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

    def _render_top(self):
        route = self.nav_stack[-1]
        # The login screen is full-window chrome-free; everything else shows the
        # top bar.
        if route["kind"] == "login":
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
        if itype in SERIES_TYPES:
            self.navigate({"kind": "series", "series_id": item["Id"], "title": title})
        elif itype in FOLDER_TYPES:
            self.navigate({"kind": "grid", "parent_id": item["Id"], "title": title})
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
                        watched_only=False):
        self.r_queue.put(("delete_download", {
            "item_id": item_id, "series_id": series_id, "season_id": season_id,
            "watched_only": watched_only}))

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
            self.root.deiconify()
            self.root.lift()
            try:
                self.root.focus_force()
            except Exception:
                pass
        elif cmd == "hide":
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
            if param:
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
        elif cmd == "sync_state":
            ss = param or {}
            self.sync_items = set(ss.get("items") or [])
            self.sync_series = set(ss.get("series") or [])
            self.sync_total = ss.get("total_bytes", 0)
            self.sync_active = ss.get("active", 0)
            self.sync_downloading = ss.get("downloading")
            self._dl_percent = None  # new item / state change; await fresh progress
            self._update_statusbar()
            # Refresh views whose download buttons/state changed.
            kind = self.nav_stack[-1]["kind"] if self.nav_stack else None
            if kind in ("detail", "series"):
                self._render_top()
            else:
                self._dispatch_view("on_sync_state", ss)
        elif cmd == "download_estimate":
            if self._download_dialog is not None:
                self._download_dialog.on_estimate(param or {})
        elif cmd == "download_progress":
            payload = param or {}
            total = payload.get("total", 0)
            self._dl_percent = (int(payload.get("downloaded", 0) * 100 / total)
                                if total else None)
            if payload.get("name"):
                self.sync_downloading = payload["name"]
            self._update_statusbar()
            self._dispatch_view("on_download_progress", payload)
        elif cmd == "connection_settled":
            self._on_connection_settled(param or {})
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
        # Don't decide hide-vs-quit here: the main process owns that policy
        # (it knows whether the system tray is available).
        self.r_queue.put(("window_closed", None))

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
                r_queue.put(("browser_died", None))
            except Exception:
                pass
