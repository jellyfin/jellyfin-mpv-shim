"""The reader as a screen: keys, page turns, and the position it writes.

Against a **real epub file** and a real `EpubDocument`, because everything
this page does that could be wrong is a question about the book — where the
page boundaries are, what the offset is, what fraction that offset is. A
stand-in document would answer all of them by agreeing with the page.

What is faked is the shell around it: the source, the download catalog and
the controller, all from ``_shell_harness``. The one addition is a fake
`app` that records key claims, because "the page claims LEFT/RIGHT while it
is up and gives them back when it is not" is a property with no other
observable.
"""

import os
import tempfile
import unittest

from jellyfin_mpv_shim.books import EPUB_FULL_TICKS
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._epub_fixtures import build_epub, paragraphs, png_bytes
from tests._shell_harness import (FakeController, FakeSource, _SyncPool,
                             build_scene, ids)


class FakeApp:
    """Records what the shell claims. Everything else is what the browser
    tolerates from ``app=None`` today."""

    def __init__(self):
        self.claims = []

    def claim_keys(self, keys=()):
        self.claims.append(tuple(keys))

    def invalidate(self):
        pass

    def node_rect(self, _node_id):
        return None

    def scroll_offsets(self):
        return {}


def book(item_id="bk1", path="/library/A Novel.epub", ticks=0, **extra):
    return {"Id": item_id, "Name": "A Novel", "Type": "Book", "Path": path,
            "RunTimeTicks": EPUB_FULL_TICKS,
            "UserData": {"PlaybackPositionTicks": ticks}, **extra}


class ReaderHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.epub = build_epub(
            os.path.join(self._tmp.name, "novel.epub"),
            [paragraphs(20, words=45) for _i in range(4)],
            title="A Novel", author="An Author",
            toc=[("One", "ch1.xhtml"), ("Three", "ch3.xhtml")],
            extra={"p.png": png_bytes()})

    def open_reader(self, ticks=0, state=None):
        src = FakeSource()
        src.libraries = [{"Id": "lib-books", "Name": "Books",
                          "Type": "CollectionFolder",
                          "CollectionType": "books"}]
        item = book(ticks=ticks)
        src.items["bk1"] = item
        browser = MpvtkBrowser(app=FakeApp(), source=src)
        browser._pool = _SyncPool()
        browser.controller = FakeController()
        browser.server = "srv1"
        browser.controller.book_downloads["bk1"] = (
            state if state is not None else ("complete", self.epub))
        browser.navigate({"kind": "reader", "server": "srv1",
                          "item_id": "bk1", "title": "A Novel"})
        self.src = src
        self.item = item
        return browser

    @staticmethod
    def doc(browser):
        return browser.route.get("_doc")


class TestOpening(ReaderHarness):
    def test_a_downloaded_book_opens_and_draws_a_page(self):
        browser = self.open_reader()
        self.assertIsNotNone(self.doc(browser), "the book did not open")
        nodes, _handlers = build_scene(browser)
        self.assertTrue(any(node["t"] in ("img", "imgmap") for node in nodes),
                        "no page bitmap was drawn")

    def test_a_book_that_is_not_downloaded_yet_is_fetched(self):
        """Read cannot mean "stream it": there is no endpoint that serves
        part of a book. So the reader route is a download the user is
        waiting inside of, and it says so rather than showing an error."""
        browser = self.open_reader(state=(None, None))
        self.assertIsNone(self.doc(browser))
        self.assertTrue(browser.controller.enqueued,
                        "the reader did not fetch the book")
        build_scene(browser)

    def test_the_book_opens_when_the_download_lands(self):
        """Driven by the catalog's change hook, not by a poller: the same
        wait the Read button has always had, ending in the reader."""
        browser = self.open_reader(state=(None, None))
        browser.controller.book_downloads["bk1"] = ("complete", self.epub)
        page = browser._page_for(browser.route)
        page.refresh_download_state()
        self.assertIsNotNone(self.doc(browser))

    def test_a_file_that_is_not_an_epub_reports_instead_of_raising(self):
        broken = os.path.join(self._tmp.name, "broken.epub")
        with open(broken, "wb") as handle:
            handle.write(b"this is not a zip")
        browser = self.open_reader(state=("complete", broken))
        self.assertIsNone(self.doc(browser))
        self.assertTrue(browser.route.get("_error"))
        build_scene(browser)


class TestReading(ReaderHarness):
    def test_the_page_claims_its_turn_keys_while_it_is_up(self):
        browser = self.open_reader()
        build_scene(browser)
        self.assertIn("LEFT", browser.app.claims[-1])
        self.assertIn("RIGHT", browser.app.claims[-1])

    def test_leaving_the_reader_gives_the_keys_back(self):
        """A claim that outlives its page takes LEFT and RIGHT away from
        every screen after it — and, if it survived to playback, from the
        player's own seek keys."""
        browser = self.open_reader()
        build_scene(browser)
        browser.go_back()
        build_scene(browser)
        self.assertEqual(browser.app.claims[-1], ())

    def test_a_turn_key_turns_the_page(self):
        browser = self.open_reader()
        build_scene(browser)
        before = self.doc(browser).char_offset
        browser._on_claimed_key("RIGHT")
        self.assertGreater(self.doc(browser).char_offset, before)
        browser._on_claimed_key("LEFT")
        self.assertEqual(self.doc(browser).char_offset, before)

    def test_clicking_the_right_of_the_page_turns_it_forward(self):
        browser = self.open_reader()
        _nodes, handlers = build_scene(browser)
        before = self.doc(browser).char_offset
        handlers["rd-fwd-half"]["click"]()
        self.assertGreater(self.doc(browser).char_offset, before)
        handlers["rd-back-half"]["click"]()
        self.assertEqual(self.doc(browser).char_offset, before)

    def test_paging_past_the_end_of_a_chapter_enters_the_next(self):
        browser = self.open_reader()
        build_scene(browser)
        doc = self.doc(browser)
        for _i in range(400):
            if doc.spine_index > 0:
                break
            browser._on_claimed_key("RIGHT")
        self.assertGreater(doc.spine_index, 0,
                           "the reader never left the first chapter")

    def test_the_chapter_picker_jumps_to_its_document(self):
        browser = self.open_reader()
        _nodes, handlers = build_scene(browser)
        handlers["rd-toc"]["select"](1, None)
        self.assertEqual(self.doc(browser).spine_index, 2)

    def test_the_type_size_buttons_keep_the_reader_in_place(self):
        browser = self.open_reader()
        _nodes, handlers = build_scene(browser)
        for _i in range(5):
            browser._on_claimed_key("RIGHT")
        offset = self.doc(browser).char_offset
        handlers["rd-bigger"]["click"]()
        self.assertEqual(self.doc(browser).char_offset, offset)
        handlers["rd-smaller"]["click"]()
        self.assertEqual(self.doc(browser).char_offset, offset)

    def test_the_palette_button_cycles_and_redraws(self):
        browser = self.open_reader()
        _nodes, handlers = build_scene(browser)
        first = browser.route.get("_palette")
        handlers["rd-theme"]["click"]()
        self.assertNotEqual(browser.route.get("_palette"), first)
        build_scene(browser)


class TestPosition(ReaderHarness):
    def test_a_page_turn_writes_the_position_back(self):
        browser = self.open_reader()
        build_scene(browser)
        browser._on_claimed_key("RIGHT")
        self.assertTrue(browser.controller.positions_written,
                        "nothing was reported to the server")
        item_id, ticks = browser.controller.positions_written[-1]
        self.assertEqual(item_id, "bk1")
        self.assertGreater(ticks, 0)
        self.assertLessEqual(ticks, EPUB_FULL_TICKS)

    def test_the_reported_position_only_ever_moves_forward_while_reading(
            self):
        """The multi-step property. A per-turn test passes even when the
        fraction is computed against the wrong section base, because any
        single number looks plausible."""
        browser = self.open_reader()
        build_scene(browser)
        previous = -1
        for _i in range(60):
            browser._on_claimed_key("RIGHT")
        for _item, ticks in browser.controller.positions_written:
            self.assertGreaterEqual(ticks, previous)
            previous = ticks

    def test_the_same_position_is_not_written_twice(self):
        """Paging into a chapter and back out lands on the same offset, and
        a write per frame would be a request per frame."""
        browser = self.open_reader()
        build_scene(browser)
        browser._on_claimed_key("RIGHT")
        count = len(browser.controller.positions_written)
        browser._on_claimed_key("LEFT")
        browser._on_claimed_key("RIGHT")
        self.assertLessEqual(len(browser.controller.positions_written),
                             count + 2)

    def test_a_stored_position_is_resumed_before_the_first_paint(self):
        """Otherwise the book shows page one of chapter one and then jumps,
        which reads as having lost the place and found it again."""
        opened = self.open_reader()
        build_scene(opened)
        for _i in range(30):
            opened._on_claimed_key("RIGHT")
        ticks = opened.controller.positions_written[-1][1]

        resumed = self.open_reader(ticks=ticks)
        doc = self.doc(resumed)
        self.assertGreater(doc.spine_index + doc.page_number, 0,
                           "the book opened at the beginning")
        self.assertAlmostEqual(doc.fraction(), ticks / float(EPUB_FULL_TICKS),
                               delta=0.03)

    def test_the_books_page_shows_the_position_the_reader_wrote(self):
        """The DTO in hand is updated too — going back to the book's own
        page otherwise shows the figure it was loaded with."""
        browser = self.open_reader()
        build_scene(browser)
        browser._on_claimed_key("RIGHT")
        item = browser.route["_data"]["item"]
        self.assertGreater(item["UserData"]["PlaybackPositionTicks"], 0)


class TestHeadless(ReaderHarness):
    def test_a_cast_target_cannot_reach_the_reader(self):
        """``headless`` means the library is unreachable from this machine,
        and a book is library content like any other. The navigator refuses
        by default — this pins that the new route did not have to be
        remembered anywhere to be covered."""
        browser = self.open_reader()
        browser.headless = True
        allowed = browser._nav.allows({"kind": "reader"})
        self.assertFalse(allowed)


if __name__ == "__main__":
    unittest.main()
