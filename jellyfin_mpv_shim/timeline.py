import logging
import threading
import os

import jellyfin_apiclient_python.exceptions

from .conf import settings
from .player import playerManager, _mpv_errors
from .utils import Timer

log = logging.getLogger("timeline")


class TimelineManager(threading.Thread):
    def __init__(self):
        self.idleTimer = Timer()
        self.halt = False
        self.trigger = threading.Event()
        self.is_idle = True
        # Tracks whether we've already told the browser's music bar that
        # playback stopped, so we push "stopped" once on the active→inactive
        # edge (e.g. end of a queue) rather than every idle tick.
        self._pushed_stopped = False

        threading.Thread.__init__(self)

    # Same reasoning as ActionThread.JOIN_TIMEOUT: this thread's loop body
    # posts progress to the server, so an unresponsive server can park it
    # for a full request timeout. Bounded so it cannot hold up the exit.
    JOIN_TIMEOUT = 15.0

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join(timeout=self.JOIN_TIMEOUT)
        if self.is_alive():
            log.warning(
                "Timeline thread did not stop within %.0fs; continuing "
                "shutdown without it.", self.JOIN_TIMEOUT)

    def run(self):
        while not self.halt:
            # This thread must survive anything — if it dies, progress
            # reporting and the idle hooks are silently gone for the rest of
            # the session. The wait stays outside the try so a persistent
            # error can't turn into a busy loop.
            try:
                if playerManager.is_active():
                    self.send_timeline()
                    # Keep the browser's music bar position in sync between the
                    # discrete state-change pushes (the bar interpolates locally
                    # while playing).
                    playerManager.push_playstate()
                    # Persist volume changes (music bar or mpv keys) per type.
                    playerManager._maybe_save_volume()
                    self._pushed_stopped = False
                    if not settings.idle_when_paused or not playerManager.is_paused():
                        if self.is_idle and settings.idle_ended_cmd:
                            os.system(settings.idle_ended_cmd)
                        self.delay_idle()
                if not playerManager.is_active() and not self._pushed_stopped:
                    # Active→inactive edge (stop, or a queue that ran out):
                    # hide the browser's music bar exactly once.
                    playerManager.push_playstate(stopped=True)
                    self._pushed_stopped = True
                if (
                    self.idleTimer.elapsed() > settings.idle_cmd_delay
                    and not self.is_idle
                ):
                    if (
                        settings.idle_when_paused
                        and settings.stop_idle
                        and playerManager.has_video()
                    ):
                        playerManager.stop()
                    if settings.idle_cmd:
                        os.system(settings.idle_cmd)
                    self.is_idle = True
                # Quit mpv after a longer idle period to free the window/GPU;
                # idle_quit() is self-gated and re-opens on the next play.
                if (
                    settings.mpv_idle_quit
                    and not playerManager.is_active()
                    and self.idleTimer.elapsed() > settings.mpv_idle_quit_secs
                ):
                    playerManager.idle_quit()
            except Exception:
                log.exception("Error in timeline thread.")
            if self.trigger.wait(5):
                self.trigger.clear()

    def delay_idle(self):
        self.idleTimer.restart()
        self.is_idle = False

    @staticmethod
    def send_timeline():
        try:
            # Send_timeline sometimes (once every couple hours) gets a 404 response from Jellyfin.
            # Without this try/except that would cause this entire thread to crash keeping it from self-healing.
            playerManager.send_timeline()
        except jellyfin_apiclient_python.exceptions.HTTPException:
            # FIXME: Log this
            pass
        except _mpv_errors:
            pass


timelineManager = TimelineManager()
