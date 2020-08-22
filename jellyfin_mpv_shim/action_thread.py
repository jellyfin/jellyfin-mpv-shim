import threading

from .player import playerManager


class ActionThread(threading.Thread):
    def __init__(self):
        self.trigger = threading.Event()
        self.halt = False

        threading.Thread.__init__(self)

    def stop(self):
        self.halt = True
        self.join()

    def run(self):
        force_next = False
        while not self.halt:
            if playerManager.is_active() or force_next:
                playerManager.update()

            force_next = False
            if self.trigger.wait(1):
                force_next = True
                self.trigger.clear()


actionThread = ActionThread()
