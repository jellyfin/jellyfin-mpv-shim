"""A failed page fetch must not retry itself on every repaint.

`_fetch` guards on `_pages` and `_page_loading`. On failure neither is set:
`done` never runs, and `always=clear` drops the in-flight marker.
`AsyncRunner.run` then calls `self.invalidate()` in a bare `finally` -- after
every outcome, success or not -- so the next render calls `ensure()`, which
calls `_fetch`, which sees no guard and asks again. A fast 500 or 503 is a
continuous request/repaint loop with no user input, on the shared four-worker
pool that every route load and client mutation queues behind.

Windowed mode has had the answer since it was written: `_win_tried` remembers
the attempt, and `rewindow` clears it on a scroll so a user gesture retries.
Fixed pages got the in-flight set and not the attempted set.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.pagination import Paginator  # noqa: E402


class _Runner:
    """Enough of AsyncRunner: runs inline and invalidates afterwards, which
    is the half that closes the loop."""

    epoch = 1

    def __init__(self):
        self.invalidations = 0

    def run(self, work, on_done, epoch, on_error=None, always=None):
        try:
            try:
                result = work()
            except Exception as exc:
                if on_error is not None:
                    on_error(exc)
                return
            on_done(result)
        finally:
            if always is not None:
                always()
            self.invalidations += 1


class PagedFetchDoesNotRetryForeverTest(unittest.TestCase):

    PS = 12

    def _paginator(self, run):
        return Paginator(
            run,
            content_h=lambda route, size: 600,
            is_current=lambda route: True,
            status=lambda *_a, **_k: None,
            invalidate=lambda: None,
            enabled=lambda: True,
            cols=lambda *_a, **_k: 4,
        )

    def _failing(self, calls):
        def fetch(start, limit):
            calls.append((start, limit))
            raise RuntimeError("503 Service Unavailable")
        return fetch

    def test_a_failing_page_is_asked_for_once(self):
        run = _Runner()
        pages = self._paginator(run)
        route = {"kind": "grid", "_total": 240}
        calls = []
        fetch = self._failing(calls)

        for _ in range(5):          # five repaints
            pages.ensure(route, self.PS, fetch)

        # One for the current page and one for each neighbour that exists;
        # what must not happen is that number growing with every repaint.
        self.assertLessEqual(
            len(calls), 2,
            "the failed page was re-requested on every repaint: %d calls "
            "across five renders, on the shared api pool" % len(calls))

    def test_the_user_moving_pages_retries(self):
        """`go` is the fixed-page equivalent of a scroll, which is what
        clears `_win_tried` in windowed mode. Without this the page is dead
        for the rest of the session."""
        run = _Runner()
        pages = self._paginator(run)
        route = {"kind": "grid", "_total": 240}
        calls = []
        fetch = self._failing(calls)

        pages.ensure(route, self.PS, fetch)
        before = len(calls)
        pages.go(route, 1)
        pages.ensure(route, self.PS, fetch)
        self.assertGreater(len(calls), before,
                           "moving to another page did not retry")

    def test_a_reset_retries(self):
        """A sort/filter change replaces the result set, so the previous
        failure says nothing about the new one."""
        run = _Runner()
        pages = self._paginator(run)
        route = {"kind": "grid", "_total": 240}
        calls = []
        fetch = self._failing(calls)

        pages.ensure(route, self.PS, fetch)
        before = len(calls)
        Paginator.reset(route)
        pages.ensure(route, self.PS, fetch)
        self.assertGreater(len(calls), before, "a reset did not retry")

    def test_a_working_page_still_loads(self):
        """The control: a guard that blocked successful fetches would pass
        the first test and empty every grid."""
        run = _Runner()
        pages = self._paginator(run)
        route = {"kind": "grid", "_total": 240}
        items = [{"Id": str(i)} for i in range(240)]

        def fetch(start, limit):
            return items[start:start + limit], 240

        got = pages.ensure(route, self.PS, fetch)
        self.assertEqual([i["Id"] for i in got], [str(i) for i in range(12)])


if __name__ == "__main__":
    unittest.main()
