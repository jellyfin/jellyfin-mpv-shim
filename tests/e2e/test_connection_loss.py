"""The server stops answering while you are looking at the library.

Three ways it happens, and they are not the same failure: the box goes away
(connection refused), the token stops being accepted (401), and — the one
that matters most — it happens *after* a screenful has already drawn, while
paging further in.

That last case has a rule the other two do not, and it is the one worth
holding on to: **a failure while paging must not take away what is already on
screen.** The first page is real data the user is reading. Replacing it with
an error panel because the *second* page failed loses a screenful to a
problem that did not touch it. So `Paginator.window` raises a toast and
leaves the items alone, while a load that has nothing at all to show sets
`_error` and offers Retry.

The other rule is a rate one. Windows are requested from **render**, because
which items are visible is a question about geometry — so a page that
re-requested on failure would issue one request per frame for as long as the
server stayed down, and the toast it raises is itself a repaint. One attempt
per page per scroll (`_win_tried`), cleared by `rewindow` when the user
moves.

None of this can be seen against a fake: a fake answers, so there is no
failure to recover from, and the failure the app has to survive is a real
socket error raised from inside a worker thread. The server here is real and
the connection to it is genuinely broken — by pointing the client at a closed
port, which is what a stopped server looks like, and by having an admin
revoke the device's session, which is what a signed-out client looks like.
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SIZE = (1280, 720)
#: A port nothing listens on. Refused immediately, which is what a stopped
#: server does; a blackholed address would instead time out, which is a
#: different failure with a different (much slower) shape.
DEAD = "http://127.0.0.1:1"

#: Loggers that narrate a failed request. Both spellings of the apiclient's
#: root are here on purpose: it uses `'JELLYFIN.' + __name__` in five modules
#: and `'Jellyfin.' + __name__` in http.py, and logger names are
#: case-sensitive, so silencing the shouted one alone leaves every connection
#: error on the console.
_NOISY = ("Jellyfin", "JELLYFIN", "mpvtk_browser.async_runner",
          "mpvtk_browser.repository", "mpvtk_browser.app")

BIG_LIBRARY = "Bulk Movies"
SMALL_LIBRARY = "Movies"


class _SyncPool:
    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


class _BrowserCase(unittest.TestCase):
    """A real browser over a real source, with its worker pool made
    synchronous so a load has finished by the time navigate() returns."""

    account = "qa-user"

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk.layout import layout

        self.layout = layout
        self.session = _e2e.Session(self.account)
        self.addCleanup(self._stop_session)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)
        self.libraries = {lib["Name"]: lib
                          for lib in self.source.get_libraries(_e2e.SOURCE_UUID)}

        self.browser = MpvtkBrowser(app=None, source=self.source,
                                    server_uuid=_e2e.SOURCE_UUID)
        self.browser._async._pool.shutdown(wait=True, cancel_futures=True)
        self.browser._pool = _SyncPool()
        self.addCleanup(self._shutdown)
        self.conn = self.source._conn(_e2e.SOURCE_UUID)
        self.address = self.conn.client.config.data["auth.server"]
        self.addCleanup(self._revive)

    def _stop_session(self):
        try:
            self.session.stop()
        except Exception:
            pass

    def _shutdown(self):
        try:
            self.browser.shutdown()
        except Exception:
            pass

    def _revive(self):
        self.conn.client.config.data["auth.server"] = self.address

    # -- driving -----------------------------------------------------------

    def _kill(self):
        """The server stops answering, mid-session.

        Every failure below is deliberate, and both the apiclient and the
        async runner log the whole traceback for one — three screens of it
        per test, on a run where nothing is wrong. Silenced from here so
        that a traceback appearing in this suite's output still means
        something.
        """
        for name in _NOISY:
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.CRITICAL)
        self.conn.client.config.data["auth.server"] = DEAD

    def _goto(self, name):
        library = self.libraries.get(name)
        if library is None:
            self.skipTest("no %r library" % name)
        self.browser.navigate({
            "kind": "grid", "server": _e2e.SOURCE_UUID,
            "parent_id": library["Id"], "title": name,
            "collection_type": library.get("CollectionType")})
        return library

    def _render(self):
        return self.layout(self.browser.build(SIZE), *SIZE)

    def _loaded(self):
        return sum(1 for x in (self.browser.route.get("_items") or [])
                   if x is not None)

    def _count_requests(self):
        """Count HTTP calls to the server from here on. Returns a getter."""
        http = self.conn.client.http
        original = http.request
        calls = []

        def counted(*a, **k):
            calls.append(1)
            return original(*a, **k)

        http.request = counted
        self.addCleanup(setattr, http, "request", original)
        return lambda: len(calls)


@_e2e.require_server
class ServerGoneTest(_BrowserCase):

    def test_a_load_with_nothing_to_show_says_so_and_offers_a_retry(self):
        """Without `_error` the route's data stays None and the view spins
        forever, so an unreachable server reads as a hang."""
        self._kill()
        self._goto(SMALL_LIBRARY)
        self.assertTrue(
            self.browser.route.get("_error"),
            "a grid that could not load anything set no error, so the screen "
            "spins with no retry and no explanation")
        _nodes, handlers = self._render()
        self.assertIn(
            "route-retry", handlers,
            "no Retry button on a failed load — the only way out is Back")

    def test_retry_recovers_once_the_server_is_back(self):
        """The button has to actually re-issue the load, not just clear the
        error and leave an empty screen."""
        self._kill()
        self._goto(SMALL_LIBRARY)
        _nodes, handlers = self._render()
        self.assertIn("route-retry", handlers)

        self._revive()
        handlers["route-retry"]["click"]()
        self.assertIsNone(self.browser.route.get("_error"),
                          "the error survived a successful retry")
        self.assertGreater(
            self._loaded(), 0,
            "Retry cleared the error but loaded nothing, so the library is "
            "blank rather than broken — which is worse")

    def test_navigating_on_while_down_does_not_crash_the_browser(self):
        """A dead server must not make the shell unusable. Every route the
        user can reach from a failed one still has to build."""
        self._kill()
        self._goto(SMALL_LIBRARY)
        for route in ({"kind": "home", "server": _e2e.SOURCE_UUID},
                      {"kind": "search", "server": _e2e.SOURCE_UUID,
                       "query": "standard"},
                      {"kind": "settings", "server": _e2e.SOURCE_UUID}):
            with self.subTest(kind=route["kind"]):
                self.browser.navigate(dict(route))
                self._render()      # must not raise
        self.browser.go_back()
        self._render()


@_e2e.require_server
class PagingWhileDownTest(_BrowserCase):
    """The case with the extra rule: it broke *after* something drew."""

    def setUp(self):
        super().setUp()
        self._goto(BIG_LIBRARY)
        self._render()
        self.first_page = self._loaded()
        if self.first_page < 50 or (self.browser.route.get("_total") or 0) < 500:
            self.skipTest("need a library big enough to page through; got "
                          "%d of %r loaded" % (self.first_page,
                                               self.browser.route.get("_total")))
        self.page = self.browser._page_for(self.browser.route)
        self.assertIsNotNone(self.page, "the grid has no page object")

    def _ask_for_a_far_window(self, first=400, last=460):
        """What the view does when the user scrolls to items it has not got.

        Called directly rather than by faking a scroll offset: the offset is
        owned by the renderer's scroll state, and forging one tests the
        forgery. This is the call render makes.
        """
        self.page._window(first, last)

    def test_a_failed_page_in_keeps_what_is_already_on_screen(self):
        self._kill()
        self._ask_for_a_far_window()
        self._render()
        self.assertEqual(
            self._loaded(), self.first_page,
            "a failed page-in dropped items that had already loaded")
        self.assertIsNone(
            self.browser.route.get("_error"),
            "a failed page-in set the route error, which replaces a "
            "screenful of real data with an error panel")

    def test_it_says_something_rather_than_failing_silently(self):
        self._kill()
        self.browser.set_status("")
        self._ask_for_a_far_window()
        self.assertTrue(
            self.browser.status,
            "paging failed with no message at all, so the grid simply stops "
            "filling in and nothing says why")

    def test_the_tiles_are_still_drawn(self):
        """The point of keeping the items: the screen still works."""
        self._kill()
        self._ask_for_a_far_window()
        _nodes, handlers = self._render()
        self.assertNotIn(
            "route-retry", handlers,
            "the whole grid was replaced by the error panel because a page "
            "further down failed")
        self.assertTrue(
            [i for i in handlers if "click" in handlers[i]],
            "nothing on the grid is clickable any more")

    def test_a_dead_server_is_not_re_asked_every_frame(self):
        """Windows are requested from render, so a retry-on-failure here is
        a request per frame for as long as the server stays down — and the
        toast the failure raises is itself a repaint."""
        self._kill()
        count = self._count_requests()
        for _frame in range(5):
            self._ask_for_a_far_window()
            self._render()
        self.assertEqual(
            count(), 1,
            "five frames against a dead server issued %d requests; one "
            "attempt per page per scroll is the rule, and render is what "
            "drives this" % count())

    def test_moving_again_retries_the_window_that_failed(self):
        """`rewindow` is called on a scroll, which is the cadence: a window
        that failed is retried when the user moves, and never while they hold
        still. Without it the hole stays a hole for the rest of the session.
        """
        self._kill()
        self._ask_for_a_far_window()
        self._render()
        self.assertEqual(self._loaded(), self.first_page)

        self._revive()
        self.browser._pages.rewindow(self.browser.route)   # a scroll
        self._ask_for_a_far_window()
        self.assertGreater(
            self._loaded(), self.first_page,
            "the window that failed while the server was down was never "
            "asked for again, so those tiles stay blank forever")


@_e2e.require_server
class RevokedSessionTest(_BrowserCase):
    """Signed out from elsewhere: the address answers, the token does not.

    A different failure from an unreachable server and, until measured, an
    open question — a 401 that came back as an empty result set would render
    a perfectly calm, perfectly empty library, which is the worst of the
    three outcomes because nothing looks wrong.
    """

    account = "qa-nosyncplay"

    def test_a_revoked_token_reads_as_an_error_not_an_empty_library(self):
        for name in _NOISY:
            logger = logging.getLogger(name)
            self.addCleanup(logger.setLevel, logger.level)
            logger.setLevel(logging.CRITICAL)
        admin = _e2e.Session("qa-admin")
        self.addCleanup(admin.stop)
        admin.purge_devices(self.account)

        self._goto(SMALL_LIBRARY)
        self.assertEqual(
            self._loaded(), 0,
            "items came back after the session was revoked")
        # The message does not distinguish this from a network failure
        # ("Failed to load. Check the connection."), which is worth
        # improving; what matters here is that it is an error at all rather
        # than a library that looks empty.
        self.assertTrue(
            self.browser.route.get("_error"),
            "a revoked session rendered as an empty library with no error, "
            "so the user sees a server that has lost their media")
        _nodes, handlers = self._render()
        self.assertIn("route-retry", handlers)


if __name__ == "__main__":
    unittest.main()
