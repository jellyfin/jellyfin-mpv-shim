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

    def play(self, item, server_uuid, offset_ticks=None):
        self.play_list([item.get("Id")], server_uuid, 0,
                       offset_ticks=offset_ticks)

    def play_list(self, item_ids, server_uuid, start_index, offset_ticks=None):
        from ..event_handler import start_playback
        client = clientManager.clients.get(server_uuid)
        if client is None:
            log.warning("mpvtk play: no connected client for %s", server_uuid)
            return
        try:
            start_playback(client, list(item_ids), start_index=start_index,
                           offset_ticks=offset_ticks)
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
            log.warning("mpvtk browser: no servers connected")
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
