"""Transport the now-playing bar drives: play/pause, seek, volume.

Split out of the single 1,154-line ``PlayerGateway``; see
``gateway/__init__.py`` for why the facade is composed rather than nested.
"""

import logging
import time
from .base import GatewayCore

log = logging.getLogger("mpvtk_browser.gateway.transport")


class TransportMixin(GatewayCore):
    def raise_window(self):
        from ...player import playerManager
        playerManager.raise_window()

    # -- client-side decorations -------------------------------------------
    #
    # Not routed through _act: these are window-manager gestures, and the
    # reason _act exists is that the player's lock is held for the whole of a
    # playback start. Making the title bar's Close button queue behind a
    # loading file is exactly the freeze a title bar must not have -- the
    # window furniture has to answer while the app is busy, or it reads as a
    # hang. None of them touch playback state.

    def window_chrome_state(self):
        """``{"controls": bool, "maximized": bool}``. See
        ``WindowMixin.window_controls_wanted`` for why "does this window have
        a title bar" is an mpv property read and not an environment check."""
        from ...player import playerManager
        return playerManager.window_chrome_state()

    def minimize_window(self):
        from ...player import playerManager
        playerManager.minimize_window()

    def toggle_window_maximized(self):
        from ...player import playerManager
        playerManager.toggle_window_maximized()

    def close_window(self):
        """Close the window the way mpv's own close button does, so
        close_to_tray and the no-tray safeguard are consulted once, in one
        place (mpvtk_browser.ui.on_window_closed)."""
        from ...player import playerManager

        handler = playerManager.on_window_closed
        if handler is None:
            # No in-window UI owning the window -- same fallback the player's
            # CLOSE_WIN handler takes.
            self._act(lambda pm: pm.stop_and_close())
            return
        handler()

    def refresh_playstate(self):
        """Re-push the now-playing snapshot (the bar's 1s clock tick)."""
        from ...player import playerManager
        playerManager.push_playstate()

    def toggle_pause(self):
        self._act(lambda pm: pm.toggle_pause())

    def stop(self):
        # The now-playing bar's stop button must not take the window with it:
        # stop_and_close() drops force_window, which closed the library out
        # from under the bar that was just clicked.
        self._act(lambda pm: pm.stop_to_browser())

    def stop_for_close(self):
        """Stop playback on the way out of the window — NOT stop_to_browser(),
        which re-asserts the browse window we are in the middle of releasing.

        stop_for_window_close rather than stop(): closing the window leaves a
        SyncPlay group outright, because there is no library left to reach the
        SyncPlay menu from."""
        self._act(lambda pm: pm.stop_for_window_close())

    def next(self):
        self._act(lambda pm: pm.play_next())

    def prev(self):
        self._act(lambda pm: pm.play_prev())

    @staticmethod
    def _ui_seek(pm):
        # HUD-originated seeks are exempt from seek-to-skip-intro for a
        # couple of seconds (scrubbing must not warp to the end of the
        # intro) — the same exemption the lua OSC requested by message.
        pm._last_ui_seek_time = time.time()

    def seek(self, secs):
        def do(pm):
            self._ui_seek(pm)
            pm.seek(float(secs), absolute=True)
        self._act(do)

    def seek_relative(self, secs):
        """Relative seek for the HUD's step buttons (±10s/±30s)."""
        def do(pm):
            self._ui_seek(pm)
            pm.seek(float(secs))
        self._act(do)

    def kb_seek(self, action):
        """Seek as the keyboard's arrow ``action`` ("up"/"down"/"left"/
        "right") would, for the game controller's right stick.

        Not `seek_relative`: the distance is not ours to pick. `kb_seek`
        reads it out of whatever the user has bound that arrow to in their
        own input.conf, applies use_web_seek, and routes through a seek the
        SyncPlay group is told about."""
        self._act(lambda pm: pm.kb_seek(action))

    def remote_action(self, action):
        """Run a Jellyfin remote-control action ("menu", "home", ...).

        The gateway's door onto `menu_action`, which is the one place that
        knows a given action means different things with the library up and
        over a playing video."""
        self._act(lambda pm: pm.menu_action(action))

    def set_volume(self, pct, notify=True):
        self._act(lambda pm: pm.set_volume(float(pct), notify=notify))

    def set_repeat(self, mode):
        self._act(lambda pm: pm.set_repeat(mode))

    def toggle_favorite(self):
        self._act(lambda pm: pm.toggle_current_favorite())
