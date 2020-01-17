from pystray import Icon, MenuItem, Menu
from PIL import Image
from collections import deque
import tkinter as tk
from tkinter import ttk
import subprocess
from multiprocessing import Process, Queue
import threading
import sys
import logging
import queue
import os.path

from .constants import USER_APP_NAME, APP_NAME
from .conffile import confdir
from .clients import clientManager


icon_file = os.path.join(os.path.dirname(__file__), "systray.png")
log = logging.getLogger('gui_mgr')

# From https://stackoverflow.com/questions/6631299/
# This is for opening the config directory.
def _show_file_darwin(path):
    subprocess.check_call(["open", path])

def _show_file_linux(path):
    subprocess.check_call(["xdg-open", path])

def _show_file_win32(path):
    subprocess.check_call(["explorer", "/select", path])

_show_file_func = {'darwin': _show_file_darwin, 
                   'linux': _show_file_linux,
                   'win32': _show_file_win32,
                   'cygwin': _show_file_win32}

try:
    show_file = _show_file_func[sys.platform]
    def open_config():
        show_file(confdir(APP_NAME))
except KeyError:
    open_config = None
    log.warning("Platform does not support opening folders.")

# Setup a log handler for log items.
log_cache = deque([], 1000)
root_logger = logging.getLogger('')

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
guiHandler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)8s] %(message)s"))
root_logger.addHandler(guiHandler)

# Why am I using another process for the GUI windows?
# Because both pystray and tkinter must run
# in the main thread of their respective process.

class LoggerWindow(threading.Thread):
    def __init__(self):
        self.dead = False
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
    
    def handle(self, action, params):
        self.queue.put((action, params))

    def stop(self, is_source=False):
        self.r_queue.put(("die", None))
    
    def _die(self):
        guiHandler.callback = None
        self.handle("die", None)
        self.process.terminate()
        self.dead = True

class LoggerWindowProcess(Process):
    def __init__(self, queue, r_queue):
        self.queue = queue
        self.r_queue = r_queue
        Process.__init__(self)

    def update(self):
        try:
            self.text.config(state=tk.NORMAL)
            while True:
                action, param = self.queue.get_nowait()
                if action == "append":
                    self.text.config(state=tk.NORMAL)
                    self.text.insert(tk.END, "\n")
                    self.text.insert(tk.END, param)
                    self.text.config(state=tk.DISABLED)
                    self.text.see(tk.END)
                elif action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.text.after(100, self.update)

    def run(self):
        root = tk.Tk()
        self.root = root
        root.title("Application Log")
        text = tk.Text(root)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand = tk.YES)
        text.config(wrap=tk.WORD)
        self.text = text
        yscroll = tk.Scrollbar(command=text.yview)
        text['yscrollcommand'] = yscroll.set
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.config(state=tk.DISABLED)
        self.update()
        root.mainloop()
        self.r_queue.put(("die", None))

class PreferencesWindow(threading.Thread):
    def __init__(self):
        self.dead = False
        threading.Thread.__init__(self)

    def run(self):
        self.queue = Queue()
        self.r_queue = Queue()
        self.process = PreferencesWindowProcess(self.queue, self.r_queue)
        self.process.start()
        while True:
            action, param = self.r_queue.get()
            if action == "die":
                self._die()
                break
    
    def handle(self, action, params):
        self.queue.put((action, params))

    def stop(self, is_source=False):
        self.r_queue.put(("die", None))
    
    def _die(self):
        self.handle("die", None)
        self.process.terminate()
        self.dead = True

class PreferencesWindowProcess(Process):
    def __init__(self, queue, r_queue):
        self.queue = queue
        self.r_queue = r_queue
        Process.__init__(self)

    def update(self):
        try:
            self.text.config(state=tk.NORMAL)
            while True:
                action, param = self.queue.get_nowait()
                if action == "append":
                    self.text.config(state=tk.NORMAL)
                    self.text.insert(tk.END, "\n")
                    self.text.insert(tk.END, param)
                elif action == "die":
                    self.root.destroy()
                    self.root.quit()
                    return
        except queue.Empty:
            pass
        self.text.config(state=tk.DISABLED)
        self.text.see(tk.END)
        self.text.after(100, self.update)

    def run(self):
        root = tk.Tk()
        self.root = root
        root.title("Server Configuration")
        text = tk.Text(root)
        text.pack(side=tk.LEFT, fill=tk.BOTH, expand = tk.YES)
        text.config(wrap=tk.WORD)
        self.text = text
        yscroll = tk.Scrollbar(command=text.yview)
        text['yscrollcommand'] = yscroll.set
        yscroll.pack(side=tk.RIGHT, fill=tk.Y)
        text.config(state=tk.DISABLED)
        self.update()
        root.mainloop()
        self.r_queue.put(("die", None))

class UserInterface:
    def __init__(self):
        self.open_player_menu = lambda: None
        self.icon_stop = lambda: None
        self.log_window = None
        self.preferences_window = None

    def login_servers(self):
        clientManager.cli_connect()

    def stop(self):
        if self.log_window and not self.log_window.dead:
            self.log_window.stop()
        if self.preferences_window and not self.preferences_window.dead:
            self.preferences_window.stop()
        self.icon_stop()

    def show_console(self):
        if self.log_window is None or self.log_window.dead:
            self.log_window = LoggerWindow()
            self.log_window.start()

    def show_preferences(self):
        if self.preferences_window is None or self.preferences_window.dead:
            self.preferences_window = PreferencesWindow()
            self.preferences_window.start()

    def run(self):
        menu_items = [
            MenuItem("Configure Servers", self.show_preferences),
            MenuItem("Show Console", self.show_console),
            MenuItem("Application Menu", self.open_player_menu),
        ]

        if open_config:
            menu_items.append(MenuItem("Open Config Folder", open_config))

        icon = Icon(USER_APP_NAME, menu=Menu(*menu_items))
        icon.icon = Image.open(icon_file)
        self.icon_stop = icon.stop
        icon.run()

userInterface = UserInterface()
