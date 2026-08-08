"""The comic reader as a screen: pages, placement, and the window it borrows.

Against a **real CBZ** and a real ``ComicArchive``, because everything that
could be wrong here is a question about the archive — which file is page 1,
what shape it is, whether the bytes come out. What is faked is the shell and
the player: the picture is mpv's, so "which page is on screen" has no node
in any scene and the fake controller's record of what it was handed is the
only observable there is.

The placement maths (``gateway/picture.py``) is tested separately and
without a window; here the question is only whether this page feeds it the
right numbers and at the right moments.
"""

import io
import os
import tempfile
import unittest
import zipfile

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (FakeController, FakeSource, _DeferredPool,
                                  _SyncPool, build_scene, ids)


def build_cbz(path, pages=4, size=(1400, 2100), names=None):
    """A real CBZ of real JPEGs, with the junk a shipped one carries."""
    from PIL import Image

    names = names or ["comic/p%d.jpg" % (i + 1) for i in range(pages)]
    with zipfile.ZipFile(path, "w") as archive:
        for i, name in enumerate(names):
            buffer = io.BytesIO()
            Image.new("RGB", size, (250 - i, 246, 238)).save(
                buffer, "JPEG", quality=60)
            archive.writestr(name, buffer.getvalue())
        archive.writestr("ComicInfo.xml", "<ComicInfo/>")
        archive.writestr("__MACOSX/._p1.jpg", b"resource fork")
    return path


def comic(item_id="cb1", path="/library/A Comic.cbz", **extra):
    return {"Id": item_id, "Name": "A Comic", "Type": "Book", "Path": path,
            **extra}


class ComicHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cbz = build_cbz(os.path.join(self._tmp.name, "a.cbz"))

    def open_comic(self, state=None, **kw):
        source = FakeSource()
        source.libraries = [{"Id": "lib-books", "Name": "Books",
                             "Type": "CollectionFolder",
                             "CollectionType": "books"}]
        item = comic(**kw)
        source.items[item["Id"]] = item
        browser = MpvtkBrowser(app=None, source=source)
        browser._pool = _SyncPool()
        browser.controller = FakeController()
        browser.server = "srv1"
        browser.controller.book_downloads[item["Id"]] = (
            state if state is not None else ("complete", self.cbz))
        browser.navigate({"kind": "comic", "server": "srv1",
                          "item_id": item["Id"], "title": "A Comic"})
        self.item = item
        return browser

    @staticmethod
    def page(browser):
        return browser._page_for(browser.route)


class TestOpening(ComicHarness):
    def test_a_downloaded_comic_opens_on_its_first_page(self):
        browser = self.open_comic()
        page = self.page(browser)
        self.assertEqual(page.page_count(), 4)
        self.assertEqual(page.page_index(), 0)
        self.assertTrue(browser.controller.pictures,
                        "no page was handed to the player")

    def test_the_page_handed_over_is_a_real_file(self):
        browser = self.open_comic()
        build_scene(browser)
        path = browser.controller.pictures[-1]
        self.assertTrue(os.path.exists(path))
        self.assertGreater(os.path.getsize(path), 0)

    def test_a_comic_that_is_not_downloaded_yet_is_fetched(self):
        browser = self.open_comic(state=(None, None))
        build_scene(browser)
        self.assertTrue(browser.controller.enqueued
                        or browser.controller.opened,
                        "nothing was fetched")

    def test_a_file_that_is_not_a_comic_says_so(self):
        bad = os.path.join(self._tmp.name, "broken.cbz")
        with open(bad, "wb") as handle:
            handle.write(b"not a zip")
        browser = self.open_comic(state=("complete", bad))
        build_scene(browser)
        self.assertTrue(browser.route.get("_error"),
                        "a broken comic opened without complaint")


class TestPaging(ComicHarness):
    def test_turning_pages_hands_over_each_one_in_order(self):
        """Over the whole comic, and against the ARCHIVE's order.

        The extracted filename is built from the index, so asserting
        ``page00000.jpg, page00001.jpg…`` says only that the index counted
        up — it holds whatever order the archive was read in. The reading
        order *is* the natural sort, so the fixture is named to make a
        lexicographic sort visibly wrong (1, 10, 11, 2 instead of
        1, 2, 10, 11) and the assertion follows the bytes.
        """
        names = ["c/p1.jpg", "c/p2.jpg", "c/p10.jpg", "c/p11.jpg"]
        self.cbz = build_cbz(os.path.join(self._tmp.name, "order.cbz"),
                             names=names)
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        self.assertEqual(page.archive.pages, names,
                         "the archive is not in reading order")
        seen = [self._page_bytes(browser)]
        for _i in range(3):
            page._turn(1)
            seen.append(self._page_bytes(browser))
        self.assertEqual(page.page_index(), 3)
        expected = [self._entry_bytes(name) for name in names]
        self.assertEqual(seen, expected,
                         "the pages were handed over in the wrong order")

    def _page_bytes(self, browser):
        with open(browser.controller.pictures[-1], "rb") as handle:
            return handle.read()

    def _entry_bytes(self, name):
        with zipfile.ZipFile(self.cbz) as archive:
            return archive.read(name)

    def test_paging_stops_at_both_ends(self):
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        page._turn(-1)
        self.assertEqual(page.page_index(), 0)
        for _i in range(10):
            page._turn(1)
        self.assertEqual(page.page_index(), 3)

    def test_a_page_entered_backwards_starts_at_its_bottom(self):
        """Paging back and landing at the top means scrolling down to see
        what you went back for, on every page you walk back through."""
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        page._turn(1)
        forward = browser.controller.picture_views[-1]["pan_y"]
        page._turn(-1)
        backward = browser.controller.picture_views[-1]["pan_y"]
        # Less, not greater: video-pan-y moves the PICTURE, so the top of a
        # page is the larger value. Asserting it the other way round is how
        # the sign error this caught survived being written twice.
        self.assertLess(backward, forward,
                        "going back landed at the top of the page")

    def test_the_jump_box_goes_to_that_page(self):
        browser = self.open_comic()
        _nodes, handlers = build_scene(browser)
        handlers["cm-page"]["submit"]("3")
        self.assertEqual(self.page(browser).page_index(), 2)

    def test_a_jump_outside_the_comic_is_clamped_not_obeyed(self):
        browser = self.open_comic()
        _nodes, handlers = build_scene(browser)
        handlers["cm-page"]["submit"]("99")
        self.assertEqual(self.page(browser).page_index(), 3)
        handlers["cm-page"]["submit"]("nonsense")
        self.assertEqual(self.page(browser).page_index(), 3)

    def test_a_turn_key_turns_the_page(self):
        browser = self.open_comic()
        build_scene(browser)
        browser._on_claimed_key = browser._on_claimed_key
        page = self.page(browser)
        page.on_key("RIGHT")
        self.assertEqual(page.page_index(), 1)
        page.on_key("LEFT")
        self.assertEqual(page.page_index(), 0)
        page.on_key("END")
        self.assertEqual(page.page_index(), 3)
        page.on_key("HOME")
        self.assertEqual(page.page_index(), 0)


class TestPlacement(ComicHarness):
    def test_the_first_page_is_fitted_to_the_width(self):
        browser = self.open_comic()
        build_scene(browser)
        view = browser.controller.picture_views[-1]
        self.assertIsNotNone(view["zoom"])
        # A 1400x2100 page in a 1280x720 window: fitting the width is a
        # long way past fitting the window, so the zoom is positive.
        self.assertGreater(view["zoom"], 0.0)

    def test_fit_page_is_smaller_than_fit_width(self):
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        wide = browser.controller.picture_views[-1]["zoom"]
        page._set_mode("page")
        whole = browser.controller.picture_views[-1]["zoom"]
        self.assertLess(whole, wide)

    def test_zooming_walks_the_steps_and_stops(self):
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        seen = []
        for _i in range(12):
            page._step_zoom(1)
            seen.append(page.zoom())
        self.assertEqual(seen, sorted(seen), "the zoom did not keep rising")
        self.assertEqual(seen[-1], seen[-2], "the zoom did not stop")
        for _i in range(12):
            page._step_zoom(-1)
        self.assertEqual(page.zoom(), 0.5)

    def test_a_zoom_change_reaches_the_player(self):
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        before = browser.controller.picture_views[-1]["zoom"]
        page._step_zoom(1)
        self.assertGreater(browser.controller.picture_views[-1]["zoom"],
                           before)

    def test_resizing_the_window_replaces_the_page(self):
        """The window is what the zoom is measured against, so a resize
        moves it — and the page is only redrawn when something asks."""
        browser = self.open_comic()
        build_scene(browser, size=(1280, 720))
        before = browser.controller.picture_views[-1]["zoom"]
        build_scene(browser, size=(1920, 720))
        self.assertNotEqual(browser.controller.picture_views[-1]["zoom"],
                            before)


class TestGestures(ComicHarness):
    """The wheel and the drag live in the renderer; these are the two
    answers it cannot give itself."""

    class PanApp:
        """Records what gesture model the renderer was handed."""

        def __init__(self):
            self.models = []

        def set_picture_pan(self, config=None):
            self.models.append(config)

        def node_rect(self, _node_id):
            return None

        def scroll_offsets(self):
            return {}

        def invalidate(self):
            pass

        def claim_keys(self, keys=()):
            pass

    def open_comic(self, *a, **kw):
        browser = super().open_comic(*a, **kw)
        browser.app = self.PanApp()
        build_scene(browser)
        return browser

    def models(self, browser):
        return [m for m in browser.app.models if m]

    def test_the_renderer_is_given_a_clamp_and_a_unit(self):
        browser = self.open_comic()
        self.page(browser)._place()
        model = self.models(browser)[-1]
        for key in ("unitx", "unity", "minx", "maxx", "miny", "maxy",
                    "step"):
            self.assertIn(key, model)
        self.assertGreater(model["unity"], model["unitx"],
                           "a portrait page came out wider than it is tall")
        self.assertLessEqual(model["miny"], model["maxy"])

    def test_the_unit_is_the_displayed_size_not_the_window(self):
        """mpv's pan is measured in the scaled picture (measured — see
        tests/test_picture_view.py). Handing over the window's size instead
        makes every drag wrong by whatever the zoom is."""
        browser = self.open_comic()
        page = self.page(browser)
        page._place()
        model = self.models(browser)[-1]
        window = browser.route["_window"]
        self.assertAlmostEqual(model["unitx"], window[0], delta=2,
                               msg="fit-width should display at window "
                                   "width")
        self.assertGreater(model["unity"], window[1],
                           "a fit-width page is taller than the window")

    def test_zooming_re_sends_the_model(self):
        """The clamp is a function of the zoom, so a stale one lets the
        page be dragged off the screen."""
        browser = self.open_comic()
        page = self.page(browser)
        page._place()
        before = self.models(browser)[-1]
        page._step_zoom(1)
        after = self.models(browser)[-1]
        self.assertNotEqual(before["unity"], after["unity"])

    def test_a_wheel_notch_off_the_bottom_turns_the_page(self):
        browser = self.open_comic()
        page = self.page(browser)
        page.on_picture_gesture("vpan", {"edge": "bottom"})
        self.assertEqual(page.page_index(), 1)
        page.on_picture_gesture("vpan", {"edge": "top"})
        self.assertEqual(page.page_index(), 0)

    def test_ctrl_wheel_zooms(self):
        browser = self.open_comic()
        page = self.page(browser)
        before = page.zoom()
        page.on_picture_gesture("vzoom", {"dir": 1})
        self.assertGreater(page.zoom(), before)
        page.on_picture_gesture("vzoom", {"dir": -1})
        self.assertEqual(page.zoom(), before)

    def test_leaving_drops_the_model(self):
        """A gesture model that outlives its page pans a picture nobody
        can see."""
        browser = self.open_comic()
        browser.go_back()
        build_scene(browser)
        self.assertIsNone(browser.app.models[-1])


class TestYieldingTheWindow(ComicHarness):
    """What a page took hold of has to be given back when the browser hands
    the window to playback.

    ``build()`` returns before ``_retire_page`` once ``_browsing`` is
    False, so a page that yields is never retired — which is why none of
    this is covered by the leaving-the-route tests. Every one of these
    outlives the page in a way the user feels.
    """

    class GrabApp:
        def __init__(self):
            self.claims = []
            self.models = []

        def claim_keys(self, keys=()):
            self.claims.append(tuple(keys))

        def set_picture_pan(self, config=None):
            self.models.append(config)

        def node_rect(self, _node_id):
            return None

        def scroll_offsets(self):
            return {}

        def invalidate(self):
            pass

    def open_comic(self, *a, **kw):
        browser = super().open_comic(*a, **kw)
        browser.app = self.GrabApp()
        build_scene(browser)
        return browser

    def test_yielding_to_playback_resets_the_pictures_zoom(self):
        """video-zoom and video-pan are GLOBAL mpv options, so a film that
        started while a comic was open inherits the page's zoom — and so
        does every film after it, because clear_picture refuses once
        _video is set."""
        browser = self.open_comic()
        self.page(browser)._place()
        self.assertEqual(browser.controller.picture_views_reset, 0)
        browser._yield()
        self.assertGreaterEqual(browser.controller.picture_views_reset, 1)

    def test_yielding_to_playback_gives_the_keys_back(self):
        """SPACE stays force-bound to the reader otherwise: pause is dead
        and instead turns the page and reports the new position."""
        browser = self.open_comic()
        browser._yield()
        self.assertEqual(browser.app.claims[-1], ())

    def test_yielding_to_playback_drops_the_pan_model(self):
        """Or a wheel notch pans the playing video inside the comic's
        clamp."""
        browser = self.open_comic()
        self.page(browser)._place()
        browser._yield()
        self.assertIsNone(browser.app.models[-1])

    def test_minimizing_releases_the_same_three_things(self):
        browser = self.open_comic()
        self.page(browser)._place()
        browser.minimize()
        self.assertEqual(browser.app.claims[-1], ())
        self.assertIsNone(browser.app.models[-1])
        self.assertGreaterEqual(browser.controller.picture_views_reset, 1)


class TestAbandonedOpen(ComicHarness):
    """A book whose open is still in flight when the user navigates away.

    **On `_DeferredPool`, deliberately.** Every other suite here installs
    `_SyncPool`, which runs work at submit time — so the window this is
    about, a job in flight across a navigation, cannot exist in them, and
    the bug below shipped under a green suite for exactly that reason.
    """

    def open_deferred(self):
        source = FakeSource()
        source.libraries = [{"Id": "lib-books", "Name": "Books",
                             "Type": "CollectionFolder",
                             "CollectionType": "books"}]
        item = comic()
        source.items[item["Id"]] = item
        browser = MpvtkBrowser(app=None, source=source)
        pool = _DeferredPool()
        browser._pool = pool
        browser.controller = FakeController()
        browser.server = "srv1"
        browser.controller.book_downloads[item["Id"]] = ("complete", self.cbz)
        browser.navigate({"kind": "comic", "server": "srv1",
                          "item_id": item["Id"], "title": "A Comic"})
        return browser, pool

    def test_navigating_away_mid_open_does_not_strand_the_route(self):
        """AsyncRunner drops BOTH callbacks when the epoch has moved on, so
        a flag cleared only in done/failed is never cleared at all — and
        every re-entry is gated on it. That history entry then says
        "Getting the comic…" for the rest of the session.
        """
        browser, pool = self.open_deferred()
        route = browser.route
        pool.release(0)                    # the item load lands...
        self.assertTrue(route.get("_opening"),
                        "the archive open is not in flight")   # ...and submits
        browser.go_back()                  # bumps the epoch
        pool.release(0)                    # the open finishes, too late
        self.assertFalse(route.get("_opening"),
                         "the in-flight flag was never cleared, so this "
                         "route can never open the file again")

    def test_coming_back_to_an_abandoned_open_still_opens_the_comic(self):
        """The whole point of clearing the flag: the route is re-enterable.
        The route dict survives in the history with its data intact, so
        load() will not run again and only the render path can notice."""
        browser, pool = self.open_deferred()
        pool.release(0)
        browser.go_back()
        pool.release(0)                    # abandoned
        browser.go_forward()
        build_scene(browser)               # the render path re-opens
        pool.drain()
        build_scene(browser)
        pool.drain()
        self.assertIsNone(browser.route.get("_error"))
        self.assertIsNotNone(self.page(browser).archive,
                             "the comic never re-opened")


class TestResume(ComicHarness):
    def test_a_comic_opens_where_the_server_says_it_was_left(self):
        browser = self.open_comic(
            RunTimeTicks=4 * 10000,
            UserData={"PlaybackPositionTicks": 2 * 10000})
        self.assertEqual(self.page(browser).page_index(), 2)

    def test_turning_a_page_reports_it_back(self):
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        page._turn(1)
        self.assertTrue(browser.controller.positions_written,
                        "nothing was reported to the server")
        item_id, ticks = browser.controller.positions_written[-1]
        self.assertEqual(item_id, "cb1")
        self.assertEqual(ticks, 1 * 10000)

    def test_the_reported_page_tracks_the_reading(self):
        """Over the whole comic, because the failure this catches is an
        index that drifts by one somewhere in the middle rather than at
        either end."""
        browser = self.open_comic()
        build_scene(browser)
        page = self.page(browser)
        for expected in range(1, 4):
            page._turn(1)
            _id, ticks = browser.controller.positions_written[-1]
            self.assertEqual(ticks, expected * 10000)

    def test_a_stored_page_past_the_end_is_clamped(self):
        """The count the server probed is its own count of the entries,
        and a file added or removed since would resume past the end."""
        browser = self.open_comic(
            RunTimeTicks=99 * 10000,
            UserData={"PlaybackPositionTicks": 90 * 10000})
        self.assertEqual(self.page(browser).page_index(), 3)


class TestLeaving(ComicHarness):
    def test_leaving_takes_the_picture_down(self):
        """It is mpv's picture, so nothing else will: the comic would
        otherwise sit behind the library grid."""
        browser = self.open_comic()
        build_scene(browser)
        self.assertEqual(browser.controller.pictures_cleared, 0)
        browser.go_back()
        build_scene(browser)
        self.assertGreaterEqual(browser.controller.pictures_cleared, 1)

    def test_leaving_deletes_the_pages_it_extracted(self):
        browser = self.open_comic()
        build_scene(browser)
        path = browser.controller.pictures[-1]
        self.assertTrue(os.path.exists(path))
        browser.go_back()
        build_scene(browser)
        self.assertFalse(os.path.exists(path),
                         "the extracted page was left on disk")

    def test_going_back_in_reopens_the_comic(self):
        """close() has to leave the page usable, not spent: the route dict
        survives in the history with its data intact, so load() will not
        run again and only the render pass can notice the archive is
        gone."""
        browser = self.open_comic()
        build_scene(browser)
        browser.go_back()
        build_scene(browser)
        browser.go_forward()
        build_scene(browser)
        page = self.page(browser)
        self.assertIsNotNone(page.archive, "the comic did not re-open")
        self.assertTrue(os.path.exists(browser.controller.pictures[-1]))


class TestChrome(ComicHarness):
    def test_the_bar_offers_paging_zoom_and_a_mode(self):
        browser = self.open_comic()
        nodes, _handlers = build_scene(browser)
        found = ids(nodes)
        for node_id in ("cm-prev", "cm-next", "cm-page", "cm-zoom-in",
                        "cm-zoom-out", "cm-mode", "cm-back"):
            self.assertIn(node_id, found)

    def test_the_reader_draws_no_page_of_its_own(self):
        """The picture is mpv's. A bitmap here would be drawn *over* it —
        overlay images composite above everything the renderer draws — so
        one appearing means the page has quietly gone back to rasterizing
        comics in Python."""
        browser = self.open_comic()
        nodes, _handlers = build_scene(browser)
        self.assertFalse([n for n in nodes if n["t"] in ("img", "imgmap")],
                         "the comic page drew a bitmap")


if __name__ == "__main__":
    unittest.main()
