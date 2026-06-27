from PIL import Image
from collections import deque
import subprocess
from multiprocessing import Process, Queue
import threading
import sys
import typing
import logging

from .constants import USER_APP_NAME, APP_NAME
from .conffile import confdir
from .clients import clientManager
from .conf import settings, Settings
from .sync.manager import syncManager
from .utils import get_resource
from .log_utils import CustomFormatter, root_logger
from .i18n import _

log = logging.getLogger("gui_mgr")


def _classify_setting(ann):
    """Map a Settings annotation to a simple widget type for the config UI."""
    if ann is bool:
        return "bool"
    if ann is int:
        return "int"
    if ann is float:
        return "float"
    if ann is str:
        return "str"
    if typing.get_origin(ann) is typing.Union:
        non_none = [a for a in typing.get_args(ann) if a is not type(None)]
        if len(non_none) == 1:
            return _classify_setting(non_none[0])
    return "skip"  # lists / language_config etc. — not editable in the form


def _settings_schema():
    return {key: _classify_setting(ann)
            for key, ann in Settings.__annotations__.items()}

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

# Setup a log handler for log items. The library browser's log viewer renders
# these (forwarded over the IPC queue); log_cache seeds it on open.
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


# Why are the tray and the browser window separate processes (from each other
# and from the player)? Because pystray and Tkinter each require the main thread
# of their process, and historically pystray + libmpv in one process segfaults
# with GNOME AppIndicator. UserInterface (this thread, in the main process) owns
# both child processes and brokers messages between them and the player.


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
        guiHandler.callback = None

        if self.browser_process is not None:
            self._send_browser(("die", None))
            self.browser_process.join(timeout=3)
            if self.browser_process.is_alive():
                self.browser_process.terminate()

        if self.tray_process is not None:
            self.tray_process.terminate()
        self.dead = True

    # -- startup -----------------------------------------------------------

    def login_servers(self):
        # Non-blocking: the browser shows its own login screen when no servers
        # are connected (the old blocking PreferencesWindow is gone).
        clientManager.try_connect()
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
        # Forward live log lines to the browser's log viewer.
        guiHandler.callback = self._forward_log
        # Push download catalog changes / progress to the browser.
        syncManager.on_change = self._push_sync_state
        syncManager.on_progress = self._push_download_progress

    def refresh_servers(self):
        if self.browser_process is not None and self.browser_process.is_alive():
            self._send_browser(("servers", {
                "connected": self._collect_servers(),
                "all": self._collect_credentials(),
            }))

    def _collect_servers(self):
        """Connected servers with tokens — what the browser browses with."""
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
    def _collect_credentials():
        """All saved servers with status — what the Servers screen manages."""
        return [{
            "uuid": cred.get("uuid"),
            "name": cred.get("Name") or cred.get("address"),
            "username": cred.get("username", ""),
            "connected": bool(cred.get("connected")),
        } for cred in clientManager.credentials]

    def _browser_options(self, start_hidden):
        return {
            "page_size": settings.library_page_size,
            "image_width": settings.library_image_width,
            "image_cache_mb": settings.library_image_cache_mb,
            "device_id": settings.client_uuid,
            "player_name": settings.player_name,
            "verify_ssl": not settings.ignore_ssl_cert,
            "start_hidden": start_hidden,
            "last_server": settings.library_last_server,
            "server_list": self._collect_credentials(),
            "settings": settings.dict(),
            "settings_schema": _settings_schema(),
            "sync_state": syncManager.state(),
            "catalog_path": syncManager.db.path if syncManager.db else None,
        }

    def _send_browser(self, message):
        if self.browser_cmd_queue is not None:
            try:
                self.browser_cmd_queue.put(message)
            except Exception:
                log.debug("Failed to message browser process", exc_info=True)

    def _forward_log(self, line):
        # Called from arbitrary logging threads — must not log (would recurse).
        q = self.browser_cmd_queue
        if q is not None:
            try:
                q.put(("log_line", line))
            except Exception:
                pass

    def _push_sync_state(self):
        self._send_browser(("sync_state", syncManager.state()))

    def _push_download_progress(self, item_id, name, downloaded, total):
        self._send_browser(("download_progress", {
            "item_id": item_id, "name": name,
            "downloaded": downloaded, "total": total}))

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

    def on_add_server(self, payload):
        payload = payload or {}
        server = payload.get("server", "")
        username = payload.get("username", "")
        password = payload.get("password", "")

        # Logging in hits the network; run it off the event loop so the UI stays
        # responsive and other actions keep flowing.
        def work():
            ok = False
            try:
                ok = clientManager.login(server, username, password)
            except Exception:
                log.error("Error while adding server.", exc_info=True)
            self._send_browser(("server_result", {
                "ok": ok,
                "error": None if ok else _(
                    "Could not connect. Please check your details."),
            }))
            if ok:
                self.refresh_servers()

        threading.Thread(target=work, daemon=True).start()

    def on_remove_server(self, uuid):
        if not uuid:
            return
        try:
            clientManager.remove_client(uuid)
        except Exception:
            log.error("Error while removing server.", exc_info=True)
        self.refresh_servers()

    def on_request_logs(self, _param):
        self._send_browser(("log_init", list(log_cache)))

    def on_save_settings(self, changes):
        if isinstance(changes, dict):
            try:
                data = settings.dict()
                data.update(changes)
                safe = settings.parse_obj(data)
                for key in changes:
                    # Only apply values that coerced cleanly; parse_obj falls
                    # back to the default for bad input, which we must not write.
                    if key in safe.__fields_set__:
                        setattr(settings, key, getattr(safe, key))
                settings.save()
            except Exception:
                log.error("Failed to save settings", exc_info=True)
        self._send_browser(("settings_data", settings.dict()))

    def on_estimate_download(self, payload):
        payload = payload or {}

        def work():
            try:
                est = syncManager.estimate(payload.get("server_uuid"),
                                           payload.get("item_id"),
                                           payload.get("item_type"))
            except Exception:
                log.error("Download estimate failed", exc_info=True)
                est = {"count": 0, "total_bytes": 0, "watched_count": 0}
            est["item_id"] = payload.get("item_id")
            self._send_browser(("download_estimate", est))

        threading.Thread(target=work, daemon=True).start()

    def on_download(self, payload):
        payload = payload or {}

        def work():
            try:
                syncManager.enqueue(payload.get("server_uuid"),
                                    payload.get("item_id"),
                                    payload.get("item_type"),
                                    payload.get("include_watched", False))
            except Exception:
                log.error("Enqueue download failed", exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def on_delete_download(self, payload):
        payload = payload or {}

        def work():
            try:
                syncManager.delete(
                    item_id=payload.get("item_id"),
                    series_id=payload.get("series_id"),
                    season_id=payload.get("season_id"),
                    watched_only=payload.get("watched_only", False))
            except Exception:
                log.error("Delete download failed", exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def on_show_preferences(self, _param):
        # Tray "Configure Servers" → open the browser on the Servers tab.
        self._send_browser(("show", None))
        self._send_browser(("navigate", {"kind": "settings", "tab": "servers"}))

    def on_show_console(self, _param):
        # Tray "Show Console" → open the browser on the Logs tab.
        self._send_browser(("show", None))
        self._send_browser(("navigate", {"kind": "settings", "tab": "logs"}))

    def on_open_player_menu(self, _param):
        self.open_player_menu()

    def on_open_config(self, _param):
        self.open_config_brs()

    # -- helpers -----------------------------------------------------------

    def _mark_gui_ready(self):
        if self.gui_ready and not self.gui_ready.is_set():
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
