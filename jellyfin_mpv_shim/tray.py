"""System tray icon (pystray / AppIndicator), decoupled from the Tk GUI.

The tray used to live alongside the old Tk browser process; the in-window
browser has no such process, so this stands alone (and must — it pulls
in Tk), so the tray lives here and either UI can own one.

**It runs in a separate PROCESS, not a thread.** pystray needs its own
process's main thread for its GTK/AppIndicator loop, and historically
pystray + libmpv in one process segfaults with GNOME AppIndicator — that is
the whole reason the original was a ``Process``. What lives in *this*
process is a small pump thread reading the child's command queue and
dispatching to callbacks.

Per the optional-dependency policy: a missing or broken pystray logs a
warning and leaves the app running headless-but-functional.
"""

import logging
import multiprocessing
import os
import sys
import threading
from multiprocessing import Process, Queue

from .constants import APP_NAME, USER_APP_NAME
from .i18n import _
from .utils import get_resource

log = logging.getLogger("tray")


class TrayProcess(Process):
    """The pystray loop. Everything it can do is "put a command name on the
    queue" — it holds no references to the player or the browser, because
    with the 'spawn' start method it is a fresh interpreter anyway."""

    def __init__(self, r_queue: "Queue"):
        self.r_queue = r_queue
        self.icon_stop = None
        Process.__init__(self, daemon=True, name="jellyfin-mpv-shim-tray")

    def run(self):
        # Force the X11 GTK backend to dodge Wayland startup issues. These
        # variables only mean anything to GTK on Linux/BSD; pystray uses
        # native APIs on Windows and macOS, so leave the env alone there.
        if sys.platform.startswith("linux") or sys.platform.startswith("freebsd"):
            os.environ.pop("WAYLAND_DISPLAY", None)
            os.environ["GDK_BACKEND"] = "x11"

        # Spawned child: it never ran main(), so gettext is unconfigured and
        # the menu would come out untranslated.
        try:
            from . import i18n

            i18n.configure()
        except Exception:
            log.debug("tray i18n setup failed", exc_info=True)

        try:
            from PIL import Image
            from pystray import Icon, Menu, MenuItem
        except Exception as e:
            log.error("Failed to import pystray: %s", e)
            self.r_queue.put(("tray_died", None))
            return

        def send(command):
            def wrapper():
                self.r_queue.put((command, None))

            return wrapper

        def die():
            # icon.stop() crashes on Linux, so let the parent tear us down.
            if sys.platform == "linux":
                self.r_queue.put(("quit", None))
            else:
                self.icon_stop()

        menu_items = [
            MenuItem(_("Show Library Browser"), send("show")),
            MenuItem(_("Configure Servers"), send("show_preferences")),
            MenuItem(_("Show Console"), send("show_console")),
            MenuItem(_("Application Menu"), send("open_player_menu")),
            MenuItem(_("Open Config Folder"), send("open_config")),
            MenuItem(_("Quit"), die),
        ]

        icon = Icon(APP_NAME, title=USER_APP_NAME, menu=Menu(*menu_items))
        try:
            icon.icon = Image.open(get_resource("systray.png"))
        except Exception:
            log.debug("tray icon image missing", exc_info=True)
        self.icon_stop = icon.stop

        def setup(tray_icon):
            tray_icon.visible = True
            self.r_queue.put(("ready", None))

        try:
            icon.run(setup=setup)
        except Exception:
            log.error("System tray failed to start.", exc_info=True)
            self.r_queue.put(("tray_died", None))
            return
        # icon.run only returns on a clean stop (Quit on Windows/macOS).
        self.r_queue.put(("quit", None))


class TrayManager:
    """Owns the tray process and pumps its commands to ``handlers``.

    ``handlers`` maps the command names the child emits ("show",
    "show_preferences", "show_console", "open_player_menu", "open_config",
    "quit") to callables. Unknown commands are ignored, so the child and the
    parent can disagree about the menu without crashing either.
    """

    def __init__(self, handlers=None):
        self.handlers = dict(handlers or {})
        self.ready = threading.Event()
        self.available = False
        self._queue = None
        self._process = None
        self._thread = None
        self._halt = threading.Event()

    def start(self):
        try:
            self._queue = multiprocessing.Queue()
            self._process = TrayProcess(self._queue)
            self._process.start()
        except Exception:
            log.warning("Could not start the system tray.", exc_info=True)
            self._process = None
            return False
        self._thread = threading.Thread(target=self._pump, daemon=True,
                                        name="tray-pump")
        self._thread.start()
        return True

    def _pump(self):
        while not self._halt.is_set():
            try:
                command, _param = self._queue.get(timeout=0.5)
            except Exception:
                continue  # Empty, or the queue died with the child
            self.dispatch(command)

    def dispatch(self, command):
        """Apply one command from the tray child. Never raises: a broken
        handler must not take the pump (and with it the whole tray) down."""
        if command == "ready":
            self.available = True
            self.ready.set()
            log.info("System tray is up.")
            return
        if command == "tray_died":
            self.available = False
            self.ready.set()   # unblock anyone waiting, don't hang
            log.warning("System tray is unavailable "
                        "(missing pystray/AppIndicator).")
            return
        handler = self.handlers.get(command)
        if handler is None:
            log.debug("tray: no handler for %r", command)
            return
        try:
            handler()
        except Exception:
            log.error("tray handler %r failed", command, exc_info=True)

    def stop(self):
        self._halt.set()
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                log.debug("tray terminate failed", exc_info=True)
            self._process = None
