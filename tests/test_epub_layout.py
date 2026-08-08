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


class TestNovelTypographyOnThePage(unittest.TestCase):
    """The half of the typography that only exists once it is laid out."""

    def paginate(self, body, sheet_css=None, width=COLUMN[0], height=10000,
                 **style):
        return layout.paginate(blocks_of(body, sheet_css), width, height,
                               measurer(**style))

    def lines(self, *a, **kw):
        return [ln for page in self.paginate(*a, **kw) for ln in page.items
                if isinstance(ln, layout.Line)]

    DROPCAP = "span.dc{font-size:3.4em}"

    def test_a_drop_capital_is_drawn_once_and_hangs_in_the_margin(self):
        lines = self.lines('<p><span class="dc">T</span>he morning came in '
                           + "grey over the harbour and stayed there. " * 6
                           + "</p>", self.DROPCAP)
        caps = [p for ln in lines for p in ln.pieces if p.text == "T"]
        self.assertEqual(len(caps), 1, "the capital was drawn twice or not "
                                       "at all")
        self.assertEqual(caps[0].x, 0, "the capital is not in the margin")
        self.assertIs(caps[0], lines[0].pieces[0])

    def test_the_lines_beside_a_drop_capital_start_after_it(self):
        """And the ones below it do not — a gutter that never closes is a
        paragraph indented for no reason."""
        lines = self.lines('<p><span class="dc">T</span>he morning came in '
                           + "grey over the harbour and stayed there. " * 8
                           + "</p>", self.DROPCAP)
        cap = lines[0].pieces[0]
        beside = lines[0].pieces[1].x
        self.assertGreater(beside, cap.x, "the text overlaps the capital")
        below = [ln.pieces[0].x for ln in lines[3:]]
        self.assertTrue(below, "the fixture is too short to have a below")
        self.assertLess(min(below), beside,
                        "the gutter never closed")

    def test_the_capital_reaches_down_past_the_line_it_is_drawn_on(self):
        lines = self.lines('<p><span class="dc">T</span>he morning came in '
                           + "grey over the harbour. " * 6 + "</p>",
                           self.DROPCAP)
        self.assertGreater(lines[0].pieces[0].dy, 0,
                           "the capital sits on the first baseline, so it "
                           "is a large letter rather than a drop cap")

    def test_a_drop_capital_is_not_split_across_a_page_break(self):
        """The letter goes with the first line and its reach is measured
        from there, so a break underneath it would point off the page.

        Swept over page heights rather than tried once: whether the
        capital lands near the bottom depends on the height, and a single
        height is overwhelmingly likely to put it somewhere safe and pass
        whatever the code does.
        """
        body = ("<p>" + "filler text to push things down. " * 40 + "</p>"
                '<p><span class="dc">T</span>he morning came in grey '
                + "over the harbour and stayed. " * 8 + "</p>")
        checked = 0
        for height in range(300, 620, 7):
            pages = self.paginate(body, self.DROPCAP, height=height)
            for page in pages:
                lines = [ln for ln in page.items
                         if isinstance(ln, layout.Line)]
                caps = [(i, ln) for i, ln in enumerate(lines)
                        if ln.pieces and ln.pieces[0].text == "T"]
                if not caps:
                    continue
                index, line = caps[0]
                checked += 1
                reach = line.y + line.pieces[0].dy + line.ascent
                self.assertLessEqual(
                    reach, height,
                    "at %d px the capital reaches %d px past the page"
                    % (height, reach - height))
                self.assertGreater(
                    len(lines) - index, 1,
                    "at %d px the capital was left alone at the bottom"
                    % height)
        self.assertGreater(checked, 10, "the sweep never found the capital")

    def test_a_paragraph_that_is_only_a_drop_capital_still_draws(self):
        """A chapter number or an ornament set at 2.6em on its own line.
        `_add_dropcap` only fires when there are lines to hang it on, so
        this used to emit nothing at all — no glyph and no gap."""
        lines = self.lines('<p class="num">II</p>', "p.num{font-size:2.6em}")
        self.assertTrue(lines, "the paragraph vanished")
        self.assertEqual("".join(p.text for ln in lines
                                 for p in ln.pieces), "II")

    def test_a_raised_marker_is_moved_up_by_the_body_size(self):
        lines = self.lines("<p>carry.<sup>1</sup> And on she went.</p>")
        marker = [p for ln in lines for p in ln.pieces if p.text == "1"][0]
        plain = [p for ln in lines for p in ln.pieces if "carry" in p.text][0]
        self.assertLess(marker.dy, 0, "the marker is on the baseline")
        self.assertEqual(plain.dy, 0)

    def test_two_markers_at_different_sizes_sit_at_the_same_height(self):
        """The rise is in ems of the *body*, not of the run — otherwise a
        footnote reference and an exponent in one sentence step up the
        line."""
        lines = self.lines(
            '<p>a<sup>1</sup> and b<sup class="tiny">2</sup></p>',
            "sup.tiny{font-size:0.4em}")
        rises = {p.dy for ln in lines for p in ln.pieces
                 if p.text in ("1", "2")}
        self.assertEqual(len(rises), 1, "the two markers are at %r" % rises)

    def test_an_inset_block_is_narrower_on_both_sides(self):
        measure = measurer()
        text = "a quoted sentence that will wrap. " * 6
        quoted = self.lines("<blockquote><p>" + text + "</p></blockquote>")
        plain = self.lines("<p>" + text + "</p>")

        def span(lines):
            # The right EDGE of the last run, not where it starts: a line
            # that begins its last word further left can still end further
            # right, which is exactly what a block that kept the full
            # measure does.
            starts, ends = [], []
            for line in lines:
                if not line.pieces:
                    continue
                starts.append(line.pieces[0].x)
                last = line.pieces[-1]
                ends.append(last.x + measure.width(last.text, last.style))
            return min(starts), max(ends)

        qs, qe = span(quoted)
        ps, pe = span(plain)
        self.assertGreater(qs, ps, "not inset from the left")
        self.assertLess(qe, pe - 1, "not inset from the right")

    def test_the_measure_is_capped_and_the_column_centred(self):
        """Swept across window widths, because the property is about what
        happens as one grows: an uncapped reader is correct at every width
        it was tried at and sets 140-character lines on a maximised one.
        """
        style = layout.ReaderStyle(font_px=20, margin_x=30, max_measure=34.0)
        widest = 0
        for width in range(500, 3000, 37):
            col, left = style.column(width)
            self.assertLessEqual(col, 34.0 * 20 + 1, "the measure ran away")
            self.assertLessEqual(col, width - 2 * 30)
            self.assertEqual(left * 2 + col, width - (width - col) % 2,
                             "the column is not centred")
            widest = max(widest, col)
        self.assertEqual(widest, int(34.0 * 20), "the cap never bound")

    def test_the_cap_follows_the_type_size(self):
        """What is being capped is a count of characters, so a reader at
        36px must get a wider column than one at 15px — a fixed pixel cap
        would give the larger type a third of the words per line."""
        wide = layout.ReaderStyle(font_px=36, margin_x=30).column(4000)[0]
        small = layout.ReaderStyle(font_px=15, margin_x=30).column(4000)[0]
        self.assertGreater(wide, small * 2)

    def test_a_list_marker_hangs_outside_the_text(self):
        lines = self.lines("<ul><li>" + "an item that wraps onto a second "
                           "line because it is long. " * 3 + "</li></ul>")
        marker = lines[0].pieces[0]
        self.assertEqual(marker.text, "\u2022")
        self.assertLess(marker.x, lines[0].pieces[1].x)
        self.assertLess(marker.x, lines[1].pieces[0].x,
                        "the marker is not hanging in the gutter")


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
        """Strictly, which is what the name says.

        ``sorted(offsets) == offsets`` permits exactly the duplicates this
        forbids, and duplicates are the bug: every page broken out of one
        paragraph used to report the same offset, so a resize resolved the
        tie forward and the reported position stood still across the whole
        paragraph. A novel is mostly multi-page paragraphs.
        """
        pages = layout.paginate(blocks_of(paragraphs(30)), COLUMN[0],
                                COLUMN[1], measurer())
        offsets = [p.start_offset for p in pages]
        self.assertEqual(offsets, sorted(offsets))
        self.assertEqual(len(set(offsets)), len(offsets),
                         "pages share a start offset: %r" % (offsets,))

    def test_a_paragraph_longer_than_a_page_still_moves_the_offset(self):
        """The narrow case the above generalises, stated on its own because
        it is the one that reaches the server: one paragraph, several
        pages, and the position has to advance across them."""
        one = "<p>" + " ".join("word%d" % i for i in range(4000)) + "</p>"
        pages = layout.paginate(blocks_of(one), COLUMN[0], COLUMN[1],
                                measurer())
        self.assertGreater(len(pages), 3, "the fixture fits on one page")
        offsets = [p.start_offset for p in pages]
        self.assertEqual(len(set(offsets)), len(offsets), repr(offsets))

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

    def _image(self, body, size=(256, 256), sheet=None, font_px=20):
        pages = layout.paginate(blocks_of(body, sheet), COLUMN[0], COLUMN[1],
                                measurer(font_px=font_px),
                                image_size=lambda _src: size)
        items = [i for p in pages for i in p.items
                 if isinstance(i, layout.ImageItem)]
        self.assertEqual(len(items), 1, "expected exactly one image")
        return items[0]

    def test_an_image_the_book_said_nothing_about_keeps_its_own_size(self):
        """The default has to stay the file's own size, or every ordinary
        illustration changes shape to fix a case that is about icons."""
        image = self._image('<img src="a.png"/>', (256, 256))
        self.assertEqual((image.w, image.h), (256, 256))

    def test_a_css_width_is_honoured_over_the_files_own_size(self):
        """The reported bug: a cookbook's step arrows are 256px PNGs set to
        about a line tall, and every one was drawn a quarter page tall."""
        image = self._image('<img class="arrow" src="a.png"/>', (256, 256),
                            sheet="img.arrow { width: 1em; }", font_px=20)
        self.assertEqual((image.w, image.h), (20, 20))

    def test_a_width_attribute_is_a_number_of_pixels(self):
        """HTML's presentation attribute, which is what an older book
        carries. A bare number there is px; the same bare number in CSS is
        not a length at all."""
        image = self._image('<img src="a.png" width="32"/>', (256, 256),
                            font_px=16)
        self.assertEqual(image.w, 32)

    def test_css_beats_the_attribute(self):
        image = self._image('<img src="a.png" width="200"/>', (256, 256),
                            sheet="img { width: 1em; }", font_px=20)
        self.assertEqual(image.w, 20)

    def test_naming_one_side_keeps_the_aspect_ratio(self):
        image = self._image('<img src="a.png"/>', (400, 200),
                            sheet="img { width: 5em; }", font_px=20)
        self.assertEqual((image.w, image.h), (100, 50))

    def test_naming_both_sides_is_taken_at_its_word(self):
        """An author who gave two numbers meant them; correcting one would
        second-guess the only explicit statement in play."""
        image = self._image('<img src="a.png"/>', (400, 200),
                            sheet="img { width: 5em; height: 5em; }",
                            font_px=20)
        self.assertEqual((image.w, image.h), (100, 100))

    def test_a_percentage_is_of_the_measure_not_of_the_type_size(self):
        image = self._image('<img src="a.png"/>', (400, 200),
                            sheet="img { width: 50%; }")
        self.assertEqual(image.w, COLUMN[0] // 2)

    def test_a_max_height_narrows_an_icon_rather_than_squashing_it(self):
        """`max-height` is the commoner spelling for these little marks,
        and a cap that only clipped the height would distort every one."""
        image = self._image('<img src="a.png"/>', (400, 200),
                            sheet="img { max-height: 1em; }", font_px=20)
        self.assertEqual((image.w, image.h), (40, 20))

    def test_a_cap_larger_than_the_image_leaves_it_alone(self):
        image = self._image('<img src="a.png"/>', (40, 20),
                            sheet="img { max-width: 30em; }", font_px=20)
        self.assertEqual((image.w, image.h), (40, 20))

    def test_a_declared_size_still_cannot_exceed_the_measure(self):
        image = self._image('<img src="a.png"/>', (100, 100),
                            sheet="img { width: 400em; }", font_px=20)
        self.assertEqual(image.w, COLUMN[0])

    def test_an_auto_height_is_not_a_size(self):
        """`width: 100%; height: auto` is the commonest rule in any epub,
        and reading `auto` as a length draws every image a hair tall."""
        image = self._image('<img src="a.png"/>', (400, 200),
                            sheet="img { width: 5em; height: auto; }",
                            font_px=20)
        self.assertEqual((image.w, image.h), (100, 50))

    def test_the_declared_size_scales_with_the_readers_type_size(self):
        """Ems, not pixels: an icon sized against the text stays sized
        against the text when the reader turns the type up."""
        small = self._image('<img src="a.png"/>', (256, 256),
                            sheet="img { width: 1em; }", font_px=16)
        large = self._image('<img src="a.png"/>', (256, 256),
                            sheet="img { width: 1em; }", font_px=32)
        self.assertEqual((small.w, large.w), (16, 32))

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
            # `min(before, page.end_offset)` here made this unfalsifiable —
            # x >= min(y, x) holds for every x — and this is the guard for
            # exactly the bug that shipped underneath it.
            self.assertGreaterEqual(
                page.end_offset, before,
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
