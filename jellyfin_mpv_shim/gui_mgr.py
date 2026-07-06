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
from .clients import clientManager, QuickConnectError
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
        self.browser_ready = False
        self._connecting = False
        self._connect_thread = None
        self._shutting_down = False
        self._quick_connect_cancel = None  # threading.Event for the active QC flow

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

    def activate(self):
        """Surface the window — called when a second launch is blocked. Safe to
        call from any thread (just enqueues onto the UI action queue)."""
        if self.r_queue is not None:
            self.r_queue.put(("show", None))

    def _detach_browser(self):
        """Stop forwarding logs / sync updates into the browser's command queue.

        Called on any browser death. This matters for two reasons: (a) pushes
        must not pile up forever in a queue nobody is draining after a crash;
        and (b) these callbacks are set *after* the process forks, so a later
        relaunch must find them cleared — otherwise the forked child inherits a
        live log callback pointed at its own cmd queue and echoes its own logs
        back into it. start_browser() re-establishes them per launch."""
        guiHandler.callback = None
        syncManager.on_change = lambda: None
        syncManager.on_progress = lambda item_id, name, downloaded, total: None

    def _die(self):
        self._shutting_down = True
        # Stop pushing into queues we're about to tear down (but keep the queue
        # reference so we can still deliver the "die" message below).
        self._detach_browser()

        if self.browser_process is not None:
            self._send_browser(("die", None))
            self.browser_process.join(timeout=3)
            if self.browser_process.is_alive():
                self.browser_process.terminate()
                self.browser_process.join(timeout=1)

        if self.tray_process is not None:
            self.tray_process.terminate()
            self.tray_process.join(timeout=1)
        self.dead = True

    # -- startup -----------------------------------------------------------

    def login_servers(self):
        # Load saved creds synchronously (fast, no network) so the window opens
        # knowing which servers exist, then connect in the background — the
        # browser appears immediately in a "connecting" state instead of
        # blocking on the network. The browser shows its own login screen when
        # no servers connect (the old blocking PreferencesWindow is gone).
        clientManager.load_credentials()
        # Refresh the browser's servers list when a background status change
        # lands (e.g. the cast-session verifier confirms or gives up). This is
        # a status-only push — it must not rebuild the live browse source.
        clientManager.on_servers_changed = self._push_server_status
        if not settings.work_offline:
            self._connecting = True
        self.start_browser()
        if not settings.work_offline:
            self._begin_connect()

    def _begin_connect(self):
        """Kick off a connection attempt in the background (idempotent)."""
        if self._connect_thread is not None and self._connect_thread.is_alive():
            return
        self._connecting = True
        self._connect_thread = threading.Thread(target=self._connect_worker,
                                                 daemon=True)
        self._connect_thread.start()

    def _connect_worker(self):
        try:
            clientManager.connect_all()
        except Exception:
            log.error("Connection attempt failed", exc_info=True)
        finally:
            self._connecting = False
            self._push_connection_settled()

    def _push_connection_settled(self):
        self._send_browser(("connection_settled", {
            "connected": self._collect_servers(),
            "all": self._collect_credentials(),
        }))

    def on_retry_connect(self, _param):
        self._begin_connect()

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

        self.browser_ready = False
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

    def _push_server_status(self):
        """Status-only update (e.g. cast-session badge): refresh the servers
        metadata without touching the live browse connections."""
        self._send_browser(("server_status", self._collect_credentials()))

    def _collect_servers(self):
        """Connected servers with tokens — what the browser browses with."""
        name_by_uuid = {
            cred.get("uuid"): cred.get("Name") or cred.get("address")
            for cred in list(clientManager.credentials)
        }
        servers = []
        # Snapshot: the health-check thread can mutate clients mid-iteration.
        for uuid, client in list(clientManager.clients.items()):
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
            # Unset (e.g. reconnects without a verifier) counts as ready; only
            # an explicit False — a connect still verifying or one that failed
            # to register a cast session — shows the degraded state.
            "casting": cred.get("cast_ready", True),
        } for cred in list(clientManager.credentials)]

    def _browser_options(self, start_hidden):
        return {
            "page_size": settings.library_page_size,
            "image_width": settings.library_image_width,
            "image_cache_mb": settings.library_image_cache_mb,
            "device_id": settings.client_uuid,
            "player_name": settings.player_name,
            "verify_ssl": not settings.ignore_ssl_cert,
            "start_hidden": start_hidden,
            "connecting": self._connecting,
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
        self.browser_ready = True
        self._mark_gui_ready()

    def on_show(self, _param):
        # If the window process has gone (crash, or a failed first start), bring
        # it back rather than messaging a dead queue.
        if self.browser_process is None or not self.browser_process.is_alive():
            self.start_browser()
        else:
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
        was_ready = self.browser_ready
        self.browser_ready = False
        # Let the dead process be reaped and allow a later relaunch via on_show.
        if self.browser_process is not None:
            self.browser_process.join(timeout=0)
            self.browser_process = None
        # Drop the dead child's queue and stop forwarding into it. Without this,
        # log/sync pushes accumulate in an undrained queue, and a forked
        # relaunch would inherit a live log callback aimed at the new queue and
        # echo its own logs into it. start_browser() re-establishes both.
        self._detach_browser()
        self.browser_cmd_queue = None
        if not was_ready:
            # Never came up (e.g. no display / Tk init failure). Don't take the
            # whole app down — keep running as a cast target; the tray (if any)
            # can relaunch the window once the display is back.
            log.warning("Library browser failed to start; continuing without a "
                        "window. The app still works as a cast target%s.",
                        " (use the tray to retry)" if self.tray_alive else "")
            self._mark_gui_ready()
            return
        if self.tray_alive:
            log.warning("Library browser exited unexpectedly; minimized to tray. "
                        "Use the tray to reopen it.")
            return
        log.warning("Library browser exited unexpectedly; shutting down.")
        self.r_queue.put(("quit", None))

    def on_play(self, payload):
        from .event_handler import start_playback

        payload = payload or {}
        item_ids = payload.get("item_ids") or []
        client = clientManager.clients.get(payload.get("server_uuid"))
        if client is None:
            # Offline: play locally if the first item is downloaded.
            if not (item_ids and syncManager.db
                    and syncManager.db.is_complete(item_ids[0])):
                log.warning("Play requested for a disconnected server with no "
                            "local copy.")
                return
        try:
            start_playback(
                client,
                item_ids,
                start_index=payload.get("start_index", 0),
                offset_ticks=payload.get("offset_ticks"),
                aid=payload.get("audio_index"),
                sid=payload.get("subtitle_index"),
                srcid=payload.get("media_source_id"),
                # The browser's pickers already reflect language_config; the
                # user's pick is final and must not be re-derived downstream.
                explicit_tracks=True,
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

    def on_quick_connect(self, payload):
        """Start a Quick Connect login for SSO/passwordless users.

        Initiate the request, push the user-facing code to the browser, then
        poll until the user authorizes it from another Jellyfin session. The
        final outcome reuses the same ``server_result`` channel as a password
        login so the browser re-navigates to Home on success.
        """
        server = (payload or {}).get("server", "")
        cancel = threading.Event()
        self._quick_connect_cancel = cancel

        def work():
            try:
                client, secret, code = clientManager.quick_connect_initiate(server)
            except QuickConnectError as e:
                self._send_browser(("server_result", {"ok": False, "error": str(e)}))
                return
            except Exception:
                log.error("Error starting Quick Connect.", exc_info=True)
                self._send_browser(("server_result", {
                    "ok": False,
                    "error": _("Could not start Quick Connect."),
                }))
                return

            self._send_browser(("quick_connect_code", {"code": code}))
            ok = False
            try:
                ok = clientManager.quick_connect_wait(
                    client, secret, should_cancel=cancel.is_set)
            except Exception:
                log.error("Error during Quick Connect.", exc_info=True)
            if cancel.is_set():
                return  # user cancelled; the form already reset itself
            self._send_browser(("server_result", {
                "ok": ok,
                "error": None if ok else _(
                    "Quick Connect was not authorized in time. Please try again."),
            }))
            if ok:
                self.refresh_servers()

        threading.Thread(target=work, daemon=True).start()

    def on_quick_connect_cancel(self, _param):
        if self._quick_connect_cancel is not None:
            self._quick_connect_cancel.set()

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
                self._materialize_language_preset(changes)
                settings.save()
            except Exception:
                log.error("Failed to save settings", exc_info=True)
        self._send_browser(("settings_data", settings.dict()))

    @staticmethod
    def _materialize_language_preset(changes):
        """The language dropdown writes language_config rules (README-style):
        a preset generates rules, Unset clears them, Custom leaves them alone."""
        if "language_preference" not in changes and "preferred_language" not in changes:
            return
        from .language_config import preset_rules, parse_language_config
        pref = settings.language_preference
        if pref == "custom":
            return
        if pref == "unset":
            settings.language_config = None
            return
        settings.language_config = parse_language_config(
            preset_rules(pref, settings.preferred_language))

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

    def on_set_watched(self, payload):
        payload = payload or {}
        item_id = payload.get("item_id")
        if not item_id:
            return
        watched = bool(payload.get("watched"))
        server_uuid = payload.get("server_uuid")
        refresh = payload.get("refresh", False)
        client = clientManager.clients.get(server_uuid)

        def work():
            ok = False
            if client is not None:
                try:
                    # Jellyfin cascades a played/unplayed mark on a series or
                    # season to all its episodes, so one call covers every type.
                    client.jellyfin.item_played(item_id, watched)
                    ok = True
                except Exception:
                    log.error("Failed to set watched=%s for %s", watched, item_id,
                              exc_info=True)
            elif watched and syncManager.db and syncManager.db.is_complete(item_id):
                # Fully offline: queue the watched mark (advance-only sync).
                # Marking unwatched offline isn't representable, so it's skipped.
                try:
                    syncManager.db.upsert_playstate(server_uuid, item_id,
                                                    played=True)
                    ok = True
                except Exception:
                    log.error("Failed to queue offline watched mark for %s",
                              item_id, exc_info=True)
            else:
                log.warning("Cannot change watched state for %s while offline.",
                            item_id)
            if ok and refresh:
                self._send_browser(("watched_changed", None))

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
