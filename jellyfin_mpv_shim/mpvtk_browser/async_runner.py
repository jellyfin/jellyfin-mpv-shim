"""Off-thread work with epoch-guarded staleness.

Extracted from ``MpvtkBrowser`` (step 2 of ``docs/ARCHITECTURE_TARGET.md``
§3). The browser previously owned the epoch counter, its lock, and the
worker pool as three attributes among seventy-three; they are one mechanism
and now have one owner.

**The epoch.** A monotonic counter meaning "navigation has moved on".
Dispatchers read it on the loop thread and hand it to :meth:`run`, which
drops the result if it no longer matches. That is a property of the app, not
of any one screen, which is why it stays global rather than moving into the
eventual page objects.

Reading the epoch from anywhere is fine and expected. *Advancing* it is
this module's job alone — ``tests/test_source_invariants.py`` enforces that,
because ``app.py`` asserted it in prose for a long time with nothing
checking.

**The lock protects writers from each other, not from the reader.** The
browser's ``build()`` reads route data unlocked. That is deliberate and safe
only because every writer ends with ``invalidate()``, so a torn read is a
one-frame glitch the next build heals. Do not "fix" it by locking the
reader.

**Callback contract** (all three arms have bitten, and
``tests/test_async_runner.py`` pins each):

``on_done(result)``
    Applied only if the epoch still matches. Runs under the lock.

``on_error(exc)``
    Runs when ``work()`` raises, and **is deliberately not epoch-gated**. A
    rollback undoes an optimistic edit in the route dict it captured, or
    clears a paging guard — neither is a claim about what is currently on
    screen. Gating it meant navigating away before the failure landed
    dropped the rollback, so the route kept a change the server had refused
    and showed it again on the way back.

    That puts the burden on the handler: anything in an ``on_error`` that
    touches the live screen must check for itself.

``always()``
    Runs after every outcome — success, failure, *and a result dropped
    because the epoch moved*. Use it for a guard that must not outlive the
    call. ``on_error`` alone is not enough: a stale success calls neither
    callback, so a flag cleared only in ``on_done`` stays set forever.

Every callback is individually guarded because they run on a pool worker,
where an escaping exception kills the thread with no caller to see it.
"""

import logging
import threading

from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("mpvtk_browser.async_runner")

#: Pool width. Four is enough to overlap a route load with the client
#: mutations a screen issues, and small enough that a slow server cannot
#: fan out into dozens of sockets. Jobs that can take minutes must not run
#: here at all — see ``MpvtkBrowser._run_long``.
WORKERS = 4


class AsyncRunner:
    """Owns the epoch, its lock, and the worker pool."""

    def __init__(self, invalidate=None, workers=WORKERS,
                 thread_name_prefix="mpvtk-api"):
        #: Called with no arguments after every job, to wake the render loop.
        #: Settable so the browser can hand over a bound method built after
        #: the runner exists.
        self.invalidate = invalidate or (lambda: None)
        self._epoch = 0
        self._lock = threading.RLock()
        self._pool = ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix=thread_name_prefix)

    # -- epoch -------------------------------------------------------------

    @property
    def epoch(self):
        """Current epoch. Read it on the loop thread and pass it down; do not
        re-read it inside a worker, which would be racing the navigation the
        value is meant to guard."""
        return self._epoch

    def bump(self):
        """Invalidate every in-flight result. Returns the new epoch."""
        with self._lock:
            self._epoch += 1
            return self._epoch

    # -- the pool ----------------------------------------------------------

    @property
    def pool(self):
        """The executor. Exposed because a few callers submit bare jobs that
        need no epoch guard (the cast compositor, a fire-and-forget client
        mutation), and because tests substitute a synchronous stand-in."""
        return self._pool

    @pool.setter
    def pool(self, value):
        self._pool = value

    def submit(self, fn):
        """Run ``fn()`` off the loop thread with no epoch guard and no
        callbacks. For work whose result nothing waits on."""
        return self._pool.submit(fn)

    def shutdown(self, wait=False, cancel_futures=True):
        self._pool.shutdown(wait=wait, cancel_futures=cancel_futures)

    # -- the main entry point ----------------------------------------------

    def run(self, work, on_done, epoch, on_error=None, always=None):
        """Run ``work()`` off the loop thread; apply ``on_done(result)`` only
        if ``epoch`` still matches. See the module docstring for the full
        contract — every clause of it is load-bearing."""
        def task():
            try:
                try:
                    result = work()
                except Exception as exc:
                    log.warning("async work failed", exc_info=True)
                    if on_error is None:
                        return
                    with self._lock:
                        try:
                            on_error(exc)
                        except Exception:
                            log.warning("async on_error failed", exc_info=True)
                    return
                with self._lock:
                    if epoch != self._epoch:
                        return  # superseded by a newer navigation
                    try:
                        on_done(result)
                    except Exception:
                        log.warning("async on_done failed", exc_info=True)
            finally:
                if always is not None:
                    with self._lock:
                        try:
                            always()
                        except Exception:
                            log.warning("async always failed", exc_info=True)
                self.invalidate()

        try:
            self._pool.submit(task)
        except RuntimeError:
            # The pool is shut down -- the app is on its way out, or a test
            # harness closed it before a last render. Dropping the work is
            # right either way, but `always` still has to run: it is what
            # releases the caller's in-flight guard, and a guard left set
            # outlives the pool on the route dict.
            log.debug("pool is shut down; dropping async work")
            if always is not None:
                try:
                    always()
                except Exception:
                    log.warning("async always failed", exc_info=True)
