import logging
import threading
import os

import jellyfin_apiclient_python.exceptions

from .conf import settings
from .player import playerManager
from .utils import Timer

log = logging.getLogger("timeline")


class TimelineManager(threading.Thread):
    def __init__(self):
        self.idleTimer = Timer()
        self.halt = False
        self.trigger = threading.Event()
        self.is_idle = True

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.join()

    def run(self):
        while not self.halt:
            if playerManager.is_active() and (
                not settings.idle_when_paused or not playerManager.is_paused()
            ):
                self.send_timeline()
                self.delay_idle()
            if self.idleTimer.elapsed() > settings.idle_cmd_delay and not self.is_idle:
                if (
                    settings.idle_when_paused
                    and settings.stop_idle
                    and playerManager.has_video()
                ):
                    playerManager.stop()
                if settings.idle_cmd:
                    os.system(settings.idle_cmd)
                self.is_idle = True
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


timelineManager = TimelineManager()
