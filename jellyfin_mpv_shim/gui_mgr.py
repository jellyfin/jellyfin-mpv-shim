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
from .users import userManager
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


def _edit_apis_available():
    """Whether the installed jellyfin-apiclient-python has the playlist /
    collection editing calls (>= 1.15). The browser hides its edit
    affordances when they're absent — graceful degradation, per policy."""
    try:
        from jellyfin_apiclient_python.api import API
    except Exception:
        return False
    return all(hasattr(API, m) for m in (
        "new_playlist", "add_playlist_items", "remove_playlist_items",
        "move_playlist_item", "new_collection", "add_collection_items",
        "remove_collection_items"))

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
        # Incremented per launch; browser_died notices carry it back so a
        # stale death notice can't tear down a relaunched browser.
        self._browser_epoch = 0
        self._connecting = False
        self._connect_thread = None
        self._shutting_down = False
        self._quick_connect_cancel = None  # threading.Event for the active QC flow
        self._folder_move_active = False   # a download-folder move is in flight
        self._startup_locked = False       # active user needs a startup PIN
        self._switching = False            # a user switch is in flight

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
        # Only if a browser was ever launched (which imported .player); avoids
        # importing player at teardown time.
        if getattr(self, "_player_mgr", None) is not None:
            self._player_mgr.on_playstate = None

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
        # A server that (re)connects in the background (health-check retry,
        # websocket reconnect) must become browsable, not just get a status
        # badge: push the full servers payload. The browser keeps the current
        # selection/screen and just gains the server in the switcher.
        clientManager.on_server_connected = self.refresh_servers
        # A locked user that opted into a startup PIN must not connect until the
        # PIN is entered: the browser opens on a locked gate and drives the
        # unlock through on_switch_user. Everything else connects immediately.
        self._startup_locked = userManager.startup_needs_unlock()
        if not settings.work_offline and not self._startup_locked:
            self._connecting = True
        self.start_browser()
        if not settings.work_offline and not self._startup_locked:
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
            "device_id": clientManager.device_id,
        }))

    def _push_users(self):
        self._send_browser(("users", userManager.public_state()))

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
        self._browser_epoch += 1
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
        # Push playback state to the browser's now-playing music bar. Stash the
        # manager so _detach_browser can clear the callback without importing
        # .player at teardown time (that import triggers arg parsing).
        from .player import playerManager
        self._player_mgr = playerManager
        playerManager.on_playstate = self._push_playstate

    def refresh_servers(self):
        if self.browser_process is not None and self.browser_process.is_alive():
            self._send_browser(("servers", {
                "connected": self._collect_servers(),
                "all": self._collect_credentials(),
                "device_id": clientManager.device_id,
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
            # The active user's device id — the browse-only clients must present
            # the same device as the control clients so they share a session.
            "device_id": clientManager.device_id,
            "users": userManager.public_state(),
            "player_name": settings.player_name,
            "verify_ssl": not settings.ignore_ssl_cert,
            "start_hidden": start_hidden,
            "tray_available": self.tray_alive,
            "connecting": self._connecting,
            "last_server": settings.library_last_server,
            "server_list": self._collect_credentials(),
            "settings": settings.dict(),
            "settings_schema": _settings_schema(),
            "sync_state": syncManager.state(),
            "catalog_path": syncManager.db.path if syncManager.db else None,
            # Echoed back in browser_died so stale death notices are ignorable.
            "epoch": self._browser_epoch,
            "edit_apis": _edit_apis_available(),
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

    def _push_playstate(self, state):
        # Called from player/timeline threads; just forwards the compact
        # now-playing snapshot to the browser's music bar.
        self._send_browser(("playstate", state))

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

    def on_window_closed(self, param):
        # The browser may send an explicit choice from the first-close prompt;
        # otherwise fall back to the persisted close_to_tray preference. Either
        # way, minimizing needs a tray — without one we exit.
        param = param or {}
        minimize = param.get("minimize")
        if minimize is None:
            minimize = settings.close_to_tray
        if minimize and self.tray_alive:
            self._send_browser(("hide", None))
        else:
            if minimize and not self.tray_alive:
                log.info("Minimize-to-tray requested but no system tray is "
                         "available; exiting instead.")
            self.r_queue.put(("quit", None))

    def on_browser_died(self, param):
        if self._shutting_down:
            return
        # The tray's "show" and the dying browser's death notice come from
        # different producer processes on the same queue, so a relaunch can be
        # processed before the death notice of the process it replaced. Acting
        # on that stale notice would null the NEW browser's queue/callbacks and
        # orphan its live window — match the launch epoch and ignore strays.
        epoch = (param or {}).get("epoch")
        if epoch is not None and epoch != self._browser_epoch:
            log.debug("Ignoring stale browser death notice (epoch %s != %s).",
                      epoch, self._browser_epoch)
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
        from .player import playerManager

        payload = payload or {}
        item_ids = payload.get("item_ids") or []
        # Fast path: clicking another track in the queue that's already playing
        # just seeks within it — no rebuild, no new play session, no reload of
        # the whole list. Skipped when a resume offset is requested (let the
        # normal path honor it).
        if (not payload.get("offset_ticks") and item_ids
                and playerManager.try_skip_within_queue(
                    item_ids, payload.get("start_index", 0))):
            return
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
                # The browser's pickers already reflect language_config, so a
                # carried pick is final and must not be re-derived downstream.
                # Playback started without pickers (playlists, shuffle, queue)
                # sends both indexes as None — those items still need the
                # language-config / server-default track selection, so they
                # must NOT be marked explicit ("no subtitles" from a picker
                # arrives as -1, not None).
                explicit_tracks=(payload.get("audio_index") is not None
                                 or payload.get("subtitle_index") is not None),
            )
        except Exception:
            log.error("Failed to start playback from library browser",
                      exc_info=True)

    # -- now-playing music bar controls (from the browser) ----------------

    @staticmethod
    def _player_action(fn):
        """Run a player control off the browser, swallowing errors so a bad
        message can't take down the UI thread."""
        try:
            from .player import playerManager
            fn(playerManager)
        except Exception:
            log.error("Music-bar control failed", exc_info=True)

    def on_playpause(self, _param):
        self._player_action(lambda pm: pm.toggle_pause())

    def on_stop_playback(self, _param):
        self._player_action(lambda pm: pm.stop_and_close())

    def on_play_next(self, _param):
        self._player_action(lambda pm: pm.play_next())

    def on_play_prev(self, _param):
        self._player_action(lambda pm: pm.play_prev())

    def on_seek_to(self, pos):
        self._player_action(lambda pm: pm.seek(float(pos), absolute=True))

    def on_set_volume(self, pct):
        self._player_action(lambda pm: pm.set_volume(float(pct)))

    def on_set_repeat(self, mode):
        self._player_action(lambda pm: pm.set_repeat(mode))

    def on_toggle_favorite(self, _param):
        self._player_action(lambda pm: pm.toggle_current_favorite())

    def on_request_queue(self, _param):
        from .player import playerManager
        data = {"items": [], "current_id": None, "server_uuid": None}
        try:
            data = playerManager.get_queue()
            video = playerManager.get_video()
            if video is not None:
                data["server_uuid"] = next(
                    (u for u, c in clientManager.clients.items()
                     if c is video.client), None)
        except Exception:
            log.error("get_queue failed", exc_info=True)
        self._send_browser(("queue_data", data))

    def on_skip_to(self, payload):
        self._player_action(lambda pm: pm.skip_to((payload or {}).get("id")))

    def on_queue_remove(self, payload):
        self._player_action(
            lambda pm: pm.queue_remove_many(
                (payload or {}).get("playlist_item_ids") or []))
        self.on_request_queue(None)

    def on_queue_reorder(self, payload):
        self._player_action(
            lambda pm: pm.queue_reorder((payload or {}).get("order") or []))
        self.on_request_queue(None)

    def on_queue_to_playlist(self, _param):
        # Resolve the playing queue's ids here (the player lives in this
        # process), then hand them back to the browser to open its picker.
        ids = []
        try:
            from .player import playerManager
            ids = playerManager.get_queue_ids()
        except Exception:
            log.error("get_queue_ids failed", exc_info=True)
        self._send_browser(("open_queue_playlist", {"item_ids": ids}))

    def on_queue_items(self, payload):
        # Append items to the playing queue (like the remote's PlayLast); if
        # nothing is playing, just start them as a new queue.
        payload = payload or {}
        item_ids = payload.get("item_ids") or []
        if not item_ids:
            return
        from .player import playerManager
        try:
            if not playerManager.has_video():
                self.on_play(payload)
                return
            video = playerManager.get_video()
            if video is not None:
                video.parent.insert_items(item_ids, append=True)
                playerManager.upd_player_hide()
        except Exception:
            log.error("Failed to queue items from library browser",
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
        prev, self._quick_connect_cancel = self._quick_connect_cancel, cancel
        if prev is not None:
            # Supersede any still-polling flow. Without this the old worker
            # keeps polling (uncancellably) for up to 5 minutes and then posts
            # its stale server_result, yanking the UI to Home and/or adding a
            # server the user had abandoned.
            prev.set()

        def work():
            try:
                client, secret, code = clientManager.quick_connect_initiate(server)
            except QuickConnectError as e:
                if not cancel.is_set():
                    self._send_browser(("server_result",
                                        {"ok": False, "error": str(e)}))
                return
            except Exception:
                log.error("Error starting Quick Connect.", exc_info=True)
                if not cancel.is_set():
                    self._send_browser(("server_result", {
                        "ok": False,
                        "error": _("Could not start Quick Connect."),
                    }))
                return

            if cancel.is_set():
                return  # superseded/cancelled while initiating
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

    # -- user switching ----------------------------------------------------

    def on_switch_user(self, payload):
        """Switch the active local user (parental-control PIN gated).

        A locked user requires a matching PIN before the switch proceeds — this
        gates both an in-session switch and the startup unlock gate. The connect
        runs on a worker thread (it can block on retries) so the action loop
        keeps flowing; the browser shows the connecting screen meanwhile."""
        payload = payload or {}
        user_id = payload.get("user_id")
        pin = payload.get("pin")

        target = userManager.get(user_id)
        if target is None:
            self._send_browser(("switch_result",
                                {"ok": False, "error": _("User not found.")}))
            return
        if userManager.is_locked(user_id) and not userManager.verify_pin(user_id, pin):
            self._send_browser(("switch_result",
                                {"ok": False, "error": _("Incorrect PIN.")}))
            return
        if self._switching:
            # Reply, don't drop: a locked-user PIN dialog waits on this
            # switch_result and would hang open forever on silence. "busy"
            # tells the browser a switch is still running (so it should stay
            # on the connecting screen rather than bailing out).
            self._send_browser(("switch_result", {
                "ok": False, "busy": True,
                "error": _("Another user switch is already in progress."),
            }))
            return

        # Accepted: tell the browser to drop any PIN dialog and show connecting,
        # then do the real work off the loop.
        self._switching = True
        self._startup_locked = False
        self._send_browser(("switch_result", {"ok": True}))

        def work():
            try:
                clientManager.switch_user(user_id)
            except Exception:
                log.error("Error switching user.", exc_info=True)
            finally:
                self._switching = False
            self._push_users()
            self._push_connection_settled()

        threading.Thread(target=work, daemon=True).start()

    def on_add_user(self, payload):
        name = (payload or {}).get("name", "")
        userManager.add_user(name)
        self._push_users()

    def on_rename_user(self, payload):
        payload = payload or {}
        userManager.rename_user(payload.get("user_id"), payload.get("name", ""))
        self._push_users()

    def on_delete_user(self, payload):
        payload = payload or {}
        ok, error = userManager.delete_user(payload.get("user_id"))
        if not ok:
            self._send_browser(("user_action_error", {"error": error}))
        self._push_users()

    def on_set_user_pin(self, payload):
        """Set / change / clear a user's PIN. Changing or clearing an existing
        PIN requires the current PIN (so a child can't just remove the lock)."""
        payload = payload or {}
        user_id = payload.get("user_id")
        new_pin = payload.get("pin") or ""
        require_startup = bool(payload.get("require_startup"))
        current_pin = payload.get("current_pin")

        if userManager.is_locked(user_id) and not userManager.verify_pin(
            user_id, current_pin
        ):
            self._send_browser(("user_action_error",
                                {"error": _("Incorrect PIN.")}))
            return
        userManager.set_pin(user_id, new_pin, require_startup)
        self._push_users()

    def on_request_logs(self, _param):
        self._send_browser(("log_init", list(log_cache)))

    def on_save_settings(self, changes):
        move_target = None
        move_requested = False
        if isinstance(changes, dict):
            # A download-folder change moves files (possibly across drives) and
            # is handled asynchronously below — peel it off so it isn't written
            # until the move actually succeeds.
            if "sync_path" in changes:
                new_path = changes.get("sync_path") or None
                if (new_path or "") != (settings.sync_path or ""):
                    move_requested = True
                    move_target = new_path
                    changes = {k: v for k, v in changes.items() if k != "sync_path"}
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
        if move_requested:
            self._start_folder_move(move_target)

    def _start_folder_move(self, new_path):
        """Move the download store on a background thread (never on the UI action
        loop — a large cross-drive copy would freeze it and Windows would kill
        the process), streaming byte progress to the browser."""
        if self._folder_move_active:
            self._send_browser(("settings_status", {"ok": False,
                "text": _("A download-folder change is already running.")}))
            return
        self._folder_move_active = True

        def work():
            try:
                def progress(copied, total):
                    self._send_browser(("download_folder_progress",
                                        {"copied": copied, "total": total}))
                ok, message = syncManager.relocate(new_path, progress=progress)
                if ok:
                    # Persist the resolved destination so start() and relocate
                    # agree on where the store lives (relocate expands ~ and
                    # makes the path absolute).
                    settings.sync_path = syncManager.root if new_path else None
                    try:
                        settings.save()
                    except Exception:
                        log.error("Failed to persist sync_path", exc_info=True)
                    self._send_browser(("catalog_path",
                        syncManager.db.path if syncManager.db else None))
                    self._send_browser(("settings_data", settings.dict()))
                    # Refresh the browser's download counts against the moved
                    # catalog so indicators/status aren't stale.
                    self._push_sync_state()
                    # The browser subprocess still holds the old catalog wiring
                    # for live download progress; a restart re-reads everything.
                    self._send_browser(("settings_status", {"ok": True,
                        "restart": True,
                        "text": _("Download folder moved. Restart the app to "
                                  "finish switching to the new folder.")}))
                else:
                    self._send_browser(("settings_status",
                                        {"ok": False, "text": message}))
            except Exception:
                log.error("Download folder move failed", exc_info=True)
                self._send_browser(("settings_status", {"ok": False,
                    "text": _("Moving the downloads failed.")}))
            finally:
                self._folder_move_active = False

        threading.Thread(target=work, daemon=True).start()

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
                    playlist_id=payload.get("playlist_id"),
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
            elif watched and syncManager.db:
                # Fully offline: queue the watched mark (advance-only sync).
                # Marking unwatched offline isn't representable, so it's skipped.
                # A series/season id isn't itself a download — fan the mark out
                # to its downloaded episodes, mirroring the server's cascade.
                targets = self._offline_watch_targets(item_id, server_uuid)
                for target_id, target_server in targets:
                    try:
                        syncManager.db.upsert_playstate(target_server, target_id,
                                                        played=True)
                        # Mirror into the stored userdata like offline playback
                        # does: the browser overlay and watched-based delete read
                        # userdata_json, not the pending queue — without this the
                        # mark is invisible until the server syncs.
                        syncManager.db.update_userdata(target_id, played=True)
                        ok = True
                    except Exception:
                        log.error("Failed to queue offline watched mark for %s",
                                  target_id, exc_info=True)
                if not targets:
                    log.warning("Cannot change watched state for %s while "
                                "offline.", item_id)
            else:
                log.warning("Cannot change watched state for %s while offline.",
                            item_id)
            if ok and refresh:
                self._send_browser(("watched_changed", None))

        threading.Thread(target=work, daemon=True).start()

    def on_set_favorite(self, payload):
        payload = payload or {}
        item_id = payload.get("item_id")
        if not item_id:
            return
        favorite = bool(payload.get("favorite"))
        client = clientManager.clients.get(payload.get("server_uuid"))
        if client is None:
            log.warning("Cannot change favorite state for %s while offline.",
                        item_id)
            return

        def work():
            try:
                client.jellyfin.favorite(item_id, favorite)
            except Exception:
                log.error("Failed to set favorite=%s for %s", favorite,
                          item_id, exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    # -- playlist / collection editing --------------------------------------

    def _run_edit(self, kind, op, work_fn):
        """Run an edit call off the action loop and report the outcome back
        to the browser as one edit_result message."""
        def work():
            ok, error = False, None
            try:
                work_fn()
                ok = True
            except AttributeError:
                # Old apiclient without the editing calls; normally the UI is
                # hidden (edit_apis flag), but a stale browser could still ask.
                log.warning("%s edit needs a newer jellyfin-apiclient-python",
                            kind, exc_info=True)
                error = _("This needs a newer jellyfin-apiclient-python.")
            except Exception:
                log.error("%s %s failed", kind, op, exc_info=True)
                error = _("The change could not be applied.")
            self._send_browser(("edit_result", {
                "ok": ok, "error": error, "kind": kind, "op": op}))

        threading.Thread(target=work, daemon=True).start()

    def on_playlist_edit(self, payload):
        payload = payload or {}
        op = payload.get("op")
        client = clientManager.clients.get(payload.get("server_uuid"))
        if client is None:
            self._send_browser(("edit_result", {
                "ok": False, "kind": "playlist", "op": op,
                "error": _("Editing needs a server connection.")}))
            return
        pid = payload.get("playlist_id")

        def apply():
            if op == "add":
                client.jellyfin.add_playlist_items(
                    pid, payload.get("item_ids") or [])
            elif op == "remove":
                client.jellyfin.remove_playlist_items(
                    pid, payload.get("entry_ids") or [])
            elif op == "move":
                # Moves are (entry_id, absolute index), pre-ordered by the
                # browser so sequential application lands the final order.
                for entry_id, new_index in payload.get("moves") or []:
                    client.jellyfin.move_playlist_item(pid, entry_id,
                                                       new_index)
            elif op == "create":
                client.jellyfin.new_playlist(
                    payload.get("name") or _("New Playlist"),
                    payload.get("item_ids") or [],
                    is_public=payload.get("is_public"))
            elif op == "update":
                # Rename and/or visibility change; only the provided fields are
                # touched (None leaves the server's value alone).
                client.jellyfin.update_playlist(
                    pid, name=payload.get("name"),
                    is_public=payload.get("is_public"))
            elif op == "delete":
                # A playlist is a normal item; deleting it removes it for every
                # user (the underlying videos are untouched).
                client.jellyfin.delete_item(pid)
            else:
                raise ValueError("unknown playlist op %r" % op)

        self._run_edit("playlist", op, apply)

    def on_collection_edit(self, payload):
        payload = payload or {}
        op = payload.get("op")
        client = clientManager.clients.get(payload.get("server_uuid"))
        if client is None:
            self._send_browser(("edit_result", {
                "ok": False, "kind": "collection", "op": op,
                "error": _("Editing needs a server connection.")}))
            return
        cid = payload.get("collection_id")

        def apply():
            if op == "add":
                client.jellyfin.add_collection_items(
                    cid, payload.get("item_ids") or [])
            elif op == "remove":
                client.jellyfin.remove_collection_items(
                    cid, payload.get("item_ids") or [])
            elif op == "create":
                client.jellyfin.new_collection(
                    payload.get("name") or _("New Collection"),
                    payload.get("item_ids") or [])
            else:
                raise ValueError("unknown collection op %r" % op)

        self._run_edit("collection", op, apply)

    # -- SyncPlay (browse-side join; in-player menu keeps group creation) ---

    @staticmethod
    def _syncplay_manager():
        from .player import playerManager
        return playerManager.syncplay

    def on_syncplay_groups(self, _param):
        def work():
            current = None
            try:
                sp = self._syncplay_manager()
                if sp.is_enabled():
                    current = sp.current_group
            except Exception:
                log.debug("SyncPlay state unavailable", exc_info=True)
            name_by_uuid = {c.get("uuid"): c.get("Name") or c.get("address")
                            for c in list(clientManager.credentials)}
            groups = []
            for uuid, client in list(clientManager.clients.items()):
                try:
                    for g in client.jellyfin.get_sync_play() or []:
                        groups.append({
                            "server_uuid": uuid,
                            "server_name": (name_by_uuid.get(uuid)
                                            if len(clientManager.clients) > 1
                                            else None),
                            "group_id": g.get("GroupId"),
                            "name": g.get("GroupName"),
                            "participants": g.get("Participants") or [],
                        })
                except Exception:
                    log.warning("Failed to list SyncPlay groups for %s", uuid,
                                exc_info=True)
            self._send_browser(("syncplay_groups",
                                {"groups": groups, "current": current}))

        threading.Thread(target=work, daemon=True).start()

    def on_syncplay_join(self, payload):
        payload = payload or {}
        client = clientManager.clients.get(payload.get("server_uuid"))
        group_id = payload.get("group_id")
        if client is None or not group_id:
            return

        def work():
            try:
                # One group at a time: leave the current one first (matching
                # what the server would force anyway on the same connection).
                sp = self._syncplay_manager()
                if sp.is_enabled() and sp.client is not None \
                        and sp.client is not client:
                    sp.client.jellyfin.leave_sync_play()
                client.jellyfin.join_sync_play(group_id)
                # The server replies over the websocket (GroupJoined + queue);
                # playback starts from that push, same as an in-player join.
            except Exception:
                log.error("Failed to join SyncPlay group", exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    def on_syncplay_leave(self, _param):
        def work():
            try:
                sp = self._syncplay_manager()
                if sp.client is not None:
                    sp.client.jellyfin.leave_sync_play()
            except Exception:
                log.error("Failed to leave SyncPlay group", exc_info=True)

        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _offline_watch_targets(item_id, server_uuid):
        """Resolve an offline watched-mark to the downloaded items it covers:
        the item itself if it's a completed download, otherwise every completed
        episode of the series/season ``item_id`` names. Returns a list of
        (item_id, server_uuid) pairs; empty when nothing downloaded matches."""
        from .sync.db import STATUS_COMPLETE
        db = syncManager.db
        if db.is_complete(item_id):
            return [(item_id, server_uuid)]
        targets = []
        for row in db.list(status=STATUS_COMPLETE):
            if row["series_id"] == item_id or row["season_id"] == item_id:
                targets.append((row["item_id"],
                                row["server_uuid"] or server_uuid))
        return targets

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
