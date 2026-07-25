"""mpvtk browser as a ``user_interface`` — the in-process launcher.

Exposes the same small surface ``mpv_shim.main`` expects (``start``,
``login_servers``, ``stop``, ``open_player_menu``, ``stop_callback``) as
``cli_mgr``, but instead of a separate window in a child process it
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
import time
from typing import TYPE_CHECKING, Optional, cast

from ..clients import clientManager
from ..conf import settings
from ..i18n import _
from .gateway import (PlayerGateway, _collect_servers,
                             _saved_servers_exist)

if TYPE_CHECKING:
    # Annotation-only: both are built inside _attach, which imports them
    # lazily so this module stays importable without player.py.
    from ..mpvtk.app import MpvtkApp
    from .app import MpvtkBrowser

log = logging.getLogger("mpvtk_browser.ui")



class UserInterface:
    def __init__(self):
        self.open_player_menu = lambda: None
        self.stop_callback = None
        self.gui_ready = None
        self._app: Optional["MpvtkApp"] = None
        self._browser: Optional["MpvtkBrowser"] = None
        self._thread: Optional[threading.Thread] = None
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
            # Re-gate behind the startup PIN before anything is revealed: the
            # unlock at launch covers that launch, not every later reopen.
            self._browser.maybe_relock()
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

    def _display_content(self, client, item_id):
        """Route a remote's DisplayContent to the browser, resolving which
        connected server it came from."""
        if self._browser is None:
            return
        uuid = next((u for u, c in clientManager.clients.items()
                     if c is client), None)
        # display_item decides whether to take the window — it must not
        # interrupt playback, so waking the client is its call, not ours.
        self._browser.display_item(uuid, item_id)

    def _open_config_folder(self):
        PlayerGateway().open_config_folder()

    def _quit(self):
        if self.stop_callback is not None:
            self.stop_callback()

    def _can_run_windowless(self):
        """True if the app may keep running with no window on screen.

        Either the tray can bring it back, or the user set allow_background
        and accepted that `jellyfin-mpv-shim stop` is the way out. Anything
        else would leave a process nobody can see or reach.

        Cast-target mode is the exception: there is no library to come back
        to, and staying reachable *over the network* is the entire job. On
        those machines exiting on window close is the failure, not the
        safeguard — close_to_tray=False is how you ask for the app to quit.
        """
        if settings.headless:
            return True
        if self._tray is not None and self._tray.available:
            return True
        return bool(settings.allow_background)

    def _may_start_minimized(self):
        """True if we may come up with no window on screen.

        `--minimized` on the command line counts on its own: it is a decision
        made for this launch, in a terminal, by someone who can see the log
        line telling them how to get the window back. The config key is not
        self-authorizing in the same way — it may have been set on a machine
        that had a tray at the time — so that path still wants a tray or
        allow_background.

        Either way there is a way back: launching the app again asks the
        running copy to surface its window (see single_instance.py).
        """
        if self._can_run_windowless():
            return True
        from ..args import get_args

        if get_args().start_minimized:
            log.info("Started minimized with no system tray. Run "
                     "jellyfin-mpv-shim again to show the window, or "
                     "jellyfin-mpv-shim stop to exit.")
            return True
        return False

    def on_window_closed(self):
        """The user closed the mpv window.

        With one shared window, closing it means "minimize to tray" — the app
        stays alive as a cast target. But that is only safe if there *is* a
        tray: without one the app would keep running with no way to reach or
        quit it, so we exit instead — unless allow_background says the user
        asked for exactly that and knows how to stop it."""
        if not settings.close_to_tray:
            self._quit()
            return
        if not self._can_run_windowless():
            log.info("Window closed and no system tray is available; "
                     "exiting rather than becoming unreachable.")
            self._quit()
            return
        if self._browser is not None:
            # Gate now, while the window is going away, so the locked screen
            # is what's already there when it comes back.
            self._browser.maybe_relock()
            self._browser.minimize()
        # Closing the window means "stop playing" — for music especially,
        # which kept going with no window to control it from. Order matters:
        # minimize() cannot release force_window while something is playing
        # (set_browse_window's `not self._video` guard), so the window used to
        # stay on screen. Stopping *after* it re-enters minimize() through the
        # stopped playstate, which is where force_window finally drops.
        PlayerGateway().stop_for_close()

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
                               controller=PlayerGateway())
        self._browser = browser
        playerManager.mpvtk_active = True
        playerManager.on_playstate = browser.on_playstate
        # Loading screen + failure/retry UI. Without these a failed start was
        # a blank window for the whole playback_timeout and then nothing.
        playerManager.on_load_start = browser.on_load_start
        playerManager.on_load_error = browser.on_load_error
        # Update notices surface in the browser banner (not the MPV OSD).
        playerManager.notify_update = browser.notify_update

        playerManager.on_window_closed = self.on_window_closed
        # A server that was down at startup must appear once it answers,
        # rather than staying invisible until a manual retry or restart.
        clientManager.on_server_connected = self._on_server_connected
        # Refresh download badges the moment the catalog changes, rather
        # than only when Settings -> Downloads is opened. The push hook has
        # always existed; the browser just never subscribed.
        PlayerGateway().on_downloads_changed(browser.on_downloads_changed)
        # mpv is torn down and rebuilt across idle-quit and crash recovery;
        # the renderer is bound to a specific handle, so follow it.
        playerManager.on_mpv_gone = self.on_mpv_gone
        playerManager.on_mpv_terminated = self.on_mpv_terminated
        playerManager.on_mpv_recreated = self.on_mpv_recreated
        playerManager.on_hud_menu = self._browser.open_hud_menu
        # start_minimized: come up in the windowless state — running, castable,
        # reachable from the tray — instead of opening the library.
        # Settle whether there is a tray *before* deciding: the tray comes up
        # in another process a moment after we do, and asking too early both
        # picks the fallback path and logs advice that is about to be wrong.
        if settings.start_minimized and self._tray is not None \
                and not settings.allow_background:
            self._tray.ready.wait(5)
        if settings.start_minimized and self._may_start_minimized():
            browser.minimize()
        else:
            if settings.start_minimized:
                log.info("start_minimized ignored: no system tray to restore "
                         "the window from. Set allow_background to run "
                         "windowless anyway.")
            browser.enter_browse()  # take the window + hide the OSC
        if browser.headless:
            # Cast-target UX: the backdrop wants the whole screen, and the
            # browse window deliberately is not fullscreen (browser_
            # fullscreen), so ask explicitly — as the old mirror did.
            browser.show_cast()
            try:
                playerManager.set_fullscreen(True)
            except Exception:
                log.debug("headless fullscreen failed", exc_info=True)
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mpvtk-browser")
        self._thread.start()
        browser.start_background_work()
        # BACK/ESC — from the keyboard or a Jellyfin remote (menu_action maps
        # "back" to ESC when the in-window UI owns input).
        playerManager.on_nav_back = browser.on_back
        playerManager.on_nav_command = browser.on_nav_command
        # "Show me this" from a phone/web client. Always ours now — the
        # browser either opens the item's page or, in headless mode, paints
        # it on the cast screen.
        from ..event_handler import eventHandler

        eventHandler.display_content = self._display_content
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
            # On the connecting screen, not an empty home route: a home
            # route with no source renders as a bare spinner with nothing
            # explaining it and no way past a server that never answers.
            browser.show_connecting()
            threading.Thread(target=self._connect, daemon=True,
                             name="mpvtk-connect").start()

    # -- following mpv across teardown / re-create -------------------------

    def on_mpv_gone(self):
        """The mpv handle is no longer ours (idle-quit or a lost connection).

        Stop the render loop and detach. Deliberately does NOT free the
        composited tile bitmaps: on libmpv those are in-process buffers mpv
        reads BY ADDRESS every frame it composites, and mpv is still being
        terminated on another thread at this point. Freeing here released
        memory out from under a live compositor — a segfault on quit. That
        happens in on_mpv_terminated instead."""
        self._detaching = True
        app, self._app = self._app, None
        if app is not None:
            app.quit()
        # Wait for the render loop to actually stop before anything else
        # touches the caches it reads. quit() only enqueues.
        # Deliberately NOT cleared when the join times out: on_mpv_recreated
        # joins it again rather than starting a second loop alongside it.
        # build() is not reentrant (it writes _size, _live_offsets, the
        # poster caches, and starts pollers), so two of them is corruption.
        self._join_render_loop()
        if self._browser is not None:
            self._browser.app = None

    def on_mpv_terminated(self):
        """mpv is really dead — now the tile buffers can go.

        Holding them would both leak and defeat the memory saving that
        quitting mpv while minimized is for; freeing them any earlier
        crashes. See playerManager.on_mpv_terminated."""
        if self._browser is not None:
            try:
                # NB not strips.shutdown() here: mpv may be re-created
                # afterward (on_mpv_recreated) and the browser reuses this
                # store, so the pool must survive. clear() is lock-safe against
                # a concurrent worker insert, and mpv is dead by contract, so a
                # strip still composing can't fault. The pool is only really
                # torn down in browser.shutdown().
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
        # Belt and braces: on_mpv_gone joins the old loop, but its join is
        # bounded. Starting a second one alongside a survivor would have two
        # threads calling the non-reentrant build().
        if not self._join_render_loop():
            # The old loop is wedged. A second one would race it inside
            # build(), which is worse than not re-attaching.
            return
        self._app = app
        # set_app (not a bare assignment): the fresh app needs the
        # browser's nav/HUD callbacks re-wired or its events go nowhere.
        self._browser.set_app(app)
        self._detaching = False
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="mpvtk-browser")
        self._thread.start()
        # A fresh renderer starts active; re-assert the real state
        # (browse / HUD-idle for a video in flight / fully out of the way).
        self._browser.reassert_window_state()
        self._browser.invalidate()

    RENDER_LOOP_JOIN = 2.0

    def _join_render_loop(self):
        """Stop tracking the render loop once it has actually exited.

        Returns True if it is gone. A survivor is kept in ``_thread`` so the
        next attach joins it again instead of racing it."""
        thread = self._thread
        if thread is None or not thread.is_alive():
            self._thread = None
            return True
        if thread is threading.current_thread():
            # We ARE the render loop. This is reachable: a now-playing bar
            # button runs on this thread, run_action's fast path executes the
            # player method inline, and a dead handle takes it through
            # _handle_mpv_disconnect -> _notify_mpv_gone -> on_mpv_gone.
            # join() would raise "cannot join current thread", which
            # _notify_mpv_gone swallows -- so the detach silently stopped
            # half-done, leaving _browser.app pointing at the dead handle and
            # _thread holding a reference to us forever.
            #
            # The loop is already unwinding by the time it gets here (quit()
            # was enqueued above), and it cannot race itself, so treating this
            # as "gone" is both safe and what the caller means.
            log.debug("detaching from inside the render loop; not joining self")
            self._thread = None
            return True
        thread.join(timeout=self.RENDER_LOOP_JOIN)
        if thread.is_alive():
            log.warning("mpvtk render loop did not stop within %.0fs; "
                        "not starting another alongside it",
                        self.RENDER_LOOP_JOIN)
            return False
        self._thread = None
        return True

    def _run(self):
        from ..player import _mpv_errors, bound_ipc_replies
        # _attach sets both before it starts the thread this runs on, so
        # state the invariant rather than add a guard that would turn a
        # wiring bug into a loop that silently never starts.
        app = cast("MpvtkApp", self._app)
        browser = cast("MpvtkBrowser", self._browser)
        try:
            app.run(browser.build)
        except _mpv_errors:
            # Not a crash: the window went away under us. This is also the
            # earliest hard evidence that the IPC socket is dead, and the
            # player is very likely mid-teardown on the action thread with
            # a command whose reply will never arrive — so tighten the
            # reply wait now rather than after it has already parked.
            log.info("mpvtk browser loop ended: mpv is gone")
            bound_ipc_replies()
        except Exception:
            log.error("mpvtk browser loop crashed", exc_info=True)
        finally:
            # A loop that ended because *we* detached (idle-quit, reconnect)
            # is expected — only a real window close should stop the app.
            if not self._detaching and self.stop_callback is not None:
                self.stop_callback()

    def _on_server_connected(self, *_a):
        """A server came up after startup — rebuild so it appears.

        keep_place: this fires from the websocket redial loop, the
        cast-recovery path and the periodic health check, so it lands at
        arbitrary moments mid-session. Resetting to Home threw the user out
        of whatever they were reading every time a flaky server bounced."""
        if self._browser is None:
            return
        try:
            source = PlayerGateway().rebuild_source()
        except Exception:
            log.debug("rebuild after connect failed", exc_info=True)
            return
        if source is not None:
            self._browser.set_source(source, server_uuid=self._browser.server,
                                     keep_place=True)

    def _connect(self):
        from .repository import LibrarySource
        if not settings.work_offline:
            try:
                clientManager.connect_all()
            except Exception:
                log.error("mpvtk browser connect failed", exc_info=True)
        servers = _collect_servers()
        if self._browser is None:
            return
        if not servers:
            # No live server — browse the downloads instead of dead-ending on
            # the login screen. work_offline always arrives here (the connect
            # above was skipped), which is what makes the setting mean
            # something in this UI.
            offline = PlayerGateway().offline_source()
            if offline is not None:
                self._browser.set_source(offline)
                return
            # Nothing downloaded either. If saved servers exist this is a
            # failed connect, so say so on the connecting screen and leave
            # the retry there; a first run with no accounts wants the login
            # form.
            if _saved_servers_exist():
                log.warning("mpvtk browser: no servers connected")
                self._browser.connect_failed()
                return
            log.warning("mpvtk browser: no servers configured; showing login")
            self._browser.show_login()
            return
        source = LibrarySource(servers, clientManager.device_id,
                               settings.player_name,
                               not settings.ignore_ssl_cert)
        self._browser.set_source(source)

    def stop(self):
        from ..player import playerManager
        if self._tray is not None:
            self._tray.stop()
        playerManager.mpvtk_active = False
        app, self._app = self._app, None
        if app is not None:
            app.quit()
            # quit() only enqueues; wait for the loop to stop pushing scenes
            # before anything frees what those scenes reference.
            self._join_render_loop()
        if self._browser is not None:
            # free_bitmaps=False: mpv may still be alive here (the caller is
            # on its way to terminating it), and on libmpv it composites the
            # tile buffers by address. They are released by
            # on_mpv_terminated, or reclaimed with the process.
            self._browser.shutdown(free_bitmaps=False)


user_interface = UserInterface()
