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
from .repository import LibrarySource, PLAYABLE_TYPES, SERIES_TYPES, FOLDER_TYPES
from .thumbnails import ThumbnailStore
from .views import VIEW_TYPES
from .theme import apply_dark_theme, WINDOW_BG, CARD_BG, SUBTLE_FG

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

        self.source = LibrarySource(
            servers, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), verify_ssl)
        self.current_server = self._initial_server()

        self._build_chrome()
        self._refresh_server_switcher()
        self._show_initial()

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
        self.content = tk.Frame(self.root, bg=CARD_BG)
        self.content.pack(fill="both", expand=True)

    def _show_initial(self):
        if self.current_server:
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

    def add_server(self, payload):
        self.r_queue.put(("add_server", payload))

    def remove_server(self, uuid):
        if uuid:
            self.r_queue.put(("remove_server", uuid))

    def request_logs(self):
        self.r_queue.put(("request_logs", None))

    def save_settings(self, changes):
        self.r_queue.put(("save_settings", changes))

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

        while True:
            try:
                cmd, param = self.cmd_queue.get_nowait()
            except queue.Empty:
                break
            self._handle_cmd(cmd, param)

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
        elif cmd == "server_result":
            self._dispatch_view("on_server_result", param or {})
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
                self.settings_values = param
            self._dispatch_view("on_settings_data", param)
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
        try:
            self.source.stop()
        except Exception:
            pass
        verify_ssl = self.options.get("verify_ssl", True)
        self.source = LibrarySource(
            connected, self.options.get("device_id", ""),
            self.options.get("player_name", "mpv-shim"), verify_ssl)
        self.home_cache = {}
        server_list = self.source.servers()
        if self.current_server not in {s["uuid"] for s in server_list}:
            self.current_server = server_list[0]["uuid"] if server_list else None
        self._refresh_server_switcher()

        kind = self.nav_stack[-1]["kind"] if self.nav_stack else None
        if kind == "settings":
            self._dispatch_view("on_servers_changed", self.server_list)
        elif self.current_server:
            if kind in (None, "login"):
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
