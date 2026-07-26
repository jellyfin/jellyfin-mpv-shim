"""Serialized, off-thread delivery of session reports to the server.

Jellyfin models a queue advance as PlaybackStopped for the outgoing item
followed by PlaybackStart for the incoming one, and the server force-pushes
its whole session list to every subscribed client on *both*. Two consequences
shape this module:

* Those calls must not sit on the playback path. Reporting a stop and then
  resolving the next item took two blocking round trips between the last
  sample of one track and the first of the next — on a remote server that is
  the bulk of the audible gap, and it is also how long every remote control
  shows the session as playing nothing.

* They must still arrive in order. The server clears NowPlayingItem and
  replaces PlayState wholesale on a stop, so a stop that overtook the
  following start would leave every subscribed client showing a blank session
  while playback continues — and the server stops its own synthetic progress
  timer on a stop, so nothing would correct it until the next progress tick.

Hence one worker, not a thread per call: submissions run in submission order,
and the caller never blocks. A thread-per-call would have been simpler and
would have raced.
"""

import logging
import threading
from queue import Empty, Queue

log = logging.getLogger("session_reporter")


class SessionReporter:
    """A single background worker draining a FIFO of report callables."""

    def __init__(self, name: str = "session-report"):
        self._queue = Queue()
        self._name = name
        self._thread = None
        self._lock = threading.Lock()
        self._stopping = False
        # Set whenever the queue is observed empty by the worker. drain()
        # waits on this rather than polling the queue, so a caller cannot
        # miss the transition.
        self._idle = threading.Event()
        self._idle.set()

    def _ensure_thread(self):
        # Started on first use, not in __init__: constructing a PlayerManager
        # must not spawn threads (tests build one without ever reporting).
        if self._thread is None and not self._stopping:
            self._thread = threading.Thread(
                target=self._run, name=self._name, daemon=True)
            self._thread.start()

    def _run(self):
        while True:
            try:
                item = self._queue.get(timeout=0.5)
            except Empty:
                self._idle.set()
                continue
            if item is None:            # shutdown sentinel
                self._queue.task_done()
                self._idle.set()
                return
            fn, label = item
            try:
                fn()
            except Exception:
                # One failed report must not take the worker with it, or every
                # later report in the session is silently dropped.
                log.debug("%s failed", label, exc_info=True)
            finally:
                self._queue.task_done()
                if self._queue.empty():
                    self._idle.set()

    def submit(self, fn, label: str = "report"):
        """Queue ``fn`` to run on the worker. Never blocks, never raises."""
        with self._lock:
            if self._stopping:
                # Shutting down: run it here rather than dropping it. The
                # final stop report is the one that must not be lost.
                try:
                    fn()
                except Exception:
                    log.debug("%s failed", label, exc_info=True)
                return
            self._idle.clear()
            self._queue.put((fn, label))
            self._ensure_thread()

    def drain(self, timeout: float = 5.0) -> bool:
        """Block until queued reports have been delivered.

        Called on the way out: the worker is a daemon, so without this the
        final stop report would be lost to interpreter exit — the server
        would keep the session marked as playing. Returns whether the queue
        emptied within ``timeout``; a slow or unreachable server must delay
        shutdown by no more than that.
        """
        if self._thread is None:
            return True
        return self._idle.wait(timeout)

    def stop(self, timeout: float = 5.0):
        """Drain, then retire the worker. Later submissions run inline."""
        drained = self.drain(timeout)
        with self._lock:
            self._stopping = True
        if self._thread is not None:
            self._queue.put(None)
            self._thread.join(timeout=1.0)
            self._thread = None
        return drained
