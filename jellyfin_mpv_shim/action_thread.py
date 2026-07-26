import logging
import threading

from .player import playerManager

log = logging.getLogger("action_thread")


class ActionThread(threading.Thread):
    def __init__(self):
        self.trigger = threading.Event()
        self.halt = False

        threading.Thread.__init__(self)

    # A queued task can block for a long time by design — stop() reports the
    # stop to the server, terminate_transcode() is another round trip, and a
    # server that accepts connections but never answers makes each of those
    # wait out its timeout. That is worth waiting for, but not forever: an
    # unbounded join here means the close-the-window path never reaches the
    # rest of the shutdown, and the app sits with no window and no way to
    # quit it. Whatever the task was doing is best-effort teardown.
    JOIN_TIMEOUT = 15.0

    def stop(self):
        self.halt = True
        self.trigger.set()
        self.join(timeout=self.JOIN_TIMEOUT)
        if self.is_alive():
            log.warning(
                "Action thread did not stop within %.0fs; continuing "
                "shutdown without it. A queued player task is still "
                "running — see the stack dump from the exit watchdog.",
                self.JOIN_TIMEOUT)

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

        # Final drain: tasks queued during shutdown (e.g. the mpv teardown
        # that reports the stop to the server) must still run — exiting on
        # halt alone would silently drop them.
        try:
            playerManager.update()
        except Exception:
            log.exception("Error in final action thread drain.")


actionThread = ActionThread()
