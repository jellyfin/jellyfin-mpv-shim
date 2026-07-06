import logging
import threading

from .player import playerManager

log = logging.getLogger("action_thread")


class ActionThread(threading.Thread):
    def __init__(self):
        self.trigger = threading.Event()
        self.halt = False

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            # This thread pumps every queued player task (stop/next/prev,
            # menu actions, auto-advance); it must never die to a stray
            # exception. The wait below stays outside the try so a repeating
            # error can't become a busy loop.
            try:
                if playerManager.is_active() or force_next:
                    playerManager.update()
            except Exception:
                log.exception("Error in action thread.")

            force_next = False
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()


actionThread = ActionThread()
