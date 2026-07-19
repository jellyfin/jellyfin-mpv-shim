"""mpvtk browser as a ``user_interface`` — the in-process launcher.

Exposes the same small surface ``mpv_shim.main`` expects (``start``,
``login_servers``, ``stop``, ``open_player_menu``, ``stop_callback``) as
``cli_mgr``/``gui_mgr``, but instead of a Tk window in a child process it
attaches the mpvtk UI to the player's own mpv window (main process, next
to ``playerManager``).

Lifecycle: open the window immediately on a spinner (``enter_browse`` →
``force_window`` + OSC off), connect to servers in the background, then
swap in the live data source. A tile click on a playable item yields the
window to playback + the OSC; when playback stops (``on_playstate``) the
browser takes the window back.
"""

import logging
import os
import threading

from ..clients import clientManager
from ..conf import settings
from ..i18n import _

log = logging.getLogger("mpvtk_browser.ui")


def _collect_servers():
    """Connected servers with tokens — what the browser browses with.
    Mirrors gui_mgr._collect_servers so LibrarySource gets the same shape."""
    name_by_uuid = {
        cred.get("uuid"): cred.get("Name") or cred.get("address")
        for cred in list(clientManager.credentials)
    }
    servers = []
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


class _PlayerController:
    """Bridges the browser to the player: playback + browse/play window
    state. Imports player/event_handler lazily so the browser package
    stays independent of them for unit tests."""

    def on_browse_enter(self):
        from ..player import playerManager
        # mpvtk_active tells the player the in-window UI is on screen — it
        # gates the idle-quit and makes `q` return to the library.
        playerManager.mpvtk_active = True
        # Logo-free, free-resizing browse window (not force_window()'s menu
        # splash) — removes the Jellyfin icon and stops aspect-ratio snapping.
        playerManager.set_browse_window(True)
        playerManager.enable_osc(False)

    def on_minimize(self):
        """Drop to the windowless state. set_browse_window(False) releases
        force_window when nothing is playing; if a cast is in flight it
        leaves the picture alone, which is exactly the behaviour we want."""
        from ..player import playerManager
        # Clearing mpvtk_active un-gates the idle quit, so a minimized app
        # eventually drops mpv entirely and gives back its memory and GPU
        # context. It comes back on the next play or when the tray reopens
        # the library (see UserInterface.on_mpv_recreated).
        playerManager.mpvtk_active = False
        playerManager.enable_osc(settings.enable_osc)
        playerManager.set_browse_window(False)

    def on_browse_leave(self):
        from ..player import playerManager
        # Restore video aspect handling / playback fullscreen, then hand the
        # OSC back (respecting the user's setting).
        playerManager.browse_yield()
        playerManager.enable_osc(settings.enable_osc)

    def play(self, item, server_uuid, offset_ticks=None,
             srcid=None, aid=None, sid=None):
        self.play_list([item.get("Id")], server_uuid, 0,
                       offset_ticks=offset_ticks, srcid=srcid, aid=aid, sid=sid)

    def play_list(self, item_ids, server_uuid, start_index, offset_ticks=None,
                  srcid=None, aid=None, sid=None):
        from ..event_handler import start_playback
        client = clientManager.clients.get(server_uuid)
        if client is None:
            log.warning("mpvtk play: no connected client for %s", server_uuid)
            return
        try:
            start_playback(
                client, list(item_ids), start_index=start_index,
                offset_ticks=offset_ticks, aid=aid, sid=sid, srcid=srcid,
                explicit_tracks=(aid is not None or sid is not None))
        except Exception:
            log.error("mpvtk browser failed to start playback", exc_info=True)

    # -- now-playing bar transport (swallow errors like gui_mgr does) -----

    @staticmethod
    def _act(fn):
        from ..player import playerManager
        try:
            fn(playerManager)
        except Exception:
            log.error("mpvtk player action failed", exc_info=True)

    def refresh_playstate(self):
        """Re-push the now-playing snapshot (the bar's 1s clock tick)."""
        from ..player import playerManager
        playerManager.push_playstate()

    def toggle_pause(self):
        self._act(lambda pm: pm.toggle_pause())

    def stop(self):
        # The now-playing bar's stop button must not take the window with it:
        # stop_and_close() drops force_window, which closed the library out
        # from under the bar that was just clicked.
        self._act(lambda pm: pm.stop_to_browser())

    def next(self):
        self._act(lambda pm: pm.play_next())

    def prev(self):
        self._act(lambda pm: pm.play_prev())

    def seek(self, secs):
        self._act(lambda pm: pm.seek(float(secs), absolute=True))

    def set_volume(self, pct):
        self._act(lambda pm: pm.set_volume(float(pct)))

    def set_repeat(self, mode):
        self._act(lambda pm: pm.set_repeat(mode))

    def toggle_favorite(self):
        self._act(lambda pm: pm.toggle_current_favorite())

    # -- tile actions (watched / favorite) --------------------------------

    def set_watched(self, server_uuid, item_id, watched):
        client = clientManager.clients.get(server_uuid)
        if client is None or not item_id:
            return
        try:
            client.jellyfin.item_played(item_id, bool(watched))
        except Exception:
            log.error("mpvtk set_watched failed", exc_info=True)

    def set_favorite(self, server_uuid, item_id, favorite):
        client = clientManager.clients.get(server_uuid)
        if client is None or not item_id:
            return
        try:
            client.jellyfin.favorite(item_id, bool(favorite))
        except Exception:
            log.error("mpvtk set_favorite failed", exc_info=True)

    def list_servers(self):
        """Saved servers with a connection badge, for the Settings panel —
        the whole credential list, not just the connected ones _collect_servers
        returns (an offline server must still be removable)."""
        out = []
        for cred in list(clientManager.credentials):
            uuid = cred.get("uuid")
            client = clientManager.clients.get(uuid)
            out.append({
                "uuid": uuid,
                "name": cred.get("Name") or cred.get("address") or "?",
                "address": cred.get("address") or "",
                "username": cred.get("Username") or cred.get("username") or "",
                "connected": client is not None,
            })
        return out

    def remove_server(self, uuid):
        try:
            clientManager.remove_client(uuid)
            return True
        except Exception:
            log.error("mpvtk remove_server failed", exc_info=True)
            return False

    def add_server(self, server, username, password):
        try:
            return bool(clientManager.login(server, username, password))
        except Exception:
            log.error("mpvtk add_server failed", exc_info=True)
            return False

    def rebuild_source(self):
        from .repository import LibrarySource
        servers = _collect_servers()
        if not servers:
            return None
        return LibrarySource(servers, clientManager.device_id,
                             settings.player_name,
                             not settings.ignore_ssl_cert)

    # -- local users ------------------------------------------------------

    def list_users(self):
        """``[{id, name, locked, active}]`` for the chrome's user switcher."""
        from ..users import userManager
        try:
            active = userManager.active_id
            return [{"id": u["id"], "name": u.get("name", "?"),
                     "locked": bool(userManager.is_locked(u["id"])),
                     "active": u["id"] == active}
                    for u in userManager.public_users()]
        except Exception:
            log.error("mpvtk list_users failed", exc_info=True)
            return []

    def switch_user(self, user_id, pin=None):
        """Switch the active local user and rebuild the data source.

        Returns the new source, or None if the user is PIN-locked and the PIN
        didn't match (the caller re-prompts). Runs on the browser's worker
        pool — clientManager.switch_user reconnects and can block."""
        from ..users import userManager
        try:
            if userManager.get(user_id) is None:
                return None
            if userManager.is_locked(user_id) and not userManager.verify_pin(
                    user_id, pin or ""):
                return None
            clientManager.switch_user(user_id)
        except Exception:
            log.error("mpvtk switch_user failed", exc_info=True)
            return None
        return self.rebuild_source()

    def add_user(self, name):
        from ..users import userManager
        try:
            userManager.add_user(name)
        except Exception:
            log.error("mpvtk add_user failed", exc_info=True)

    def rename_user(self, user_id, name):
        from ..users import userManager
        try:
            userManager.rename_user(user_id, name)
        except Exception:
            log.error("mpvtk rename_user failed", exc_info=True)

    def delete_user(self, user_id):
        """Returns (ok, error) — the active user and the last user can't go."""
        from ..users import userManager
        try:
            return userManager.delete_user(user_id)
        except Exception:
            log.error("mpvtk delete_user failed", exc_info=True)
            return False, None

    def set_user_pin(self, user_id, pin, require_startup=False):
        from ..users import userManager
        try:
            userManager.set_pin(user_id, pin or None,
                                require_startup=require_startup)
            return True
        except Exception:
            log.error("mpvtk set_user_pin failed", exc_info=True)
            return False

    # -- startup PIN lock -------------------------------------------------

    def needs_unlock(self):
        from ..users import userManager
        try:
            return bool(userManager.startup_needs_unlock())
        except Exception:
            return False

    def unlock_user(self, user_id, pin):
        """Verify a specific user's PIN (the PIN-setup dialog's current-PIN
        check), as opposed to unlock() which gates the active user."""
        from ..users import userManager
        try:
            return bool(userManager.verify_pin(user_id, pin))
        except Exception:
            return False

    def unlock(self, pin):
        from ..users import userManager
        try:
            return bool(userManager.verify_pin(userManager.active_id, pin))
        except Exception:
            log.error("mpvtk unlock failed", exc_info=True)
            return False

    def connect_and_rebuild(self):
        if not settings.work_offline:
            try:
                clientManager.connect_all()
            except Exception:
                log.error("mpvtk connect failed", exc_info=True)
        return self.rebuild_source()

    def open_url(self, url):
        import webbrowser
        try:
            webbrowser.open(url)
        except Exception:
            log.error("could not open url %s", url, exc_info=True)

    def retry_connect(self):
        try:
            clientManager.connect_all()
        except Exception:
            log.error("mpvtk retry connect failed", exc_info=True)

    # -- play queue -------------------------------------------------------

    def get_queue(self):
        from ..player import playerManager
        try:
            return playerManager.get_queue()
        except Exception:
            log.error("mpvtk get_queue failed", exc_info=True)
            return {"items": [], "current_id": None}

    def skip_to(self, playlist_item_id):
        self._act(lambda pm: pm.skip_to(playlist_item_id))

    def queue_remove(self, playlist_item_ids):
        self._act(lambda pm: pm.queue_remove_many(list(playlist_item_ids)))

    def queue_reorder(self, ordered_playlist_item_ids):
        self._act(lambda pm: pm.queue_reorder(list(ordered_playlist_item_ids)))

    def queue_items(self, server_uuid, item_ids):
        """Append items to the playing queue; if nothing plays, start them."""
        from ..player import playerManager
        item_ids = list(item_ids)
        if not item_ids:
            return
        try:
            if not playerManager.has_video():
                self.play_list(item_ids, server_uuid, 0)
                return
            video = playerManager.get_video()
            if video is not None:
                video.parent.insert_items(item_ids, append=True)
                playerManager.upd_player_hide()
        except Exception:
            log.error("mpvtk queue_items failed", exc_info=True)

    # -- SyncPlay ---------------------------------------------------------

    def get_sync_groups(self, server_uuid):
        client = clientManager.clients.get(server_uuid)
        if client is None:
            return []
        try:
            return [{"id": g.get("GroupId"),
                     "name": g.get("GroupName") or "Group",
                     "participants": g.get("Participants") or []}
                    for g in (client.jellyfin.get_sync_play() or [])]
        except Exception:
            log.error("mpvtk get_sync_groups failed", exc_info=True)
            return []

    def _sync(self, server_uuid, fn):
        client = clientManager.clients.get(server_uuid)
        if client is None:
            return
        try:
            fn(client.jellyfin)
        except Exception:
            log.error("mpvtk syncplay action failed", exc_info=True)

    def sync_join(self, server_uuid, group_id):
        self._sync(server_uuid, lambda jf: jf.join_sync_play(group_id))

    def sync_new(self, server_uuid):
        self._sync(server_uuid, lambda jf: jf.new_sync_play())

    def sync_leave(self, server_uuid):
        self._sync(server_uuid, lambda jf: jf.leave_sync_play())

    # -- playlist editing -------------------------------------------------

    def _edit(self, server_uuid, fn):
        client = clientManager.clients.get(server_uuid)
        if client is None:
            return
        try:
            fn(client.jellyfin)
        except Exception:
            log.error("mpvtk playlist edit failed", exc_info=True)

    def playlist_move(self, server_uuid, playlist_id, entry_id, new_index):
        self._edit(server_uuid,
                   lambda jf: jf.move_playlist_item(playlist_id, entry_id,
                                                    new_index))

    def playlist_remove(self, server_uuid, playlist_id, entry_ids):
        self._edit(server_uuid,
                   lambda jf: jf.remove_playlist_items(playlist_id,
                                                       list(entry_ids)))

    def playlist_add(self, server_uuid, playlist_id, item_ids):
        self._edit(server_uuid,
                   lambda jf: jf.add_playlist_items(playlist_id,
                                                    list(item_ids)))

    def playlist_new(self, server_uuid, name, item_ids):
        self._edit(server_uuid,
                   lambda jf: jf.new_playlist(name, list(item_ids)))

    def playlist_delete(self, server_uuid, playlist_id):
        self._edit(server_uuid, lambda jf: jf.delete_item(playlist_id))

    def playlist_update(self, server_uuid, playlist_id, name=None,
                        is_public=None):
        self._edit(server_uuid,
                   lambda jf: jf.update_playlist(playlist_id, name=name,
                                                 is_public=is_public))

    # -- offline downloads ------------------------------------------------

    def download_estimate(self, server_uuid, item_id, item_type):
        from ..sync.manager import syncManager
        try:
            return syncManager.estimate(server_uuid, item_id, item_type)
        except Exception:
            log.error("mpvtk download estimate failed", exc_info=True)
            return {"count": 0, "total_bytes": 0}

    def download_enqueue(self, server_uuid, item_id, item_type,
                         include_watched=False):
        from ..sync.manager import syncManager
        try:
            syncManager.enqueue(server_uuid, item_id, item_type,
                                include_watched=include_watched)
        except Exception:
            log.error("mpvtk download enqueue failed", exc_info=True)

    def list_downloads(self):
        """Downloads grouped for display, mirroring the Tk DownloadsPanel:

            [{"kind": "series"|"movies", "title", "id", "size",
              "children": [{"kind": "season"|"item", ...}]}]

        Series nest their seasons, seasons nest their episodes, and loose
        movies/videos land in one flat group — a flat list of 400 episodes
        was unnavigable and gave no way to delete a whole show."""
        from ..sync.manager import syncManager
        try:
            rows = syncManager.db.list() if syncManager.db else []
        except Exception:
            log.error("mpvtk list_downloads failed", exc_info=True)
            return []

        def entry(r):
            return {
                "kind": "item",
                "id": r.get("item_id"),
                "title": r.get("name") or r.get("item_id"),
                "status": r.get("status") or "",
                "size": r.get("size") or 0,
                "index": r.get("index_number"),
            }

        series = {}
        loose = []
        for r in rows:
            sid = r.get("series_id")
            if not sid:
                loose.append(entry(r))
                continue
            show = series.setdefault(sid, {
                "kind": "series", "id": sid,
                "title": r.get("series_name") or _("Unknown Series"),
                "size": 0, "children": {},
            })
            show["size"] += r.get("size") or 0
            season_id = r.get("season_id") or sid
            season = show["children"].setdefault(season_id, {
                "kind": "season", "id": season_id,
                "series_id": sid,
                "title": (r.get("season_name")
                          or (_("Season %s") % r.get("parent_index")
                              if r.get("parent_index") is not None
                              else _("Episodes"))),
                "size": 0, "children": [],
            })
            season["size"] += r.get("size") or 0
            season["children"].append(entry(r))

        out = []
        for show in series.values():
            seasons = sorted(show["children"].values(),
                             key=lambda s: str(s["title"]))
            for s in seasons:
                s["children"].sort(key=lambda e: (e["index"] is None,
                                                  e["index"], e["title"]))
            show["children"] = seasons
            out.append(show)
        out.sort(key=lambda g: str(g["title"]))
        if loose:
            loose.sort(key=lambda e: str(e["title"]))
            out.append({"kind": "movies", "id": None, "title": _("Movies & Videos"),
                        "size": sum(e["size"] for e in loose),
                        "children": loose})
        return out

    def delete_download(self, item_id=None, series_id=None, season_id=None):
        """Delete one item, a whole season, or a whole series."""
        from ..sync.manager import syncManager
        try:
            syncManager.delete(item_id=item_id, series_id=series_id,
                               season_id=season_id)
        except Exception:
            log.error("mpvtk delete_download failed", exc_info=True)

    # -- diagnostics ------------------------------------------------------

    def recent_logs(self):
        from ..log_utils import recent_log_lines
        return recent_log_lines()

    def open_config_folder(self):
        """Reveal the config directory. The tray menu used to be the only way
        to reach it, and the mpvtk browser has no tray."""
        import subprocess
        import sys

        from .. import conffile
        from ..constants import APP_NAME

        path = os.path.dirname(conffile.get(APP_NAME, "conf.json"))
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", path])
            elif sys.platform == "win32":
                os.startfile(path)  # noqa: S606 - documented Windows API
            else:
                subprocess.Popen(["xdg-open", path])
        except Exception:
            log.error("could not open config folder %s", path, exc_info=True)

    def downloaded_ids(self):
        from ..sync.manager import syncManager
        try:
            return (set(syncManager.downloaded_item_ids()),
                    set(syncManager.downloaded_series_ids()))
        except Exception:
            return (set(), set())


class UserInterface:
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop_callback = None
        self.gui_ready = None
        self._app = None
        self._browser = None
        self._thread = None
        self._tray = None
        # True while we are deliberately tearing the render loop down (mpv
        # idle-quit / reconnect), so _run doesn't mistake it for a window
        # close and stop the whole app.
        self._detaching = False

    def start(self):
        # The tray is the only way to reach the app while the mpv window is
        # showing video (or is minimized), so it runs regardless of the
        # browser's state. It lives in its own process — pystray needs its
        # process's main thread, and pystray + libmpv in one process segfaults
        # with GNOME AppIndicator. See tray.py.
        from ..tray import TrayManager

        self._tray = TrayManager({
            "show": self.activate,
            "show_preferences": lambda: self._open_settings("servers"),
            "show_console": lambda: self._open_settings("logs"),
            "open_player_menu": lambda: self.open_player_menu(),
            "open_config": self._open_config_folder,
            "quit": self._quit,
        })
        self._tray.start()
        # The browser itself is created in login_servers, once the mpv handle
        # and saved credentials are available.

    # -- tray actions -----------------------------------------------------

    def activate(self):
        """Surface the UI: leave playback, show the browser, raise the window.

        Also what SingleInstance calls when the app is launched a second time
        (mpv_shim wires ``single.on_activate`` to this)."""
        from ..player import playerManager

        if self._browser is not None:
            self._browser.enter_browse()
        try:
            playerManager.raise_window()
        except Exception:
            log.debug("could not raise the player window", exc_info=True)

    def _open_settings(self, tab):
        if self._browser is None:
            return
        self.activate()
        self._browser.open_settings(tab)

    def _open_config_folder(self):
        _PlayerController().open_config_folder()

    def _quit(self):
        if self.stop_callback is not None:
            self.stop_callback()

    def on_window_closed(self):
        """The user closed the mpv window.

        With one shared window, closing it means "minimize to tray" — the app
        stays alive as a cast target. But that is only safe if there *is* a
        tray: without one the app would keep running with no way to reach or
        quit it, so we exit instead."""
        if not settings.close_to_tray:
            self._quit()
            return
        if self._tray is None or not self._tray.available:
            log.info("Window closed and no system tray is available; "
                     "exiting rather than becoming unreachable.")
            self._quit()
            return
        if self._browser is not None:
            self._browser.minimize()

    def login_servers(self):
        from ..player import playerManager, is_using_ext_mpv
        from ..mpvtk.app import MpvtkApp
        from ..mpvtk.rawimage import MemoryStore, cache_dir
        from .app import MpvtkBrowser
        from .repository import LibrarySource
        from .strips import StripStore
        from .thumbnails import ThumbnailStore

        clientManager.load_credentials()

        app = MpvtkApp.attach(playerManager.get_mpv(), ext=is_using_ext_mpv)
        self._app = app
        strips = (StripStore(mem_store=MemoryStore()) if app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-browser-")))
        thumbs = ThumbnailStore(
            cache_dir("mpvtk-thumbs-"),
            verify_ssl=not settings.ignore_ssl_cert,
            max_mem_mb=settings.library_image_cache_mb,
        )
        # Open immediately on an empty source (spinner); populate on connect.
        source = LibrarySource([], clientManager.device_id,
                               settings.player_name,
                               not settings.ignore_ssl_cert)
        browser = MpvtkBrowser(app, source, strips=strips, thumbs=thumbs,
                               controller=_PlayerController())
        self._browser = browser
        playerManager.mpvtk_active = True
        playerManager.on_playstate = browser.on_playstate
        # Update notices surface in the browser banner (not the MPV OSD).
        playerManager.notify_update = browser.notify_update

        playerManager.on_window_closed = self.on_window_closed
        # mpv is torn down and rebuilt across idle-quit and crash recovery;
        # the renderer is bound to a specific handle, so follow it.
        playerManager.on_mpv_gone = self.on_mpv_gone
        playerManager.on_mpv_recreated = self.on_mpv_recreated
        # start_minimized: come up in the windowless state — running, castable,
        # reachable from the tray — instead of opening the library. Without a
        # tray there'd be no way back, so honour it only when one is up.
        if settings.start_minimized and self._tray is not None:
            self._tray.ready.wait(5)
        if settings.start_minimized and self._tray is not None \
                and self._tray.available:
            browser.minimize()
        else:
            if settings.start_minimized:
                log.info("start_minimized ignored: no system tray to "
                         "restore the window from.")
            browser.enter_browse()  # take the window + hide the OSC
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mpvtk-browser")
        self._thread.start()
        # A startup PIN gates connection: show the lock screen and let the
        # unlock drive the connect. Otherwise connect in the background.
        from ..users import userManager
        try:
            locked = userManager.startup_needs_unlock()
        except Exception:
            locked = False
        if locked:
            browser.show_locked()
        else:
            threading.Thread(target=self._connect, daemon=True,
                             name="mpvtk-connect").start()

    # -- following mpv across teardown / re-create -------------------------

    def on_mpv_gone(self):
        """mpv terminated (idle-quit or a lost connection).

        Stop the render loop and drop every composited bitmap. On libmpv those
        are in-process buffers that the dead mpv read by address, so holding
        them would both leak and defeat the memory saving that quitting mpv
        while minimized is for."""
        self._detaching = True
        app, self._app = self._app, None
        if app is not None:
            app.quit()
        if self._browser is not None:
            self._browser.app = None
            try:
                self._browser.strips.clear()
            except Exception:
                log.debug("clearing the tile cache failed", exc_info=True)

    def on_mpv_recreated(self):
        """A fresh mpv handle exists — attach a new renderer to it.

        mpvtk binds its event callbacks and loads renderer.lua at attach time,
        so the app object is per-handle; the browser keeps all of its state
        (routes, data, caches) and simply gets pointed at the new one."""
        from ..player import playerManager, is_using_ext_mpv
        from ..mpvtk.app import MpvtkApp

        if self._browser is None:
            return
        try:
            app = MpvtkApp.attach(playerManager.get_mpv(), ext=is_using_ext_mpv)
        except Exception:
            log.error("could not re-attach the mpvtk UI to the new mpv",
                      exc_info=True)
            return
        self._app = app
        self._browser.app = app
        self._detaching = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mpvtk-browser")
        self._thread.start()
        # A fresh renderer starts active; if mpv came back for a cast while we
        # were minimized, it must stay out of the way.
        if not self._browser._browsing:
            self._browser._set_renderer_active(False)
        self._browser.invalidate()

    def _run(self):
        app = self._app
        try:
            app.run(self._browser.build)
        except Exception:
            log.error("mpvtk browser loop crashed", exc_info=True)
        finally:
            # A loop that ended because *we* detached (idle-quit, reconnect)
            # is expected — only a real window close should stop the app.
            if not self._detaching and self.stop_callback is not None:
                self.stop_callback()

    def _connect(self):
        from .repository import LibrarySource
        if not settings.work_offline:
            try:
                clientManager.connect_all()
            except Exception:
                log.error("mpvtk browser connect failed", exc_info=True)
        servers = _collect_servers()
        if not servers:
            log.warning("mpvtk browser: no servers connected; showing login")
            if self._browser is not None:
                self._browser.show_login()
            return
        source = LibrarySource(servers, clientManager.device_id,
                               settings.player_name,
                               not settings.ignore_ssl_cert)
        if self._browser is not None:
            self._browser.set_source(source)

    def stop(self):
        from ..player import playerManager
        if self._tray is not None:
            self._tray.stop()
        playerManager.mpvtk_active = False
        if self._app is not None:
            self._app.quit()
        if self._browser is not None:
            self._browser.shutdown()


user_interface = UserInterface()
