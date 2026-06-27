from PIL import Image
from collections import deque
import subprocess
from multiprocessing import Process, Queue
import threading
import sys
import logging
import queue

from .constants import USER_APP_NAME, APP_NAME
from .conffile import confdir
from .clients import clientManager
from .conf import settings
from .utils import get_resource
from .log_utils import CustomFormatter, root_logger
from .i18n import _

log = logging.getLogger("gui_mgr")

# From https://stackoverflow.com/questions/6631299/
# This is for opening the config directory.


def _show_file_darwin(path: str):
    subprocess.Popen(["open", path])


def _show_file_xdg(path: str):
    subprocess.Popen(["xdg-open", path])


def _show_file_win32(path: str):
    subprocess.Popen(["explorer", path])


_show_file_func = {
    "darwin": _show_file_darwin,
    "linux": _show_file_xdg,
    "openbsd": _show_file_xdg,
    "win32": _show_file_win32,
    "cygwin": _show_file_win32,
}

try:
    show_file = None
    for platform, func in _show_file_func.items():
        if sys.platform.startswith(platform):
            show_file = func

    def open_config():
        show_file(confdir(APP_NAME))

except KeyError:
    open_config = None
    log.warning("Platform does not support opening folders.")

# Setup a log handler for log items.
log_cache = deque([], 1000)


class GUILogHandler(logging.Handler):
    def __init__(self):
        self.callback = None
        super().__init__()

    def emit(self, record):
        log_entry = self.format(record)
        log_cache.append(log_entry)

        if self.callback:
            try:
                self.callback(log_entry)
            except Exception:
                pass


guiHandler = GUILogHandler()
guiHandler.setFormatter(CustomFormatter())
root_logger.addHandler(guiHandler)

# Why am I using another process for the GUI windows?
# Because both pystray and tkinter must run
# in the main thread of their respective process.


class LoggerWindow(threading.Thread):
    def __init__(self):
        self.dead = False
        self.queue = None
        self.r_queue = None
        self.process = None
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = LoggerWindowProcess(self.queue, self.r_queue)

        def handle(message):
            self.handle("append", message)

        self.process.start()
        handle("\n".join(log_cache))
        guiHandler.callback = handle
        while True:
            action, param = self.r_queue.get()
            if action == "die":
                self._die()
                break

    def handle(self, action: str, params=None):
        self.queue.put((action, params))

    def stop(self):
        self.r_queue.put(("die", None))

    def _die(self):
        guiHandler.callback = None
        self.handle("die")
        self.process.terminate()
        self.dead = True


class LoggerWindowProcess(Process):
    def __init__(self, queue: Queue, r_queue: Queue):
        self.queue = queue
        self.r_queue = r_queue
        self.tk = None
        self.root = None
        self.text = None
        Process.__init__(self)

    def update(self):
        try:
            self.text.config(state=self.tk.NORMAL)
            while True:
                action, param = self.queue.get_nowait()
                if action == "append":
                    self.text.config(state=self.tk.NORMAL)
                    self.text.insert(self.tk.END, "\n")
                    self.text.insert(self.tk.END, param)
                    self.text.config(state=self.tk.DISABLED)
                    self.text.see(self.tk.END)
                elif action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.text.after(100, self.update)

    def run(self):
        import tkinter as tk

        self.tk = tk
        root = tk.Tk()
        self.root = root
        root.title(_("Application Log"))
        try:
            icon_img = tk.PhotoImage(file=get_resource("logo.png"))
            root.iconphoto(True, icon_img)
        except Exception:
            pass
        text = tk.Text(root)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand=tk.YES)
        text.config(wrap=tk.WORD)
        self.text = text
        yscroll = tk.Scrollbar(command=text.yview)
        text["yscrollcommand"] = yscroll.set
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.config(state=tk.DISABLED)
        self.update()
        root.mainloop()
        self.r_queue.put(("die", None))


class PreferencesWindow(threading.Thread):
    def __init__(self):
        self.dead = False
        self.dead_trigger = threading.Event()
        self.queue = None
        self.r_queue = None
        self.process = None
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = PreferencesWindowProcess(self.queue, self.r_queue)
        self.process.start()
        self.handle("upd", clientManager.credentials)
        while True:
            action, param = self.r_queue.get()
            if action == "die":
                self._die()
                break
            elif action == "add":
                try:
                    is_logged_in = clientManager.login(*param)
                    if is_logged_in:
                        self.handle("upd", clientManager.credentials)
                    else:
                        self.handle("error")
                except Exception:
                    log.error("Error while adding server.", exc_info=True)
                    self.handle("error")
            elif action == "remove":
                clientManager.remove_client(param)
                self.handle("upd", clientManager.credentials)

    def handle(self, action: str, params=None):
        self.queue.put((action, params))

    def stop(self):
        self.r_queue.put(("die", None))

    def block_until_close(self):
        self.dead_trigger.wait()

    def _die(self):
        self.dead_trigger.set()
        self.handle("die")
        self.process.terminate()
        self.dead = True


class PreferencesWindowProcess(Process):
    def __init__(self, queue: Queue, r_queue: Queue):
        self.queue = queue
        self.r_queue = r_queue
        self.servers = None
        self.server_ids = None
        self.tk = None
        self.messagebox = None
        self.root = None
        self.serverList = None
        self.current_uuid = None
        self.servername = None
        self.username = None
        self.password = None
        self.add_button = None
        self.remove_button = None
        Process.__init__(self)

    def update(self):
        try:
            while True:
                action, param = self.queue.get_nowait()
                if action == "upd":
                    self.update_servers(param)
                    self.add_button.config(state=self.tk.NORMAL)
                    self.remove_button.config(state=self.tk.NORMAL)
                elif action == "error":
                    self.messagebox.showerror(
                        _("Add Server"),
                        _(
                            "Could not add server.\nPlease check your connection information."
                        ),
                    )
                    self.add_button.config(state=self.tk.NORMAL)
                elif action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.root.after(100, self.update)

    def update_servers(self, server_list):
        self.servers = server_list
        self.server_ids = [x["uuid"] for x in self.servers]
        self.serverList.set(
            [
                "{0} ({1}, {2})".format(
                    server["Name"],
                    server["username"],
                    _("Ok") if server["connected"] else _("Fail"),
                )
                for server in self.servers
            ]
        )

    def run(self):
        import tkinter as tk
        from tkinter import ttk, messagebox

        self.tk = tk
        self.messagebox = messagebox
        root = tk.Tk()
        root.title(_("Server Configuration"))
        try:
            icon_img = tk.PhotoImage(file=get_resource("logo.png"))
            root.iconphoto(True, icon_img)
        except Exception:
            pass
        self.root = root

        self.servers = {}
        self.server_ids = []
        self.serverList = tk.StringVar(value=[])
        self.current_uuid = None

        def server_select(_x):
            idxs = serverlist.curselection()
            if len(idxs) == 1:
                self.current_uuid = self.server_ids[idxs[0]]

        c = ttk.Frame(root, padding=(5, 5, 12, 0))
        c.grid(column=0, row=0, sticky=(tk.N, tk.W, tk.E, tk.S))
        root.grid_columnconfigure(0, weight=1)
        root.grid_rowconfigure(0, weight=1)

        serverlist = tk.Listbox(c, listvariable=self.serverList, height=10, width=40)
        serverlist.grid(column=0, row=0, rowspan=6, sticky=(tk.N, tk.S, tk.E, tk.W))
        c.grid_columnconfigure(0, weight=1)
        c.grid_rowconfigure(4, weight=1)

        servername_label = ttk.Label(c, text=_("Server:"))
        servername_label.grid(column=1, row=0, sticky=tk.E)
        self.servername = tk.StringVar()
        servername_box = ttk.Entry(c, textvariable=self.servername)
        servername_box.grid(column=2, row=0)
        username_label = ttk.Label(c, text=_("Username:"))
        username_label.grid(column=1, row=1, sticky=tk.E)
        self.username = tk.StringVar()
        username_box = ttk.Entry(c, textvariable=self.username)
        username_box.grid(column=2, row=1)
        password_label = ttk.Label(c, text=_("Password:"))
        password_label.grid(column=1, row=2, sticky=tk.E)
        self.password = tk.StringVar()
        password_box = ttk.Entry(c, textvariable=self.password, show="*")
        password_box.grid(column=2, row=2)

        def add_server():
            self.add_button.config(state=tk.DISABLED)
            self.r_queue.put(
                (
                    "add",
                    (self.servername.get(), self.username.get(), self.password.get()),
                )
            )

        def remove_server():
            self.remove_button.config(state=tk.DISABLED)
            self.r_queue.put(("remove", self.current_uuid))

        def close():
            self.r_queue.put(("die", None))

        self.add_button = ttk.Button(c, text=_("Add Server"), command=add_server)
        self.add_button.grid(column=2, row=3, pady=5, sticky=tk.E)
        self.remove_button = ttk.Button(
            c, text=_("Remove Server"), command=remove_server
        )
        self.remove_button.grid(column=1, row=4, padx=5, pady=10, sticky=(tk.E, tk.S))
        close_button = ttk.Button(c, text=_("Close"), command=close)
        close_button.grid(column=2, row=4, pady=10, sticky=(tk.E, tk.S))

        serverlist.bind("<<ListboxSelect>>", server_select)
        self.update()
        root.mainloop()
        self.r_queue.put(("die", None))


# Q: OK. So you put Tkinter in it's own process.
#    Now why is Pystray in another process too?!
# A: Because if I don't, MPV and GNOME Appindicator
#    try to access the same resources and cause the
#    entire application to segfault.
#
# The library browser is yet another Tkinter process for the same reason: the
# tray (pystray) and a Tkinter window cannot share one process, since both want
# the main thread. UserInterface (this thread, in the main process) owns both
# child processes and brokers messages between them and the player.


class BrowserProcess(Process):
    """Hosts the Tkinter library browser window in its own process."""

    def __init__(self, cmd_queue: Queue, r_queue: Queue, servers, options):
        self.cmd_queue = cmd_queue
        self.r_queue = r_queue
        self.servers = servers
        self.options = options
        Process.__init__(self)

    def run(self):
        from .library_browser.app import run_browser

        run_browser(self.cmd_queue, self.r_queue, self.servers, self.options)


class UserInterface(threading.Thread):
    def __init__(self):
        self.dead = False
        self.open_player_menu = lambda: None
        self.log_window = None
        self.preferences_window = None
        self.stop_callback = None
        self.gui_ready = None
        self.r_queue = None

        self.tray_process = None
        self.tray_alive = False
        self._tray_settled = threading.Event()

        self.browser_process = None
        self.browser_cmd_queue = None
        self._shutting_down = False

        threading.Thread.__init__(self)

    def run(self):
        self.r_queue = Queue()
        self.tray_process = STrayProcess(self.r_queue)
        self.tray_process.start()

        while True:
            action, param = self.r_queue.get()
            if action in ("die", "quit"):
                self._die()
                if self.stop_callback:
                    self.stop_callback()
                break
            handler = getattr(self, "on_" + action, None)
            if callable(handler):
                try:
                    handler(param)
                except Exception:
                    log.error("Error handling UI action: %s", action, exc_info=True)
            else:
                log.debug("Unhandled UI action: %s", action)

    def stop(self):
        self.r_queue.put(("die", None))

    def _die(self):
        self._shutting_down = True

        if self.browser_process is not None:
            self._send_browser(("die", None))
            self.browser_process.join(timeout=3)
            if self.browser_process.is_alive():
                self.browser_process.terminate()

        if self.tray_process is not None:
            self.tray_process.terminate()
        self.dead = True

        if self.log_window and not self.log_window.dead:
            self.log_window.stop()
        if self.preferences_window and not self.preferences_window.dead:
            self.preferences_window.stop()

    # -- startup -----------------------------------------------------------

    def login_servers(self):
        is_logged_in = clientManager.try_connect()
        if not is_logged_in:
            self.show_preferences()
            self.preferences_window.block_until_close()
        self.start_browser()

    def start_browser(self):
        if self.browser_process is not None and self.browser_process.is_alive():
            self.refresh_servers()
            return

        # Give the tray a brief moment to report ready/unavailable so the
        # start-minimized decision and close-to-tray policy are correct.
        self._tray_settled.wait(2.0)

        start_hidden = settings.start_minimized and self.tray_alive
        if settings.start_minimized and not self.tray_alive:
            log.warning(
                "start_minimized was requested but no system tray is available; "
                "showing the window instead."
            )

        self.browser_cmd_queue = Queue()
        self.browser_process = BrowserProcess(
            self.browser_cmd_queue,
            self.r_queue,
            self._collect_servers(),
            self._browser_options(start_hidden),
        )
        self.browser_process.start()

    def refresh_servers(self):
        if self.browser_process is not None and self.browser_process.is_alive():
            self._send_browser(("servers", self._collect_servers()))

    def _collect_servers(self):
        name_by_uuid = {
            cred.get("uuid"): cred.get("Name") or cred.get("address")
            for cred in clientManager.credentials
        }
        servers = []
        for uuid, client in clientManager.clients.items():
            cfg = client.config.data
            token = cfg.get("auth.token")
            user_id = cfg.get("auth.user_id")
            address = cfg.get("auth.server")
            if not (token and user_id and address):
                continue
            servers.append({
                "uuid": uuid,
                "name": name_by_uuid.get(uuid) or address,
                "address": address,
                "token": token,
                "user_id": user_id,
            })
        return servers

    @staticmethod
    def _browser_options(start_hidden):
        return {
            "page_size": settings.library_page_size,
            "image_width": settings.library_image_width,
            "image_cache_mb": settings.library_image_cache_mb,
            "device_id": settings.client_uuid,
            "player_name": settings.player_name,
            "verify_ssl": not settings.ignore_ssl_cert,
            "start_hidden": start_hidden,
            "last_server": settings.library_last_server,
        }

    def _send_browser(self, message):
        if self.browser_cmd_queue is not None:
            try:
                self.browser_cmd_queue.put(message)
            except Exception:
                log.debug("Failed to message browser process", exc_info=True)

    # -- action handlers (on_<action>) ------------------------------------

    def on_ready(self, _param):
        self.tray_alive = True
        self._tray_settled.set()
        self._mark_gui_ready()

    def on_tray_died(self, _param):
        log.warning("System tray is unavailable (missing pystray/AppIndicator).")
        self.tray_alive = False
        self._tray_settled.set()
        self._mark_gui_ready()

    def on_ready_browser(self, _param):
        self._mark_gui_ready()

    def on_show(self, _param):
        self._send_browser(("show", None))

    def on_window_closed(self, _param):
        # New paradigm: closing the window minimizes to the tray. With no tray
        # there is nowhere to minimize to, so we exit instead of stranding an
        # invisible process.
        if self.tray_alive:
            self._send_browser(("hide", None))
        else:
            log.info("Window closed and no system tray available; exiting.")
            self.r_queue.put(("quit", None))

    def on_browser_died(self, _param):
        if self._shutting_down:
            return
        log.warning("Library browser exited unexpectedly; shutting down.")
        self.r_queue.put(("quit", None))

    def on_play(self, payload):
        from .event_handler import start_playback

        client = clientManager.clients.get((payload or {}).get("server_uuid"))
        if client is None:
            log.warning("Play requested for an unknown/disconnected server.")
            return
        try:
            start_playback(
                client,
                payload.get("item_ids") or [],
                start_index=payload.get("start_index", 0),
                offset_ticks=payload.get("offset_ticks"),
                aid=payload.get("audio_index"),
                sid=payload.get("subtitle_index"),
                srcid=payload.get("media_source_id"),
            )
        except Exception:
            log.error("Failed to start playback from library browser",
                      exc_info=True)

    def on_set_last_server(self, uuid):
        if settings.library_last_server != uuid:
            settings.library_last_server = uuid
            try:
                settings.save()
            except Exception:
                log.debug("Failed to persist last server", exc_info=True)

    def on_show_preferences(self, _param):
        self.show_preferences()

    def on_show_console(self, _param):
        self.show_console()

    def on_open_player_menu(self, _param):
        self.open_player_menu()

    def on_open_config(self, _param):
        self.open_config_brs()

    # -- helpers -----------------------------------------------------------

    def _mark_gui_ready(self):
        if self.gui_ready and not self.gui_ready.is_set():
            self.gui_ready.set()

    def show_console(self):
        if self.log_window is None or self.log_window.dead:
            self.log_window = LoggerWindow()
            self.log_window.start()

    def show_preferences(self):
        if self.preferences_window is None or self.preferences_window.dead:
            self.preferences_window = PreferencesWindow()
            self.preferences_window.start()

    @staticmethod
    def open_config_brs():
        if open_config:
            open_config()
        else:
            log.error("Config opening is not available.")


class STrayProcess(Process):
    def __init__(self, r_queue: Queue):
        self.r_queue = r_queue
        self.icon_stop = None
        Process.__init__(self)

    def run(self):
        import os
        import sys

        # Force X11 backend for GTK to fix Wayland startup issues. GDK_BACKEND
        # and WAYLAND_DISPLAY only mean anything to GTK on Linux/BSD; on
        # Windows and macOS pystray uses native APIs, so leave the env alone.
        if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
            os.environ.pop("WAYLAND_DISPLAY", None)
            os.environ["GDK_BACKEND"] = "x11"

        try:
            from pystray import Icon, MenuItem, Menu
        except Exception as e:
            log.error(f"Failed to import pystray: {e}")
            self.r_queue.put(("tray_died", None))
            return

        def get_wrapper(command):
            def wrapper():
                self.r_queue.put((command, None))

            return wrapper

        def die():
            # We don't call self.icon_stop() because it crashes on Linux now...
            if sys.platform == "linux":
                # This kills the status icon uncleanly.
                self.r_queue.put(("quit", None))
            else:
                self.icon_stop()

        menu_items = [
            MenuItem(_("Show Library Browser"), get_wrapper("show")),
            MenuItem(_("Configure Servers"), get_wrapper("show_preferences")),
            MenuItem(_("Show Console"), get_wrapper("show_console")),
            MenuItem(_("Application Menu"), get_wrapper("open_player_menu")),
            MenuItem(_("Open Config Folder"), get_wrapper("open_config")),
            MenuItem(_("Quit"), die),
        ]

        icon = Icon(APP_NAME, title=USER_APP_NAME, menu=Menu(*menu_items))
        icon.icon = Image.open(get_resource("systray.png"))
        self.icon_stop = icon.stop

        def setup(icon: Icon):
            icon.visible = True
            self.r_queue.put(("ready", None))

        try:
            icon.run(setup=setup)
        except Exception:
            log.error("System tray failed to start.", exc_info=True)
            self.r_queue.put(("tray_died", None))
            return
        # icon.run only returns on a clean stop (user picked Quit on win/mac).
        self.r_queue.put(("quit", None))


user_interface = UserInterface()
