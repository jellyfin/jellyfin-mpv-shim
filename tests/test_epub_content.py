"""Parsing a book: the archive, the markup, the stylesheet, the offsets.

Two things here are safety rather than fidelity and are worth naming. The
first is that **nothing in this package parses XML with expat**: an epub
comes off a media server, which got it from whatever the user put in their
library, and `xml.etree` expands internal entities — measured at 3000
characters for a three-level "billion laughs" on CPython 3.13, gigabytes at
nine. The second is that every read out of the zip is bounded by what it
actually delivers rather than by the size the archive claims.

The rest is fidelity, and the case it keeps coming back to is that
published epubs do not use ``<h1>``. They use ``<p class="chaptertitle">``
and a stylesheet, so a reader that ignores CSS shows a novel as a
featureless wall of identical paragraphs.
"""

import os
import tempfile
import unittest
import zipfile

from jellyfin_mpv_shim.epub import content, css, xmlish
from jellyfin_mpv_shim.epub.archive import (EpubError, TooLarge, open_epub)

from tests._epub_fixtures import build_epub, paragraphs, png_bytes, xhtml

BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
]>
<html><body><p>&lol3;</p></body></html>"""


class TestMarkupSafety(unittest.TestCase):
    def test_an_entity_bomb_does_not_expand(self):
        root = xmlish.parse(BOMB)
        # The reference is left as text rather than expanded. 3000
        # characters here would be the three-level version working; the
        # nine-level one is what takes the machine down.
        self.assertLess(len(root.text()), 200)

    def test_a_doctype_is_dropped_whole(self):
        root = xmlish.parse(BOMB)
        self.assertNotIn("ENTITY", root.text())

    def test_standard_character_references_still_resolve(self):
        root = xmlish.parse("<p>a &amp; b &#8212; c</p>")
        self.assertEqual(root.text(), "a & b — c")

    def test_malformed_markup_recovers_instead_of_raising(self):
        """Unclosed tags, mismatched nesting and bare ampersands are all
        routine in shipped books. An XML parser is required to stop on each
        of them; a reader that stops has refused the book."""
        for broken in ("<p>one<p>two", "<b><i>x</b></i>", "<p>a & b</p>",
                       "<p>unterminated", "</p></div>", ""):
            blocks, _chars = content.parse_document(broken)
            self.assertIsInstance(blocks, list)

    def test_a_document_that_will_not_finish_is_given_up_on(self):
        """The parser is fast on everything measured, so this is insurance
        rather than a fix for a known input — but it has to actually
        *stop*, which is why the deadline is checked inside the parse and
        not by abandoning a thread that goes on burning CPU."""
        with self.assertRaises(xmlish.ParseTimeout):
            xmlish.parse("<p>x</p>" * 200000, timeout=0.001)

    def test_giving_up_is_the_same_answer_as_an_unreadable_document(self):
        """Half a dozen callers already catch `EpubError` to mean "skip
        this document and carry on". A timeout that needed its own
        `except` would be one somebody forgets, and a hostile chapter
        would take the whole book with it."""
        from jellyfin_mpv_shim.epub.errors import EpubError as Base

        self.assertTrue(issubclass(xmlish.ParseTimeout, Base))
        self.assertTrue(issubclass(xmlish.ParseTimeout, EpubError))

    def test_a_deeply_nested_document_still_reads(self):
        """900 nested elements is a `RecursionError` in the walk, because
        CPython allows 1000 frames — reachable by a generated book without
        any malice. Past the cap the nesting is dropped and the text is
        kept, which is the right half to lose."""
        depth = 5000
        markup = ("<html><body>" + "<div>" * depth + "words in here"
                  + "</div>" * depth + "</body></html>")
        blocks, _chars = content.parse_document(markup)
        self.assertEqual([b.text() for b in blocks], ["words in here"])

    def test_the_depth_cap_does_not_bite_a_real_document(self):
        markup = "<html><body>" + "<div>" * 30 + (
            "<p>a <em>real</em> book</p>") + "</div>" * 30 + "</body></html>"
        blocks, _chars = content.parse_document(markup)
        self.assertEqual([b.text() for b in blocks], ["a real book"])

    def test_document_order_is_preserved(self):
        """`find_all` walks pre-order. Reversing it silently reverses the
        spine, and a book opens at its last chapter."""
        root = xmlish.parse("<r><a id='1'/><a id='2'/><a id='3'/></r>")
        self.assertEqual([n.get("id") for n in root.find_all("a")],
                         ["1", "2", "3"])


class TestArchive(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def path(self, name="book.epub"):
        return os.path.join(self.dir, name)

    def test_a_book_parses_into_metadata_spine_and_toc(self):
        path = build_epub(self.path(), ["<p>one</p>", "<p>two</p>"],
                          title="T", author="A",
                          toc=[("First", "ch1.xhtml"),
                               ("Second", "ch2.xhtml")])
        package = open_epub(path)
        self.assertEqual((package.title, package.author), ("T", "A"))
        self.assertEqual(len(package.spine), 2)
        self.assertEqual([t.title for t in package.toc], ["First", "Second"])
        self.assertEqual([t.spine_index for t in package.toc], [0, 1])

    def test_an_entry_larger_than_its_cap_is_refused_by_what_it_delivers(
            self):
        """Not by the size in the header, which is attacker-controlled: a
        few kilobytes of zeroes declare whatever they like and expand to
        gigabytes."""
        path = self.path("bomb.epub")
        build_epub(path, ["<p>hi</p>"])
        with zipfile.ZipFile(path, "a") as archive:
            archive.writestr("OEBPS/big.txt", b"\0" * (1 << 20))
        package = open_epub(path)
        with self.assertRaises(TooLarge):
            package.archive.read("OEBPS/big.txt", limit=4096)

    def _handles_on(self, path):
        """How many descriptors this process has open on ``path``.

        ``/proc`` because there is no portable way to ask, and Linux is
        where the suite runs. The property is asserted on Windows by the
        second half of the test below, which is where it actually bites:
        an open handle there is a *lock*, so a book being read cannot be
        deleted from the downloads screen.
        """
        fds = "/proc/self/fd"
        if not os.path.isdir(fds):
            self.skipTest("no /proc to count descriptors with")
        target = os.path.realpath(path)
        count = 0
        for name in os.listdir(fds):
            try:
                if os.path.realpath(os.path.join(fds, name)) == target:
                    count += 1
            except OSError:
                pass
        return count

    def test_the_archive_holds_no_handle_between_reads(self):
        """Over a whole session, not one read.

        Several reads, of several kinds, with a count after each: a handle
        opened per read and closed is invisible to a check taken once at
        the end, and a handle *cached* on the first read is invisible to a
        check taken before any read happens. The count has to stay at zero
        across the sequence, which is the only shape that says "there is
        nothing to close".
        """
        path = build_epub(self.path(), ["<p>one</p>", "<p>two</p>"],
                          extra={"pic.png": png_bytes()})
        self.assertEqual(self._handles_on(path), 0)
        package = open_epub(path)
        self.assertEqual(self._handles_on(path), 0, "opening kept a handle")
        for index in range(len(package.spine)):
            package.doc_bytes(index)
            self.assertEqual(self._handles_on(path), 0,
                             "reading a document kept a handle")
        package.archive.read("OEBPS/pic.png")
        self.assertEqual(self._handles_on(path), 0,
                         "reading an image kept a handle")
        self.assertFalse(
            [v for v in vars(package.archive).values()
             if isinstance(v, zipfile.ZipFile)],
            "the archive is holding a ZipFile between reads")

    def test_a_book_can_be_deleted_while_it_is_open(self):
        """The Windows half of the rule above, stated where it is testable.

        On Windows an open handle refuses the delete outright; on Linux the
        unlink succeeds either way and the *read* is what notices. Both
        report the same thing here — a reader that is holding the file open
        makes the downloads screen unable to remove the book.
        """
        path = build_epub(self.path(), ["<p>one</p>"])
        package = open_epub(path)
        package.doc_bytes(0)
        os.unlink(path)
        with self.assertRaises(EpubError):
            package.doc_bytes(0)

    def test_a_name_that_escapes_the_archive_resolves_to_nothing(self):
        path = build_epub(self.path(), ["<p>hi</p>"])
        package = open_epub(path)
        self.assertFalse(package.archive.exists("../../etc/passwd"))
        with self.assertRaises(EpubError):
            package.archive.read("OEBPS/../../../etc/passwd")

    def test_a_book_with_no_container_still_opens(self):
        """A zip of an unpacked epub with META-INF lost in the round trip
        is common enough to be worth recovering from, and there is exactly
        one .opf to find."""
        path = build_epub(self.path(), ["<p>hi</p>"])
        stripped = self.path("stripped.epub")
        with zipfile.ZipFile(path) as source, \
                zipfile.ZipFile(stripped, "w") as target:
            for info in source.infolist():
                if info.filename != "META-INF/container.xml":
                    target.writestr(info.filename, source.read(info.filename))
        package = open_epub(stripped)
        self.assertEqual(len(package.spine), 1)

    def test_a_missing_spine_document_is_skipped_not_fatal(self):
        """A chapter that raises when the reader reaches it looks like a
        crash; a book one chapter short looks like a damaged book."""
        path = build_epub(self.path(), ["<p>one</p>", "<p>two</p>"])
        pruned = self.path("pruned.epub")
        with zipfile.ZipFile(path) as source, \
                zipfile.ZipFile(pruned, "w") as target:
            for info in source.infolist():
                if not info.filename.endswith("ch2.xhtml"):
                    target.writestr(info.filename, source.read(info.filename))
        package = open_epub(pruned)
        self.assertEqual(len(package.spine), 1)

    def test_an_image_href_resolves_against_its_own_document(self):
        """Not against the OPF — that is the mistake that puts every
        picture in a ``text/`` subfolder one directory too high."""
        path = build_epub(
            self.path(),
            [("text/ch1.xhtml", xhtml('<img src="../img/p.png"/>'))],
            extra={"img/p.png": png_bytes()})
        package = open_epub(path)
        resolved = package.resolve("OEBPS/text/ch1.xhtml", "../img/p.png")
        self.assertEqual(resolved, "OEBPS/img/p.png")
        self.assertTrue(package.archive.exists(resolved))


class TestContent(unittest.TestCase):
    def blocks(self, body, sheet_css=None):
        sheet = None
        if sheet_css:
            sheet = css.Stylesheet()
            sheet.add(sheet_css)
        return content.parse_document(xhtml(body), sheet=sheet)

    def test_headings_emphasis_and_lists_survive(self):
        blocks, _chars = self.blocks(
            "<h2>Title</h2><p>plain <b>bold</b> <i>it</i></p>"
            "<ul><li>one</li><li>two</li></ul><hr/>")
        kinds = [b.kind for b in blocks]
        self.assertEqual(kinds[0], content.HEADING)
        self.assertIn(content.RULE, kinds)
        paragraph = blocks[1]
        self.assertTrue(any(s.style.bold for s in paragraph.spans))
        self.assertTrue(any(s.style.italic for s in paragraph.spans))
        self.assertEqual([b.marker for b in blocks if b.marker], ["•", "•"])

    def test_a_stylesheet_makes_a_paragraph_a_chapter_title(self):
        """The case every published epub is: no ``<h1>`` anywhere, a
        ``<p class="chaptertitle">`` and a stylesheet that sizes it."""
        blocks, _chars = self.blocks(
            '<p class="chaptertitle">Chapter 1</p><p class="para">body</p>',
            "p.chaptertitle{font-size:1.5em;font-weight:bold;"
            "text-align:center} p.para{text-indent:1em}")
        title, body = blocks[0], blocks[1]
        self.assertGreater(title.spans[0].style.scale, 1.4)
        self.assertTrue(title.spans[0].style.bold)
        self.assertEqual(title.align, "center")
        self.assertAlmostEqual(body.first_indent, 1.0, places=3)

    def test_an_inline_style_beats_the_sheet(self):
        blocks, _chars = self.blocks(
            '<p class="x" style="font-style:italic">a</p>',
            "p.x{font-style:normal}")
        self.assertTrue(blocks[0].spans[0].style.italic)

    def test_font_weight_normal_turns_bold_back_off(self):
        """A one-way reading looks harmless until a book styles emphasis by
        class inside a ``<strong>`` for older readers, and everything is
        bold."""
        blocks, _chars = self.blocks(
            '<b><span class="q">a</span></b>', ".q{font-weight:normal}")
        self.assertFalse(blocks[0].spans[0].style.bold)

    def test_hidden_content_is_dropped_but_still_counted(self):
        """Dropped because it is not drawn; counted because epub.js's tree
        walker counts it, and our number has to be theirs."""
        blocks, chars = self.blocks(
            '<p style="display:none">hidden</p><p>shown</p>')
        self.assertEqual([b.text() for b in blocks], ["shown"])
        self.assertEqual(chars, len("hidden") + len("shown"))

    def test_span_offsets_track_the_counting_rule_not_the_rendered_text(self):
        """Normalization is lossy — the offsets cannot be recovered from
        the spans afterwards, which is why they are recorded during the
        walk."""
        blocks, chars = self.blocks("<p>one</p>\n   \n<p>two</p>")
        self.assertEqual([b.spans[0].char_offset for b in blocks], [0, 3])
        self.assertEqual(chars, 6, "whitespace-only nodes were counted")

    def test_a_descendant_selector_is_matched_against_real_ancestors(self):
        blocks, _chars = self.blocks(
            '<div class="story"><p>a</p></div><p>b</p>',
            ".story p{font-style:italic}")
        self.assertTrue(blocks[0].spans[0].style.italic)
        self.assertFalse(blocks[1].spans[0].style.italic)

    def test_a_selector_this_parser_cannot_read_is_dropped_not_guessed(self):
        """Applying a heading's size to a page of body text is far more
        damaging than not applying it."""
        blocks, _chars = self.blocks(
            '<p>a</p>', 'p:first-child{font-size:3em} p[data-x]{font-size:2em}')
        self.assertAlmostEqual(blocks[0].spans[0].style.scale, 1.0)

    def test_an_image_becomes_its_own_block(self):
        blocks, _chars = content.parse_document(
            xhtml('<p>before</p><img src="p.png" alt="A picture"/>'),
            base_href="OEBPS/ch1.xhtml",
            resolve=lambda base, href: "OEBPS/" + href)
        image = [b for b in blocks if b.kind == content.IMAGE]
        self.assertEqual(len(image), 1)
        self.assertEqual(image[0].src, "OEBPS/p.png")
        self.assertEqual(image[0].alt, "A picture")

    def test_a_stylesheet_is_read_from_the_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = build_epub(os.path.join(tmp, "b.epub"),
                              ['<p class="t">Title</p>' + paragraphs(2)],
                              css="p.t{font-size:2em;font-weight:bold}")
            package = open_epub(path)
            blocks, _chars = content.parse_spine_item(package, 0, {})
            self.assertGreater(blocks[0].spans[0].style.scale, 1.9)


class TestNovelTypography(unittest.TestCase):
    """The things a published novel is made of, past paragraphs.

    Each of these renders *something* when it is not implemented — a list
    without its bullets is still a list of lines, a drop capital is still a
    large letter — which is why they are asserted rather than eyeballed:
    the failure mode is a page that looks nearly right.
    """

    def blocks(self, body, sheet_css=None):
        from jellyfin_mpv_shim.epub import css as cssmod

        sheet = None
        if sheet_css:
            sheet = cssmod.Stylesheet()
            sheet.add(sheet_css)
        return content.parse_document(xhtml(body), sheet=sheet)[0]

    def markers(self, body, sheet_css=None):
        return [b.marker for b in self.blocks(body, sheet_css) if b.marker
                or b.text().strip()]

    # -- lists ------------------------------------------------------------

    def test_a_bulleted_list_gets_bullets_and_a_numbered_one_numbers(self):
        blocks = self.blocks(
            "<ul><li>one</li><li>two</li></ul>"
            "<ol><li>first</li><li>second</li></ol>")
        items = [b for b in blocks if b.marker]
        self.assertEqual([b.marker for b in items],
                         ["\u2022", "\u2022", "1.", "2."])

    def test_a_nested_list_restarts_its_numbering_and_changes_its_bullet(
            self):
        """Both are what a browser does, and both matter in a book: a
        sub-list that continues the outer numbering reads as one list
        wrongly indented, and one that repeats the outer bullet loses the
        distinction the nesting was for."""
        blocks = self.blocks(
            "<ol><li>outer one<ol><li>inner one</li><li>inner two</li></ol>"
            "</li><li>outer two</li></ol>"
            "<ul><li>a<ul><li>b</li></ul></li></ul>")
        markers = [b.marker for b in blocks if b.marker]
        self.assertEqual(markers[:4], ["1.", "1.", "2.", "2."])
        self.assertEqual(markers[4:], ["\u2022", "\u25e6"])

    def test_a_nested_list_is_indented_past_its_parent(self):
        blocks = self.blocks(
            "<ol><li>outer<ol><li>inner</li></ol></li></ol>")
        items = [b for b in blocks if b.marker]
        self.assertGreater(items[1].indent, items[0].indent)

    def test_an_ordered_list_honours_type_and_start(self):
        self.assertEqual(
            [b.marker for b in self.blocks(
                '<ol type="a"><li>x</li><li>y</li></ol>') if b.marker],
            ["a.", "b."])
        self.assertEqual(
            [b.marker for b in self.blocks(
                '<ol type="I"><li>x</li><li>y</li><li>z</li><li>w</li>'
                '</ol>') if b.marker],
            ["I.", "II.", "III.", "IV."])
        self.assertEqual(
            [b.marker for b in self.blocks(
                '<ol start="7"><li>x</li><li>y</li></ol>') if b.marker],
            ["7.", "8."])

    def test_list_style_type_none_leaves_no_marker(self):
        """A cast of characters or a list of dates, set as a list because
        that is what it is, with nothing down the side of the page."""
        blocks = self.blocks("<ul><li>Anna</li><li>Piet</li></ul>",
                             "ul{list-style-type:none}")
        items = [b for b in blocks if b.text().strip()]
        self.assertEqual([b.marker for b in items], ["", ""])

    def test_the_margin_shorthand_is_expanded(self):
        """Books write the shorthand far more often than the longhands —
        one real title's stylesheet uses it 40 times against 32 — so
        dropping it drops most of what the book says about its insets."""
        blocks = self.blocks('<div class="s"><p>quoted</p></div>',
                             "div.s{margin:10px 2em}")
        inset = [b for b in blocks if "quoted" in b.text()][0]
        self.assertAlmostEqual(inset.indent, 2.0, places=3)
        self.assertAlmostEqual(inset.indent_right, 2.0, places=3)

    def test_a_percentage_margin_is_read_against_the_measure(self):
        """`12.5%` as `0.125em` is a fifth of a character — not a wrong
        inset so much as no inset, which is the failure the property is
        there to prevent."""
        blocks = self.blocks('<div class="s"><p>quoted</p></div>',
                             "div.s{margin:10px 12.5%}")
        self.assertGreater(blocks[0].indent, 3.0)

    def test_the_list_style_shorthand_is_expanded(self):
        blocks = self.blocks("<ul><li>Anna</li><li>Piet</li></ul>",
                             "ul{list-style:none}")
        items = [b for b in blocks if b.text().strip()]
        self.assertEqual([b.marker for b in items], ["", ""])

    def test_a_shorthand_keyword_this_reader_cannot_draw_is_left_alone(self):
        blocks = self.blocks("<ul><li>x</li></ul>",
                             "ul{list-style:url(dot.png) outside}")
        self.assertEqual(blocks[0].marker, "\u2022")

    def test_characters_are_counted_as_epub_js_counts_them(self):
        """UTF-16 code units, not code points: `node.length` in the DOM is
        specified that way, so a non-BMP character costs two there."""
        blocks, counted = content.parse_document(
            xhtml("<p>hi \U0001F600 there</p>"))
        self.assertEqual(counted, len("hi \U0001F600 there") + 1)

    def test_a_stylesheet_can_change_the_numbering(self):
        blocks = self.blocks("<ol><li>x</li><li>y</li></ol>",
                             "ol{list-style-type:lower-roman}")
        self.assertEqual([b.marker for b in blocks if b.marker],
                         ["i.", "ii."])

    # -- quoted and inset matter ------------------------------------------

    def test_a_block_quote_is_inset_on_both_sides(self):
        """One-sided, it reads as a paragraph that lost its indent."""
        blocks = self.blocks("<p>before</p><blockquote><p>quoted</p>"
                             "</blockquote><p>after</p>")
        quoted = [b for b in blocks if "quoted" in b.text()][0]
        plain = [b for b in blocks if "before" in b.text()][0]
        self.assertGreater(quoted.indent, plain.indent)
        self.assertGreater(quoted.indent_right, plain.indent_right)

    def test_a_stylesheet_margin_insets_the_block(self):
        """How verse, an epigraph and a quoted letter all say "moved in"."""
        blocks = self.blocks('<p class="verse">and the tide came in</p>',
                             "p.verse{margin-left:2em}")
        self.assertAlmostEqual(blocks[0].indent, 2.0, places=3)

    def test_a_negative_margin_is_dropped_rather_than_applied(self):
        blocks = self.blocks('<p class="pull">out</p>',
                             "p.pull{margin-left:-4em}")
        self.assertEqual(blocks[0].indent, 0.0)

    def test_a_definition_term_is_set_bold(self):
        blocks = self.blocks("<dl><dt>Chandler</dt><dd>sells rope</dd></dl>")
        term = [b for b in blocks if "Chandler" in b.text()][0]
        body = [b for b in blocks if "rope" in b.text()][0]
        self.assertTrue(term.spans[0].style.bold)
        self.assertFalse(body.spans[0].style.bold)
        self.assertGreater(body.indent, term.indent)

    # -- runs of text -----------------------------------------------------

    def test_a_footnote_marker_is_raised_off_the_baseline(self):
        blocks = self.blocks("<p>carry.<sup>1</sup></p>")
        marker = [s for s in blocks[0].spans if s.text == "1"][0]
        self.assertGreater(marker.style.rise, 0)
        self.assertLess(marker.style.scale, 1.0)

    def test_a_subscript_goes_the_other_way(self):
        blocks = self.blocks("<p>H<sub>2</sub>O</p>")
        marker = [s for s in blocks[0].spans if s.text == "2"][0]
        self.assertLess(marker.style.rise, 0)

    def test_vertical_align_raises_a_run_that_is_not_a_sup(self):
        """A footnote reference is as often a styled anchor as a `<sup>`."""
        blocks = self.blocks('<p>carry.<a class="n">1</a></p>',
                             "a.n{vertical-align:super}")
        marker = [s for s in blocks[0].spans if s.text == "1"][0]
        self.assertGreater(marker.style.rise, 0)

    def test_small_caps_uppercases_and_shrinks_only_what_was_lowercase(self):
        """The synthetic small cap: a capital that was already a capital
        must stay full height, or the effect is just smaller text."""
        blocks = self.blocks('<p><span class="sc">Marisol</span> woke</p>',
                             "span.sc{font-variant:small-caps}")
        runs = [s for s in blocks[0].spans if s.text.strip()]
        self.assertEqual(runs[0].text, "M")
        self.assertEqual(runs[1].text, "ARISOL")
        self.assertEqual(runs[0].style.scale, 1.0)
        self.assertLess(runs[1].style.scale, 1.0)
        self.assertEqual("".join(r.text for r in runs[:2]), "MARISOL")

    def test_copying_small_caps_gives_back_what_the_author_wrote(self):
        """The uppercasing is a substitute for a face we do not have, not
        something the author typed. Copied verbatim it SHOUTS, and small
        caps mark proper nouns and chapter openings — the sentences people
        quote."""
        blocks = self.blocks('<p><span class="sc">Marisol</span> woke</p>',
                             "span.sc{font-variant:small-caps}")
        self.assertEqual(blocks[0].text().split()[0], "MARISOL")
        self.assertEqual(blocks[0].plain_text(), "Marisol woke")

    def test_small_caps_keeps_one_offset_for_one_text_node(self):
        """Splitting a run does not create positions in epub.js's index —
        the offsets are per text node, and inventing more would report a
        place no other client can resolve."""
        blocks = self.blocks('<p><span class="sc">Marisol</span></p>',
                             "span.sc{font-variant:small-caps}")
        offsets = {s.char_offset for s in blocks[0].spans}
        self.assertEqual(len(offsets), 1)

    # -- drop capitals ----------------------------------------------------

    def test_a_large_first_letter_is_recognised_as_a_drop_capital(self):
        blocks = self.blocks(
            '<p><span class="dc">T</span>he morning came</p>',
            "span.dc{font-size:3.4em}")
        self.assertTrue(blocks[0].dropcap)
        self.assertEqual(blocks[0].dropcap_span().text, "T")

    def test_an_ordinary_paragraph_is_not_a_drop_capital(self):
        for body, css in (("<p>The morning came</p>", None),
                          ('<p><em>The</em> morning came</p>', None),
                          ('<p><span class="dc">The morning</span> came</p>',
                           "span.dc{font-size:3em}")):
            with self.subTest(body=body):
                self.assertFalse(self.blocks(body, css)[0].dropcap)

    def test_a_list_item_is_never_a_drop_capital(self):
        blocks = self.blocks('<ul><li><span class="dc">T</span>hing</li>'
                             "</ul>", "span.dc{font-size:3em}")
        self.assertFalse(blocks[0].dropcap)

    # -- what came off the disk -------------------------------------------

    def test_a_document_of_nothing_but_end_tags_is_given_up_on(self):
        """The deadline is checked from the handlers, so it has to be
        checked from ALL of them — a document with no start tags and no
        text has nowhere else to fire."""
        with self.assertRaises(xmlish.ParseTimeout):
            xmlish.parse("</p>" * 200000, timeout=0.001)

    def test_a_utf16_document_is_decoded_not_mangled(self):
        """cp1252 maps every byte, so the fallback chain never fails on
        UTF-16: it comes back NUL-interleaved, which is unreadable text and
        twice the character count every other client sees."""
        markup = xhtml("<p>hello there</p>")
        for bom in ("utf-16-le", "utf-16-be"):
            with self.subTest(encoding=bom):
                raw = markup.encode("utf-16")
                if bom == "utf-16-be":
                    raw = b"\xfe\xff" + markup.encode("utf-16-be")
                text = xmlish.decode(raw)
                self.assertIn("hello there", text)
                self.assertNotIn("\x00", text)

    def test_windows_line_endings_do_not_reach_the_page(self):
        """A CR has no glyph in any face, so it draws as a tofu box at the
        end of every line of a code listing — and books converted on
        Windows are full of them. XML says the parser normalizes these."""
        blocks = self.blocks("<pre>one\r\ntwo\r\n</pre>")
        self.assertNotIn("\r", blocks[0].text())
        self.assertIn("one\ntwo", blocks[0].text())


if __name__ == "__main__":
    unittest.main()
