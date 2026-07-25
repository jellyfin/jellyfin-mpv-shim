"""``run_async``'s failure handling — the paths hand-testing never reaches.

Written before extracting the async machinery (step 2 of
docs/ARCHITECTURE_TARGET.md §3), per the recipe in
docs/REFACTORING_METHOD.md §2: measure first, and anything at zero that the
step touches gets a test before it moves. Coverage put every one of these
lines at zero — they are the ``except`` arms inside ``run_async``, and a
refactor that quietly dropped one would look green.

Each rule here is load-bearing and each is recorded in ``run_async``'s
docstring as a bug that actually happened:

* a failure with no ``on_error`` must not take the worker down, or the pool
  loses a thread per failure;
* ``on_error`` is deliberately NOT epoch-gated, because a rollback is about
  the route dict it captured, not about what is on screen;
* ``always`` runs on every outcome *including a result dropped for
  staleness*, because a flag cleared only in ``on_done`` otherwise stays set
  forever;
* a callback that itself raises must be contained — it runs on a pool
  worker, so an escape kills the thread rather than surfacing anywhere.
"""

import sys
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from tests._shell_harness import FakeSource  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402


def _browser():
    return MpvtkBrowser(app=None, source=FakeSource())


def _boom(*_a, **_k):
    raise RuntimeError("boom")


class TestFailureReachesOnError(unittest.TestCase):
    def test_on_error_receives_the_exception(self):
        b = _browser()
        seen = []
        b.run_async(_boom, lambda r: seen.append(("done", r)), b._epoch,
                    on_error=lambda exc: seen.append(("error", exc)))
        b._pool.shutdown(wait=True)
        self.assertEqual([k for k, _v in seen], ["error"])
        self.assertIsInstance(seen[0][1], RuntimeError)

    def test_a_failure_without_on_error_is_survivable(self):
        """No handler is a valid call; it must log and move on.

        If the exception escaped the task it would kill a pool worker, and
        with four of them a handful of unreachable-server calls would leave
        the browser unable to load anything at all.
        """
        b = _browser()
        b.run_async(_boom, lambda r: None, b._epoch)
        b._pool.shutdown(wait=True)
        # The pool must still work afterwards.
        b2 = _browser()
        got = []
        b2.run_async(lambda: "ok", got.append, b2._epoch)
        b2._pool.shutdown(wait=True)
        self.assertEqual(got, ["ok"])

    def test_on_error_runs_even_when_the_epoch_moved(self):
        """Deliberately not epoch-gated. A rollback undoes an optimistic edit
        in the route dict it captured, or clears a paging guard — neither is
        a claim about what is currently on screen. Gating it meant navigating
        away before the failure landed dropped the rollback, so the route kept
        a change the server had refused."""
        b = _browser()
        seen = []
        gate, release = threading.Event(), threading.Event()

        def work():
            gate.set()
            release.wait(2.0)
            raise RuntimeError("refused")

        b.run_async(work, lambda r: None, b._epoch,
                    on_error=lambda exc: seen.append(exc))
        self.assertTrue(gate.wait(2.0))
        b._bump_epoch()               # navigate away mid-flight
        release.set()
        b._pool.shutdown(wait=True)
        self.assertEqual(len(seen), 1, "the rollback was dropped")


class TestAlwaysRunsOnEveryOutcome(unittest.TestCase):
    def test_after_success(self):
        b = _browser()
        marks = []
        b.run_async(lambda: 1, lambda r: None, b._epoch,
                    always=lambda: marks.append("always"))
        b._pool.shutdown(wait=True)
        self.assertEqual(marks, ["always"])

    def test_after_failure(self):
        b = _browser()
        marks = []
        b.run_async(_boom, lambda r: None, b._epoch,
                    always=lambda: marks.append("always"))
        b._pool.shutdown(wait=True)
        self.assertEqual(marks, ["always"])

    def test_after_a_result_dropped_for_staleness(self):
        """The case ``on_error`` alone does not cover, and the reason
        ``always`` exists: a stale success calls neither callback, so a guard
        cleared only in ``on_done`` stays set forever. That was _page_more's
        ``_loading``, which silently killed infinite scroll for a route once
        you paged and then clicked into an item."""
        b = _browser()
        marks = []
        gate, release = threading.Event(), threading.Event()

        def work():
            gate.set()
            release.wait(2.0)
            return "value"

        applied = []
        b.run_async(work, applied.append, b._epoch,
                    always=lambda: marks.append("always"))
        self.assertTrue(gate.wait(2.0))
        b._bump_epoch()
        release.set()
        b._pool.shutdown(wait=True)
        self.assertEqual(applied, [], "the stale result should be dropped")
        self.assertEqual(marks, ["always"], "the guard was never released")


class TestARaisingCallbackIsContained(unittest.TestCase):
    """Callbacks run on a pool worker. An escape kills the thread silently —
    there is no caller to see it — so each is individually guarded."""

    def _still_works(self, b):
        got = []
        b.run_async(lambda: "ok", got.append, b._epoch)
        b._pool.shutdown(wait=True)
        return got == ["ok"]

    def test_a_raising_on_done_does_not_kill_the_worker(self):
        b = _browser()
        b.run_async(lambda: "value", _boom, b._epoch)
        self.assertTrue(self._still_works(b))

    def test_a_raising_on_error_does_not_kill_the_worker(self):
        b = _browser()
        b.run_async(_boom, lambda r: None, b._epoch, on_error=_boom)
        self.assertTrue(self._still_works(b))

    def test_a_raising_always_does_not_kill_the_worker(self):
        b = _browser()
        b.run_async(lambda: "value", lambda r: None, b._epoch, always=_boom)
        self.assertTrue(self._still_works(b))

    def test_always_still_runs_when_on_done_raised(self):
        # The finally: is what guarantees this. Without it a view that
        # throws while applying its data also leaks its loading guard.
        b = _browser()
        marks = []
        b.run_async(lambda: "value", _boom, b._epoch,
                    always=lambda: marks.append("always"))
        b._pool.shutdown(wait=True)
        self.assertEqual(marks, ["always"])


class TestTheEpochIsMonotonic(unittest.TestCase):
    def test_bump_returns_the_new_value_and_advances(self):
        b = _browser()
        first = b._bump_epoch()
        second = b._bump_epoch()
        self.assertEqual(second, first + 1)
        self.assertEqual(b._epoch, second)

    def test_concurrent_bumps_do_not_collide(self):
        """`+= 1` is a read-modify-write. The lock is what stops two threads
        landing on the same epoch, which would let a stale result through."""
        b = _browser()
        seen, lock = [], threading.Lock()

        def bump():
            value = b._bump_epoch()
            with lock:
                seen.append(value)

        threads = [threading.Thread(target=bump) for _ in range(24)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(set(seen)), len(seen),
                         "two bumps returned the same epoch")


if __name__ == "__main__":
    unittest.main()
