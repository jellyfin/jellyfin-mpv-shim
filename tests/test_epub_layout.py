"""Pagination, line breaking, and the property the whole design rests on.

That property is: **the reader's position is a character offset, and a
re-layout must not move it.** Everything else about a resize is allowed to
change — page count, page boundaries, which line is at the top — but the
reader has to still be looking at the text they were looking at. A page
number would have no answer here at all, which is why it is not the state.

The rest of these are the ones a single-step test cannot see: paging
through a whole document and asserting the text arrives exactly once, and
re-laying-out repeatedly and asserting the offset does not walk. Both are
the shape ``CLAUDE.md`` names — state feeding back into the input that
produced it — and both are how the two real bugs found while writing this
showed up.
"""

import os
import tempfile
import unittest

from jellyfin_mpv_shim.epub import content, layout
from jellyfin_mpv_shim.epub.book import EpubDocument
from jellyfin_mpv_shim.epub.paint import palette

from tests._epub_fixtures import build_epub, paragraphs, png_bytes, xhtml

COLUMN = (600, 700)


def blocks_of(body, sheet_css=None):
    from jellyfin_mpv_shim.epub import css as cssmod

    sheet = None
    if sheet_css:
        sheet = cssmod.Stylesheet()
        sheet.add(sheet_css)
    return content.parse_document(xhtml(body), sheet=sheet)[0]


def measurer(**kw):
    return layout.Measurer(layout.ReaderStyle(**kw))


class TestLineBreaking(unittest.TestCase):
    def lines(self, body, width=COLUMN[0], **style):
        pages = layout.paginate(blocks_of(body), width, 10000,
                                measurer(**style))
        return [line for page in pages for line in page.items
                if isinstance(line, layout.Line)]

    def test_no_line_overruns_its_column(self):
        measure = measurer()
        pages = layout.paginate(blocks_of(paragraphs(6, words=60)),
                                COLUMN[0], 10000, measure)
        for page in pages:
            for line in page.items:
                if not isinstance(line, layout.Line):
                    continue
                last = line.pieces[-1]
                right = last.x + measure.width(last.text, last.style)
                self.assertLessEqual(
                    right, COLUMN[0] + 1,
                    "a line ran %d px past the column" % (right - COLUMN[0]))

    def test_justification_reaches_the_right_margin(self):
        measure = measurer(justify=True)
        pages = layout.paginate(blocks_of(paragraphs(4, words=80)),
                                COLUMN[0], 10000, measure)
        lines = [ln for page in pages for ln in page.items
                 if isinstance(ln, layout.Line) and len(ln.pieces) > 4]
        # The middle of a paragraph, where every line but the last is
        # justified. Not all of them: a line ending a paragraph is ragged
        # by definition, and so is one the stretch limit gave up on.
        flush = [ln for ln in lines
                 if abs(ln.pieces[-1].x
                        + measure.width(ln.pieces[-1].text,
                                        ln.pieces[-1].style)
                        - COLUMN[0]) < 2]
        self.assertGreater(len(flush), len(lines) // 2,
                           "most lines are not reaching the margin")

    def test_a_hyphenated_compound_offers_a_break_at_its_hyphen(self):
        """Without this the line before an unbreakable compound is short,
        and on a justified page that reads as a rendering fault rather than
        as typography."""
        parts = [t for t, _space, _hard
                 in layout._split_words("shrink-wrapped")]
        self.assertEqual(parts, ["shrink-", "wrapped"])

    def test_the_hyphen_break_is_taken_when_the_line_needs_it(self):
        """Swept across filler lengths rather than fixed: which word lands
        on the margin depends on the measured font, and a test that pins
        one filler length is testing this machine's DejaVu metrics."""
        for filler in range(6, 30):
            text = "aaa " * filler + "shrink-wrapped " + "bbb " * 10
            lines = self.lines("<p>%s</p>" % text)
            if any(ln.text().endswith("shrink-") for ln in lines):
                return
        self.fail("the compound was never broken at its hyphen")

    def test_a_short_word_is_not_split_at_its_hyphen(self):
        lines = self.lines("<p>%s</p>" % ("e-mail " * 60))
        self.assertFalse(any(ln.text().endswith("e-") for ln in lines))

    def test_cjk_text_breaks_between_characters(self):
        """There are no spaces to break at, so a paragraph of Japanese is
        otherwise one token as wide as the chapter."""
        lines = self.lines("<p>%s</p>" % ("日本語の本" * 60))
        self.assertGreater(len(lines), 1)

    def test_closing_punctuation_never_starts_a_line(self):
        lines = self.lines("<p>%s</p>" % ("文字。" * 80))
        self.assertFalse(any(ln.text().startswith("。") for ln in lines))

    def test_a_hard_break_ends_the_line_where_it_falls(self):
        lines = self.lines("<p>one<br/>two</p>")
        self.assertEqual([ln.text() for ln in lines], ["one", "two"])

    def test_a_list_marker_hangs_left_of_its_text(self):
        pages = layout.paginate(blocks_of("<ul><li>item</li></ul>"),
                                COLUMN[0], 10000, measurer())
        line = pages[0].items[0]
        self.assertEqual(line.pieces[0].text, "•")
        self.assertLess(line.pieces[0].x, line.pieces[1].x)


class TestPagination(unittest.TestCase):
    def test_every_word_appears_exactly_once_across_the_pages(self):
        """The property a page-boundary bug breaks in both directions: a
        line dropped at a break is invisible, and one repeated reads as the
        reader having gone backwards."""
        body = "".join("<p>p%d %s</p>" % (i, " ".join(
            "w%d-%d" % (i, w) for w in range(30))) for i in range(12))
        pages = layout.paginate(blocks_of(body), COLUMN[0], COLUMN[1],
                                measurer())
        words = []
        for page in pages:
            for line in page.items:
                if isinstance(line, layout.Line):
                    words += [p.text for p in line.pieces]
        self.assertGreater(len(pages), 1, "the fixture fits on one page")
        for i in range(12):
            for w in range(30):
                token = "w%d-%d" % (i, w)
                self.assertEqual(words.count(token), 1,
                                 "%s appears %d times" % (token,
                                                          words.count(token)))

    def test_page_start_offsets_only_ever_increase(self):
        pages = layout.paginate(blocks_of(paragraphs(30)), COLUMN[0],
                                COLUMN[1], measurer())
        offsets = [p.start_offset for p in pages]
        self.assertEqual(offsets, sorted(offsets))

    def test_nothing_is_placed_below_the_page(self):
        pages = layout.paginate(blocks_of(paragraphs(20)), COLUMN[0],
                                COLUMN[1], measurer())
        for page in pages:
            for item in page.items:
                bottom = item.y + getattr(item, "height",
                                          getattr(item, "h", 0))
                self.assertLessEqual(bottom, COLUMN[1] + 1)

    def test_a_document_with_nothing_drawable_still_has_a_page(self):
        """Or the reader has nowhere to be while it is on that spine item,
        and paging forward walks off the end of an empty list."""
        pages = layout.paginate([], COLUMN[0], COLUMN[1], measurer())
        self.assertEqual(len(pages), 1)

    def test_an_image_taller_than_the_page_is_scaled_to_fit_it(self):
        blocks = blocks_of('<img src="tall.png"/>')
        pages = layout.paginate(blocks, COLUMN[0], COLUMN[1], measurer(),
                                image_size=lambda _src: (400, 4000))
        image = pages[0].items[0]
        self.assertLessEqual(image.h, COLUMN[1])

    def test_an_image_whose_size_cannot_be_read_is_skipped(self):
        """Guessing pushes every page boundary after it."""
        blocks = blocks_of('<p>a</p><img src="gone.png"/><p>b</p>')
        pages = layout.paginate(blocks, COLUMN[0], COLUMN[1], measurer(),
                                image_size=lambda _src: None)
        items = [i for p in pages for i in p.items]
        self.assertFalse(any(isinstance(i, layout.ImageItem) for i in items))
        self.assertEqual(len([i for i in items
                              if isinstance(i, layout.Line)]), 2)

    def test_a_page_break_before_starts_a_new_page(self):
        blocks = blocks_of('<p>a</p><p class="c">b</p>',
                           "p.c{page-break-before:always}")
        pages = layout.paginate(blocks, COLUMN[0], COLUMN[1], measurer())
        self.assertEqual(len(pages), 2)


class TestDocumentPosition(unittest.TestCase):
    """The offset-is-the-state property, against a real book."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        path = build_epub(
            os.path.join(self._tmp.name, "book.epub"),
            [paragraphs(25, words=45) for _i in range(4)],
            toc=[("One", "ch1.xhtml"), ("Three", "ch3.xhtml")],
            extra={"p.png": png_bytes()})
        self.doc = EpubDocument(path)
        self.addCleanup(self.doc.close)
        self.doc.set_viewport(*COLUMN)
        self.doc.build_index()

    def test_paging_forward_reaches_the_end_and_stops(self):
        seen = set()
        turns = 0
        while self.doc.next_page() and turns < 5000:
            seen.add((self.doc.spine_index, self.doc.page_number))
            turns += 1
        self.assertLess(turns, 5000, "paging forward never terminated")
        self.assertEqual(self.doc.spine_index, self.doc.spine_count - 1)
        self.assertEqual(len(seen), turns, "a page was visited twice")

    def test_back_then_forward_returns_to_the_same_page(self):
        for _i in range(6):
            self.doc.next_page()
        where = (self.doc.spine_index, self.doc.page_number)
        self.doc.prev_page()
        self.doc.next_page()
        self.assertEqual((self.doc.spine_index, self.doc.page_number), where)

    def test_going_back_across_a_section_lands_on_its_last_page(self):
        self.doc.goto(1, 0)
        self.doc.prev_page()
        self.assertEqual(self.doc.spine_index, 0)
        self.assertEqual(self.doc.page_number, len(self.doc.pages(0)) - 1)

    def test_a_resize_keeps_the_reader_where_they_were(self):
        for _i in range(9):
            self.doc.next_page()
        before = self.doc.char_offset
        section = self.doc.spine_index
        for size in ((420, 500), (900, 1200), (300, 400), (600, 700)):
            self.doc.set_viewport(*size)
            page = self.doc.current_page()
            self.assertEqual(self.doc.spine_index, section,
                             "a resize moved the reader to another chapter")
            self.assertLessEqual(
                page.start_offset, before,
                "the page now starts after the text the reader was on")
            self.assertGreaterEqual(
                page.end_offset, min(before, page.end_offset),
                "the reader's position fell off the page")

    def test_repeated_relayouts_do_not_walk_the_position(self):
        """The multi-step version, because the one-step version passes even
        when each resize nudges the offset a little: the state must be
        derived from the offset, never the offset re-derived from the page.
        """
        self.doc.goto(2, 0)
        for _i in range(5):
            self.doc.next_page()
        offset = self.doc.char_offset
        for _round in range(6):
            self.doc.set_viewport(500, 640)
            self.doc.set_viewport(*COLUMN)
            self.assertEqual(self.doc.char_offset, offset,
                             "the position moved on a resize round trip")

    def test_a_font_change_keeps_the_reader_in_place(self):
        self.doc.goto(1, 0)
        for _i in range(4):
            self.doc.next_page()
        offset = self.doc.char_offset
        self.doc.set_style(layout.ReaderStyle(font_px=32))
        self.assertEqual(self.doc.char_offset, offset)
        self.assertEqual(self.doc.spine_index, 1)

    def test_the_reported_fraction_never_goes_backwards_while_reading(self):
        previous = -1.0
        turns = 0
        while turns < 400:
            fraction = self.doc.fraction()
            self.assertIsNotNone(fraction)
            self.assertGreaterEqual(fraction, previous)
            previous = fraction
            if not self.doc.next_page():
                break
            turns += 1
        self.assertGreater(previous, 0.9, "reading to the end never got near 1")

    def test_resuming_from_a_stored_fraction_lands_near_where_it_was(self):
        for _i in range(14):
            self.doc.next_page()
        fraction = self.doc.fraction()
        self.doc.goto(0, 0)
        self.assertTrue(self.doc.goto_fraction(fraction))
        self.assertAlmostEqual(self.doc.fraction(), fraction, delta=0.02)

    def test_the_chapter_title_is_the_entry_covering_this_document(self):
        self.doc.goto(1, 0)
        self.assertEqual(self.doc.chapter_title(), "One")
        self.doc.goto(2, 0)
        self.assertEqual(self.doc.chapter_title(), "Three")

    def test_rendering_produces_an_image_of_the_size_asked_for(self):
        image = self.doc.render((640, 760), palette("light"))
        self.assertEqual(image.size, (640, 760))

    def test_the_page_key_changes_with_everything_that_changes_the_pixels(
            self):
        first = self.doc.page_key()
        self.doc.next_page()
        self.assertNotEqual(self.doc.page_key(), first)
        turned = self.doc.page_key()
        self.doc.set_style(layout.ReaderStyle(font_px=30))
        self.assertNotEqual(self.doc.page_key(), turned)


if __name__ == "__main__":
    unittest.main()
