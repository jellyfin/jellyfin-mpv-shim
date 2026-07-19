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
import threading

from ..clients import clientManager
from ..conf import settings

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
        # Logo-free, free-resizing browse window (not force_window()'s menu
        # splash) — removes the Jellyfin icon and stops aspect-ratio snapping.
        playerManager.set_browse_window(True)
        playerManager.enable_osc(False)

    def on_browse_leave(self):
        from ..player import playerManager
        # Hand the OSC back for playback (respecting the user's setting).
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

    def toggle_pause(self):
        self._act(lambda pm: pm.toggle_pause())

    def stop(self):
        self._act(lambda pm: pm.stop_and_close())

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

    # -- startup PIN lock -------------------------------------------------

    def needs_unlock(self):
        from ..users import userManager
        try:
            return bool(userManager.startup_needs_unlock())
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

    def start(self):
        # The browser is created in login_servers, once the mpv handle and
        # saved credentials are available.
        pass

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

    def _run(self):
        try:
            self._app.run(self._browser.build)
        except Exception:
            log.error("mpvtk browser loop crashed", exc_info=True)
        finally:
            # Window closed -> release main()'s halt loop.
            if self.stop_callback is not None:
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
        playerManager.mpvtk_active = False
        if self._app is not None:
            self._app.quit()
        if self._browser is not None:
            self._browser.shutdown()


user_interface = UserInterface()
