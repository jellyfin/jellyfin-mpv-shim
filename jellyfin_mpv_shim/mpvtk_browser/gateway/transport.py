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
        """Stop playback on the way out of the window — plain stop(), NOT
        stop_to_browser(), which re-asserts the browse window we are in the
        middle of releasing."""
        self._act(lambda pm: pm.stop())

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

    def set_volume(self, pct, notify=True):
        self._act(lambda pm: pm.set_volume(float(pct), notify=notify))

    def set_repeat(self, mode):
        self._act(lambda pm: pm.set_repeat(mode))

    def toggle_favorite(self):
        self._act(lambda pm: pm.toggle_current_favorite())
