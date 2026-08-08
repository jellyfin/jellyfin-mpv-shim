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
        self.addCleanup(package.close)
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
        self.addCleanup(package.close)
        with self.assertRaises(TooLarge):
            package.archive.read("OEBPS/big.txt", limit=4096)

    def test_a_name_that_escapes_the_archive_resolves_to_nothing(self):
        path = build_epub(self.path(), ["<p>hi</p>"])
        package = open_epub(path)
        self.addCleanup(package.close)
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
        self.addCleanup(package.close)
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
        self.addCleanup(package.close)
        self.assertEqual(len(package.spine), 1)

    def test_an_image_href_resolves_against_its_own_document(self):
        """Not against the OPF — that is the mistake that puts every
        picture in a ``text/`` subfolder one directory too high."""
        path = build_epub(
            self.path(),
            [("text/ch1.xhtml", xhtml('<img src="../img/p.png"/>'))],
            extra={"img/p.png": png_bytes()})
        package = open_epub(path)
        self.addCleanup(package.close)
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
            self.addCleanup(package.close)
            blocks, _chars = content.parse_spine_item(package, 0, {})
            self.assertGreater(blocks[0].spans[0].style.scale, 1.9)


if __name__ == "__main__":
    unittest.main()
