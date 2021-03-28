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
from .utils import get_resource
from .log_utils import CustomFormatter
from .i18n import _

log = logging.getLogger("gui_mgr")

# From https://stackoverflow.com/questions/6631299/
# This is for opening the config directory.


def _show_file_darwin(path: str):
    subprocess.Popen(["open", path])


def _show_file_linux(path: str):
    subprocess.Popen(["xdg-open", path])


def _show_file_win32(path: str):
    subprocess.Popen(["explorer", path])


_show_file_func = {
    "darwin": _show_file_darwin,
    "linux": _show_file_linux,
    "win32": _show_file_win32,
    "cygwin": _show_file_win32,
}

try:
    show_file = _show_file_func[sys.platform]

    def open_config():
        show_file(confdir(APP_NAME))


except KeyError:
    open_config = None
    log.warning("Platform does not support opening folders.")

# Setup a log handler for log items.
log_cache = deque([], 1000)
root_logger = logging.getLogger("")


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
# I suppose this means I can put the Tkinter GUI back
# into the main process. This is true, but then the
# two need to be merged, which is non-trivial.


class UserInterface(threading.Thread):
    def __init__(self):
        self.dead = False
        self.open_player_menu = lambda: None
        self.icon_stop = lambda: None
        self.log_window = None
        self.preferences_window = None
        self.stop_callback = None
        self.gui_ready = None
        self.r_queue = None
        self.process = None

        threading.Thread.__init__(self)

    def run(self):
        self.r_queue = Queue()
        self.process = STrayProcess(self.r_queue)
        self.process.start()

        while True:
            action, param = self.r_queue.get()
            if hasattr(self, action):
                getattr(self, action)()
            elif action == "die":
                self._die()
                if self.stop_callback:
                    self.stop_callback()
                break

    def stop(self):
        self.r_queue.put(("die", None))

    def _die(self):
        self.process.terminate()
        self.dead = True

        if self.log_window and not self.log_window.dead:
            self.log_window.stop()
        if self.preferences_window and not self.preferences_window.dead:
            self.preferences_window.stop()

    def login_servers(self):
        is_logged_in = clientManager.try_connect()
        if not is_logged_in:
            self.show_preferences()
            self.preferences_window.block_until_close()

    def show_console(self):
        if self.log_window is None or self.log_window.dead:
            self.log_window = LoggerWindow()
            self.log_window.start()

    def show_preferences(self):
        if self.preferences_window is None or self.preferences_window.dead:
            self.preferences_window = PreferencesWindow()
            self.preferences_window.start()

    def ready(self):
        if self.gui_ready:
            self.gui_ready.set()

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
        from pystray import Icon, MenuItem, Menu

        def get_wrapper(command):
            def wrapper():
                self.r_queue.put((command, None))

            return wrapper

        def die():
            # We don't call self.icon_stop() because it crashes on Linux now...
            if sys.platform == "linux":
                # This kills the status icon uncleanly.
                self.r_queue.put(("die", None))
            else:
                self.icon_stop()

        menu_items = [
            MenuItem(_("Configure Servers"), get_wrapper("show_preferences")),
            MenuItem(_("Show Console"), get_wrapper("show_console")),
            MenuItem(_("Application Menu"), get_wrapper("open_player_menu")),
            MenuItem(_("Open Config Folder"), get_wrapper("open_config_brs")),
            MenuItem(_("Quit"), die),
        ]

        icon = Icon(USER_APP_NAME, menu=Menu(*menu_items))
        icon.icon = Image.open(get_resource("systray.png"))
        self.icon_stop = icon.stop

        def setup(icon: Icon):
            icon.visible = True
            self.r_queue.put(("ready", None))

        icon.run(setup=setup)
        self.r_queue.put(("die", None))


user_interface = UserInterface()
