"""A real comic, in a real window, shown by a real mpv.

The comic reader is the one screen in the browser that cannot be walked by
`tests/e2e/test_route_walk.py`, and the reason is the design rather than an
accident of the harness. An epub page is *drawn* — Pillow makes a bitmap and
the browser pushes it through the same transport as a tile strip — so the
reader can be opened, paginated and rendered with no player in the process at
all. A comic page is *played*: `ComicPage._show_page` hands the extracted file
to `ctx.player.show_picture`, which goes through the gateway's `_act`, which
imports `player.py`, which selects an mpv backend and opens a window. The
contract tier is defined by not doing that, so `comic` is excused there and
walked here instead — and that excuse is asserted against this file existing,
not merely commented.

What a real player buys, beyond "the route builds":

* **The picture actually reaches mpv.** `show_picture` is fire-and-forget
  through `run_action`, so a page that never loaded looks identical from the
  browser's side — the route says `_showing`, the bar draws "3 of 15", and
  the window is empty. The assertion is on `path`, mpv's own property.
* **`keepaspect`, which is the trap this feature has twice.** The library
  window turns it *off* so it can resize freely, and with it off mpv stretches
  whatever is loaded to fill the window — the page comes out distorted and
  `video-zoom` has no visible effect, because a stretched picture already
  fills the window at every zoom. `show_picture` turns it back on. Nothing
  without a real mpv can see the property at all; a fake stores whatever it
  is handed and agrees.
* **The third window state holds.** A comic is "browsing" and "something is
  loaded" at once, which is neither of the two states `PlayerManager` was
  built around. It is safe only because `_video` stays None — that is what
  keeps `_on_eof_reached` from advancing a queue, keeps the timeline from
  reporting a session, and keeps `idle_quit` from firing. Asserted directly,
  because every one of those consequences is silent: a comic that quietly set
  `_video` would report phantom playback to the server and look, from inside
  the app, entirely fine.

Deliberately *not* here: the pan arithmetic and the end-of-page interlock.
`tests/test_picture_view.py` pins the measured pan unit and
`tests/lua/test_renderer.lua` the wheel behaviour, both far more cheaply than
a real window can. This module is about the handoff those two assume.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

LIBRARY = "Books"

#: The comic walked here. Named rather than found by shape for the reason
#: `test_books` gives: a `.cbz` comes back as an ordinary `Book`, so "the
#: first book" is a coin toss between a comic and a novel. Any `.cbz` will do
#: for a walk, so an unnamed one is accepted as a fallback.
COMIC = "A Test Comic 001"

SIZE = (1280, 720)


class _SyncPool:
    """Run route loaders inline, so a fetch completes before the render.

    The same substitution `test_route_walk` makes, and for the same reason:
    the work is a *real* request and a *real* extraction, and running it
    inline is what lets an assertion follow the navigation instead of racing
    it. What is not faked is anything below — the archive, the file, mpv.
    """

    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


@_e2e.require_server_and_mpv
class ComicReaderTest(_e2e.E2ETestCase):

    def setUp(self):
        super().setUp()
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.gateway import PlayerGateway

        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)
        self.source.get_libraries(_e2e.SOURCE_UUID)
        self.comic = self._comic()
        self._download_into_a_temp_store(self.comic)

        # `app=None`: this module asserts on mpv's state, not on composited
        # pixels, and a renderer would add a second owner of the window for
        # no assertion's sake. `build()` is still driven directly below, so
        # the page's own render path runs.
        self.browser = MpvtkBrowser(app=None, source=self.source,
                                    server_uuid=_e2e.SOURCE_UUID,
                                    controller=PlayerGateway())
        # __init__ already kicked the home load onto the real threaded pool;
        # drain it before swapping, or it lands after the session is gone.
        self.browser._async._pool.shutdown(wait=True, cancel_futures=True)
        self.browser._pool = _SyncPool()
        self.addCleanup(self._shutdown_browser)

        # The library owns the window before a comic is opened, which is
        # where `keepaspect=False` comes from. Entering it is not scene
        # setting — it is the precondition for the stretch bug.
        self.pm.set_browse_window(True)
        self.addCleanup(self._leave_browse_window)

    def _shutdown_browser(self):
        try:
            self.browser.shutdown()
        except Exception:
            pass

    def _leave_browse_window(self):
        try:
            self.pm.clear_picture()
            self.pm.reset_picture_view()
            self.pm.set_browse_window(False)
        except Exception:
            pass

    # -- fixtures ----------------------------------------------------------

    def _comic(self):
        books = self.session.find_all(library=LIBRARY, item_type="Book",
                                      fields="Path")
        cbz = [b for b in books
               if (b.get("Path") or "").lower().endswith(".cbz")]
        if not cbz:
            self.skipTest("no .cbz in %r — this library predates stdjflib's "
                          "book fixtures" % LIBRARY)
        named = [b for b in cbz if b.get("Name") == COMIC]
        return (named or cbz)[0]

    def _download_into_a_temp_store(self, item):
        """Fetch `item` for real, into a store the **singleton** points at.

        The singleton, because that is what `book_download_state` reads
        (gateway/downloads.py) — a `SyncManager()` of our own would be
        invisible to the page, which would then render "Getting the comic…".
        Downloaded rather than stubbed because `/Items/{id}/Download` is the
        only endpoint that yields a book's bytes at all, and because a stub
        supplying the path would remove both halves of what is under test.
        """
        import shutil
        import tempfile
        from unittest import mock

        from jellyfin_mpv_shim.sync.db import SyncDB
        from jellyfin_mpv_shim.sync.manager import syncManager

        root = tempfile.mkdtemp(prefix="jms-e2e-comic-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        db = SyncDB(os.path.join(root, "catalog.db"))
        self.addCleanup(db.close)
        for attr, value in (("db", db), ("root", root),
                            ("get_client", lambda uuid: self.session.client)):
            patcher = mock.patch.object(syncManager, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

        self.assertEqual(
            syncManager.enqueue(_e2e.SOURCE_UUID, item["Id"], "Book",
                                include_watched=True), 1,
            "the comic was not enqueued, so there is nothing to open")
        syncManager._download(db.get(item["Id"]))
        row = db.get(item["Id"])
        self.assertEqual(row["status"], "complete",
                         "the comic did not download, so the reader would be "
                         "opened against a missing file")

    # -- driving the reader ------------------------------------------------

    def _open(self):
        """Navigate to the comic route and render it, as a frame would."""
        from jellyfin_mpv_shim.mpvtk.layout import layout

        self.browser.navigate({"kind": "comic", "server": _e2e.SOURCE_UUID,
                               "item_id": self.comic["Id"],
                               "title": self.comic.get("Name", "")})
        route = self.browser.route
        self.assertEqual(route.get("kind"), "comic",
                         "the comic route did not become the screen")
        self.assertIsNone(route.get("_error"),
                          "the comic failed to open: %s" % route.get("_error"))
        self.assertFalse(
            route.get("_opening"),
            "the comic is still opening, so the screen is the 'Getting the "
            "comic…' placeholder rather than a page")
        self.assertIsNotNone(
            route.get("_comic"),
            "the route built with no open archive — that is the placeholder, "
            "which builds perfectly and proves nothing")
        tree = self.browser.build(SIZE)
        nodes, _handlers = layout(tree, *SIZE)
        self.assertTrue(nodes, "the comic reader drew nothing at all")
        return route

    def _loaded_path(self):
        """What mpv actually has open, or None while it has nothing.

        `path` is the property rather than `filename` because the archive
        extracts to a temp directory and the test needs to know *which* file
        landed there, not merely that something did.
        """
        return _e2e.wait_for(lambda: self.pm._player.path or None, timeout=15)

    # -- the tests ---------------------------------------------------------

    def test_the_page_reaches_mpv(self):
        """The walk `test_route_walk` cannot do, plus the half that matters.

        A route that says it is showing a page proves nothing on its own:
        `show_picture` is fire-and-forget through `run_action`, so every
        browser-side observable looks the same whether or not mpv ever got
        the file.
        """
        route = self._open()
        self.assertTrue(route.get("_showing"),
                        "the reader never asked for a page to be shown")

        archive = route["_comic"]
        expected = archive.page_path(route["_page"])
        loaded = self._loaded_path()
        self.assertEqual(
            os.path.basename(loaded or ""), os.path.basename(expected),
            "mpv is not holding the page the reader extracted — the window "
            "is empty while the reader draws its bar over it")

    def test_the_page_keeps_its_shape(self):
        """`keepaspect`, which is off for the library and must be on here.

        The library window turns it off so it can resize freely; with it off
        mpv stretches whatever is loaded to fill the window, so the page is
        distorted *and* `video-zoom` does nothing visible. Both symptoms,
        one property, and no fake has it.
        """
        self.assertFalse(
            self.pm._player.keepaspect,
            "the library window should have keepaspect off — the "
            "precondition for this bug is missing, so a pass here would "
            "mean nothing")
        self._open()
        self._loaded_path()
        self.assertTrue(
            self.pm._player.keepaspect,
            "the page is being stretched to the window: it is distorted, "
            "and video-zoom will appear to do nothing")

    def test_a_comic_is_not_playback(self):
        """`_video` stays None, which is what makes the third window state
        safe at all.

        Every consequence of getting this wrong is silent from inside the
        app: a queue that advances on the picture's EOF, a timeline that
        reports a session for a comic, an idle-quit that fires or does not.
        Asserting the cause is the only cheap version.
        """
        self.assertIsNone(self.pm._video, "something was playing already")
        route = self._open()
        self._loaded_path()
        self.assertIsNone(
            self.pm._video,
            "the comic registered as playback — a queue can now advance off "
            "its EOF and the timeline will report a session for it")
        self.assertIsNone(route.get("_error"))

    def test_turning_a_page_loads_a_different_file(self):
        """The observable a page turn actually has.

        Asserted over three turns rather than one. The recurring bug shape
        here is state feeding back into the input that produced it, and the
        page index is exactly that — `_show_page` clamps against the
        archive, writes `_page`, and the next turn reads it back. A
        one-step test cannot see an index that stops moving, and the Fit
        Page interlock bug was precisely a turn that worked once and then
        went dead.
        """
        route = self._open()
        archive = route["_comic"]
        if archive.page_count < 4:
            self.skipTest("need four pages to turn three times; this comic "
                          "has %d" % archive.page_count)

        # Through `on_key`, which is how a turn actually arrives: RIGHT is a
        # *claimed* key on this page (`claimed_keys`), so the press reaches
        # the page rather than the spatial navigator. Calling `_turn`
        # directly would skip the only mapping that can be wrong.
        page = self.browser._page_for(route)
        seen = [self._loaded_path()]
        for turn in range(3):
            before = route["_page"]
            page.on_key("RIGHT")
            self.assertEqual(
                route["_page"], before + 1,
                "turn %d did not advance the page index" % (turn + 1))
            self.browser.build(SIZE)
            loaded = _e2e.wait_for(
                lambda p=seen[-1]: (self.pm._player.path or None)
                if self.pm._player.path != p else None, timeout=15)
            self.assertIsNotNone(
                loaded,
                "turn %d changed the reader's page number but mpv is still "
                "holding the previous file" % (turn + 1))
            seen.append(loaded)

        names = [os.path.basename(s or "") for s in seen]
        self.assertEqual(len(set(seen)), len(seen),
                         "the same file was loaded twice while turning "
                         "forward: %s" % names)


if __name__ == "__main__":
    unittest.main()
