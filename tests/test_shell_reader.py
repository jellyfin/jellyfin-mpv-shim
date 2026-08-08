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
from jellyfin_mpv_shim.conf import settings
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from jellyfin_mpv_shim.mpvtk_browser.pages.reader import PALETTE_ORDER

from tests._epub_fixtures import build_epub, paragraphs, png_bytes
from tests._shell_harness import (FakeController, FakeSource, _SyncPool,
                             build_scene, ids)


class FakeApp:
    """Records what the shell claims. Everything else is what the browser
    tolerates from ``app=None`` today."""

    def __init__(self):
        self.claims = []
        #: node id -> laid-out rect, as the real app answers from the last
        #: pushed scene. Empty by default; a test that needs the reader to
        #: turn a pointer position into a place in the book puts the page
        #: bitmap's rect here, because that conversion is the thing under
        #: test and a fake that answered None would skip it entirely.
        self.rects = {}

    def claim_keys(self, keys=()):
        self.claims.append(tuple(keys))

    def invalidate(self):
        pass

    def node_rect(self, node_id):
        return self.rects.get(node_id)

    def scroll_offsets(self):
        return {}


def book(item_id="bk1", path="/library/A Novel.epub", ticks=0, **extra):
    return {"Id": item_id, "Name": "A Novel", "Type": "Book", "Path": path,
            "RunTimeTicks": EPUB_FULL_TICKS,
            "UserData": {"PlaybackPositionTicks": ticks}, **extra}


class ReaderHarness(unittest.TestCase):
    #: Config keys the reader owns. Saved and restored around every test,
    #: with ``save`` stubbed: these are real settings now, and a test that
    #: wrote one would rewrite the developer's own conf.json — and then
    #: leave the next test reading whatever the last one chose.
    READER_KEYS = ("reader_font_size", "reader_theme", "reader_justify")

    def setUp(self):
        self.settings = settings
        self._saved = {k: getattr(settings, k) for k in self.READER_KEYS}
        self._saved_save = settings.save
        settings.save = lambda *a, **k: None
        self.addCleanup(self._restore_settings)
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

    def _restore_settings(self):
        settings.save = self._saved_save
        for key, value in self._saved.items():
            setattr(settings, key, value)

    @staticmethod
    def doc(browser):
        return browser.route.get("_doc")

    @staticmethod
    def page(browser):
        return browser._page_for(browser.route)


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

    def test_the_page_turn_halves_are_not_decorated(self):
        """The turn zones are hit-rects over a page of prose, so neither
        ring may draw on them.

        Both are asserted because they are two different mechanisms with the
        same symptom: ``hover`` is the accent box the pointer brings, and
        being in the focus order is the accent box the arrow keys bring. A
        region that dropped only the first still boxes the paragraph the
        moment somebody presses UP from the bottom bar.
        """
        browser = self.open_reader()
        nodes, _handlers = build_scene(browser)
        halves = [n for n in nodes
                  if n.get("id") in ("rd-fwd-half", "rd-back-half")]
        self.assertEqual(len(halves), 2, "the turn zones are not on screen")
        for node in halves:
            self.assertIsNone(node.get("hover"), node["id"])
            self.assertTrue(node.get("nnav"), node["id"])
            self.assertTrue(node.get("click"), node["id"])

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
        first = settings.reader_theme
        handlers["rd-theme"]["click"]()
        self.assertNotEqual(settings.reader_theme, first)
        build_scene(browser)

    def test_the_reader_controls_write_the_settings_they_read(self):
        """Both directions, over several presses.

        The reader's buttons and the Settings form are two views of one
        value, and the failure that matters is *drift*: a control that
        keeps its own copy looks right while it is on screen and reverts
        the moment the page is rebuilt. So the assertion is that the
        setting moved, and that the reader now reports the setting — after
        each of several presses, because one press agrees with almost any
        implementation.
        """
        browser = self.open_reader()
        _nodes, handlers = build_scene(browser)
        page = self.page(browser)
        seen = []
        for _i in range(4):
            handlers["rd-bigger"]["click"]()
            self.assertEqual(page.font_size(), settings.reader_font_size)
            seen.append(settings.reader_font_size)
        self.assertEqual(seen, sorted(seen), "the size did not keep rising")
        self.assertGreater(seen[-1], seen[0])
        for _i in range(len(PALETTE_ORDER)):
            handlers["rd-theme"]["click"]()
            self.assertEqual(page.palette_name(), settings.reader_theme)
        self.assertEqual(settings.reader_theme, PALETTE_ORDER[0],
                         "cycling all the way round did not come home")

    def test_a_typed_size_is_kept_and_stepped_from(self):
        """A number nobody stepped to is still a number the user chose.

        An index into FONT_STEPS would silently round 22 to the nearest
        entry the first time either button was pressed.
        """
        settings.reader_font_size = 22
        browser = self.open_reader()
        build_scene(browser)
        page = self.page(browser)
        self.assertEqual(page.font_size(), 22)
        page._step_font(1)
        self.assertEqual(settings.reader_font_size, 24)
        page._step_font(-1)
        self.assertEqual(settings.reader_font_size, 21)

    def test_a_nonsense_setting_still_opens_the_book(self):
        """Both keys are free text in a JSON file. A hand-edited page
        colour must not reach the palette table, and a hand-edited size of
        0 must not reach the layout engine."""
        settings.reader_theme = "puce"
        settings.reader_font_size = 0
        browser = self.open_reader()
        page = self.page(browser)
        self.assertEqual(page.palette_name(), PALETTE_ORDER[0])
        self.assertGreaterEqual(page.font_size(), 10)
        nodes, _handlers = build_scene(browser)
        self.assertTrue(any(n["t"] in ("img", "imgmap") for n in nodes),
                        "the page was not drawn")

    def test_changing_the_size_in_settings_reaches_an_open_book(self):
        """Settings is reachable from the tray while a book is open, and
        the reader can also be sitting in the history with a document
        already laid out. Asserted over three changes, because a page that
        adopts the setting once — at open — passes the one-step version.
        """
        browser = self.open_reader()
        build_scene(browser)
        doc = self.doc(browser)
        widths = []
        for size in (15, 27, 36):
            settings.reader_font_size = size
            build_scene(browser)
            widths.append(doc.style.font_px)
        self.assertEqual(widths, sorted(widths))
        self.assertEqual(len(set(widths)), 3,
                         "the open book kept its original type size")


class TestCopying(ReaderHarness):
    """The context menu, which is what stands in for selecting text."""

    #: Where the test says the page bitmap was drawn. The origin is
    #: deliberately not (0, 0): a conversion that forgets to subtract it
    #: agrees with the truth at the origin and nowhere else.
    PAGE_RECT = {"id": "rd-page", "x": 40, "y": 60, "w": 700, "h": 520}

    def setUp(self):
        super().setUp()
        # Every paragraph says something different. The shared fixture is
        # twenty paragraphs of the word "word", which is fine for counting
        # lines and useless here: two neighbouring paragraphs compare equal,
        # so a hit test that picked the wrong one would agree with the right
        # answer character for character.
        self.epub = build_epub(
            os.path.join(self._tmp.name, "distinct.epub"),
            ["".join("<p>Paragraph %d. %s</p>"
                     % (i, " ".join(["sentence %d" % i] * 20))
                     for i in range(30))],
            title="A Novel", author="An Author")

    def open_reader(self, *a, **kw):
        browser = super().open_reader(*a, **kw)
        browser.app.rects["rd-page"] = dict(self.PAGE_RECT)
        return browser

    def click(self, browser, dx, dy, button="rd-fwd-half"):
        _nodes, handlers = build_scene(browser)
        handlers[button]["context"](self.PAGE_RECT["x"] + dx,
                                    self.PAGE_RECT["y"] + dy)
        return build_scene(browser)

    def menu_items(self, nodes):
        for node in nodes:
            if node.get("id") == "rd-menu":
                return node["items"]
        return None

    def test_right_clicking_offers_the_paragraph_and_the_page(self):
        browser = self.open_reader()
        nodes, _handlers = self.click(browser, 200, 120)
        items = self.menu_items(nodes)
        self.assertIsNotNone(items, "no menu opened")
        self.assertEqual(len(items), 2)

    def test_copying_a_paragraph_copies_the_one_under_the_pointer(self):
        """The paragraph, and the *right* paragraph.

        Both halves matter and only the second can fail quietly: a
        conversion that lands a few lines out still copies a paragraph, and
        every assertion about "some text was copied" passes while the user
        gets the wrong one. So this reads what is on screen at that height
        and demands the same paragraph back.
        """
        browser = self.open_reader()
        doc = self.doc(browser)
        want = doc.paragraph_at(300)
        self.assertTrue(want, "the fixture has no paragraph to copy")
        _nodes, handlers = self.click(browser, 120,
                                      300 + doc.style.margin_y)
        handlers["rd-menu"]["select"](0, None)
        self.assertEqual(browser.controller.copied, [want])

    def test_the_conversion_holds_on_a_hidpi_display(self):
        """`_column_y` is where three coordinate spaces meet, and at scale
        1.0 two of them are the same number — so every test of it was
        checking a conversion with nothing to convert.

        Asserted on the conversion itself rather than through which
        paragraph comes back: a paragraph is a hundred pixels tall, so it
        absorbs an error that a HiDPI user would feel as the wrong half of
        the page. (Checked: dropping the `px()` here does not change which
        paragraph this fixture returns.)
        """
        from jellyfin_mpv_shim.mpvtk import scaling

        original = scaling.scale()
        self.addCleanup(scaling.set_scale, original)
        for scale in (1.0, 2.0):
            with self.subTest(scale=scale):
                scaling.set_scale(scale)
                browser = self.open_reader()
                browser.app.rects["rd-page"] = dict(self.PAGE_RECT)
                build_scene(browser)
                page = self.page(browser)
                style = self.doc(browser).style
                for physical in (0, 120, 640):
                    # The event arrives in LOGICAL window pixels; the answer
                    # is in physical pixels from the top of the column.
                    event_y = (self.PAGE_RECT["y"]
                               + scaling.dip(physical + style.margin_y))
                    self.assertAlmostEqual(
                        page._column_y(event_y), physical, delta=1,
                        msg="scale %s, %d px down the column"
                            % (scale, physical))

    def test_the_paragraph_is_the_one_at_that_height_all_down_the_page(self):
        """Swept, because a conversion that is off by a constant — a
        forgotten margin, an unscaled origin — agrees with the truth over
        most of a page and disagrees only where two paragraphs meet. One
        point picked in the middle of a long one will never see it.
        """
        browser = self.open_reader()
        doc = self.doc(browser)
        page = self.page(browser)
        build_scene(browser)
        checked = set()
        for y in range(0, 460, 9):
            want = doc.paragraph_at(y)
            page._open_menu(self.PAGE_RECT["x"] + 100,
                            self.PAGE_RECT["y"] + doc.style.margin_y + y)
            got = (browser.route.get("_menu") or {}).get("para")
            self.assertEqual(got, want, "at %d px down the column" % y)
            checked.add(want)
        self.assertGreater(len(checked), 2,
                           "the sweep never crossed a paragraph")

    def test_copying_the_page_copies_every_paragraph_on_it(self):
        browser = self.open_reader()
        doc = self.doc(browser)
        _nodes, handlers = self.click(browser, 200, 120)
        items = self.menu_items(_nodes)
        handlers["rd-menu"]["select"](items.index("Copy Page"), None)
        self.assertEqual(len(browser.controller.copied), 1)
        text = browser.controller.copied[0]
        self.assertEqual(text, doc.page_text())
        # Not one line, and not the whole chapter: what is on screen.
        # Several paragraphs, not one line: a copier that took the clicked
        # line would come back with a fragment.
        self.assertGreater(len(text), 200)
        self.assertIn("\n\n", text, "the paragraphs were run together")
        # And it is *readable text*. The obvious way to build this — join
        # the laid-out lines — silently drops every space, because a space
        # is not a run: the breaker discards space tokens and
        # justification turns them into a gap between two pieces' x. The
        # result still looks like a page of text in a repr.
        self.assertIn("Paragraph 0. sentence 0 sentence 0", text)
        self.assertNotIn("sentence0sentence", text.replace(" ", "x"))
        for word in text.split("\n\n")[0].split():
            self.assertLess(len(word), 30, "the words ran together: %r"
                            % text[:80])

    def test_a_paragraph_the_page_ends_inside_is_copied_whole(self):
        """Half a paragraph is a fragment starting mid-sentence. The page
        copy is the paragraphs it draws, each entire — the same answer the
        paragraph copy gives, and the only one that can put the spaces
        back, since the laid-out lines no longer hold them."""
        browser = self.open_reader()
        doc = self.doc(browser)
        text = doc.page_text()
        last = doc._blocks_on_page()[-1]
        self.assertIn(" ".join(last.text().split()), text)

    def test_the_menu_closes_and_does_not_turn_the_page(self):
        """A right-click lands on the same region a left-click turns with,
        so the two must not share an outcome."""
        browser = self.open_reader()
        before = self.doc(browser).char_offset
        nodes, handlers = self.click(browser, 200, 120)
        self.assertEqual(self.doc(browser).char_offset, before)
        handlers["rd-menu"]["dismiss"]()
        nodes, _handlers = build_scene(browser)
        self.assertIsNone(self.menu_items(nodes), "the menu stayed open")
        self.assertEqual(self.doc(browser).char_offset, before)

    def test_a_box_with_no_clipboard_says_where_the_text_went(self):
        """A button that silently does nothing is worse than one that tells
        you it wrote a file instead."""
        browser = self.open_reader()
        browser.controller.copy_result = (True, "file", "/tmp/copied.txt")
        nodes, handlers = self.click(browser, 200, 120)
        items = self.menu_items(nodes)
        handlers["rd-menu"]["select"](items.index("Copy Page"), None)
        self.assertIn("/tmp/copied.txt", browser.status or "")

    def test_without_a_measured_page_only_the_page_can_be_copied(self):
        """The first frame has no rect to convert against. Offering "Copy
        Paragraph" then would copy whatever paragraph the fallback picked,
        which is a wrong answer dressed as a right one."""
        browser = self.open_reader()
        browser.app.rects.clear()
        nodes, _handlers = self.click(browser, 200, 120)
        self.assertEqual(self.menu_items(nodes), ["Copy Page"])


class TestPosition(ReaderHarness):
    def turn_until_reported(self, browser, limit=40):
        """Turn pages until a non-zero position is reported.

        Not one turn: the unit the server stores is a *location*, about a
        thousand characters, and a page holds fewer than that at a
        comfortable measure — so the first turn or two genuinely leave the
        fraction at zero, and a test that demanded a number after exactly
        one turn was reading the fixture's page size rather than the
        reader's behaviour.
        """
        for _i in range(limit):
            browser._on_claimed_key("RIGHT")
            written = browser.controller.positions_written
            if written and written[-1][1] > 0:
                return written[-1]
        raise AssertionError("no position was ever reported")

    def test_a_page_turn_writes_the_position_back(self):
        browser = self.open_reader()
        build_scene(browser)
        item_id, ticks = self.turn_until_reported(browser)
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
        """Paging forward and back lands on the offset already reported, so
        the second arrival must write nothing.

        ``<= count + 2`` was what a *completely undeduplicated*
        implementation produces — three presses, three writes — so the
        assertion passed with the guard removed. Measured under this
        harness: guard on, one write; guard off, three.
        """
        browser = self.open_reader()
        build_scene(browser)
        browser._on_claimed_key("RIGHT")
        after_first = list(browser.controller.positions_written)
        browser._on_claimed_key("LEFT")
        browser._on_claimed_key("RIGHT")
        self.assertEqual(browser.controller.positions_written, after_first,
                         "returning to a reported position reported it "
                         "again")

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
        _id, ticks = self.turn_until_reported(browser)
        item = browser.route["_data"]["item"]
        self.assertEqual(item["UserData"]["PlaybackPositionTicks"], ticks)


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
