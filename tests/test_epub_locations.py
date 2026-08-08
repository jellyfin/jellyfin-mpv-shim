"""The locations index — the number every other Jellyfin client divides by.

This is a port of epub.js's `Locations.parse`, and the only thing that
makes a port right is agreeing with the original on the cases where the
obvious implementation disagrees. So most of what is pinned here is the
*odd* behaviour, traced by hand from the source in
``node_modules/epubjs/src/locations.js``:

* a run started inside a text node costs ``break + 1`` characters;
* a run continued from the previous node costs exactly ``break``;
* a whitespace-only text node contributes nothing;
* every section closes its tail as a whole location — unconditionally, so
  a section ending exactly on a boundary contributes a degenerate extra one;
* ``total`` is one less than the number of locations.

Get any of these wrong and nothing fails visibly: the reader still opens,
still turns pages, still reports *a* percentage. It is simply a different
percentage from the one jellyfin-web wrote, so the book resumes in the
wrong place on whichever client the user picks up next.
"""

import os
import tempfile
import unittest

from jellyfin_mpv_shim.epub import content, locations
from jellyfin_mpv_shim.epub.archive import open_epub

from tests._epub_fixtures import build_epub, paragraphs, xhtml


class TestCountSection(unittest.TestCase):
    """``count_section`` against hand-traced expectations."""

    def test_a_node_shorter_than_the_break_is_one_location(self):
        chars, ends = locations.count_section([500], brk=1024)
        self.assertEqual(chars, 500)
        self.assertEqual(ends, [500], "the tail close is missing")

    def test_a_run_started_inside_a_node_costs_one_extra_character(self):
        """`pos += 1` happens after `dist` is computed, so the first run in
        a node consumes 1025 rather than 1024. Off by one per ~1024
        characters compounds to whole locations over a chapter."""
        _chars, ends = locations.count_section([5000], brk=1024)
        self.assertEqual(ends[:4], [1025, 2050, 3075, 4100])
        self.assertEqual(ends[-1], 5000, "the tail is not closed at the end")
        self.assertEqual(len(ends), 5)

    def test_a_run_continued_from_the_previous_node_costs_exactly_the_break(
            self):
        # 600 accumulates with no location; the second node then completes
        # the run 424 characters in — not 425, because the +1 is only paid
        # by a run that STARTS inside a node.
        _chars, ends = locations.count_section([600, 2000], brk=1024)
        self.assertEqual(ends[0], 600 + 424)

    def test_an_empty_section_contributes_nothing(self):
        self.assertEqual(locations.count_section([], brk=1024), (0, []))

    def test_the_two_walks_over_one_document_count_the_same(self):
        """**The only test that can catch a whole class of bug**, and the
        one that was missing.

        There are two independent walks over the same tree: `content`'s,
        which records a character offset on every span, and `locations`',
        which measures the sections. If they ever disagree, every stored
        reading position is wrong by the difference — and nothing else in
        the suite compares them, because each is tested against its own
        expectations.

        This replaces an assertion that read
        ``assertEqual(count_section([1500]), count_section([1500]))`` — the
        same call on both sides, true of every possible implementation. It
        occupied the name of the whitespace rule, which is in fact pinned
        by ``test_whitespace_between_tags_is_dropped`` below.

        The markup deliberately includes the shapes where the two walks
        take *different* branches: svg wrappers (which the content walk
        skips for drawing), script/style (invisible but counted), hidden
        blocks, and stray inter-tag whitespace.
        """
        cases = {
            "svg wrapper": "<svg><title>Cover Image Here</title>"
                           "<image href='c.jpg'/></svg><p>after</p>",
            "svg labels": "<p>before</p><svg><text>x axis</text>"
                          "<text>y axis</text></svg><p>after</p>",
            "script and style": "<p>shown</p><script>var x = 1;</script>"
                                "<style>p{color:red}</style>",
            "hidden": "<div hidden>secret words</div><p>shown</p>",
            "image with alt": '<p>a</p><img src="p.png" alt="a picture"/>'
                              "<p>b</p>",
            "whitespace between tags": "<p>one</p>\n   \n<p>two</p>",
            "nested inline": "<p>plain <em>emph <strong>both</strong></em>"
                             " tail</p>",
        }
        for name, body in cases.items():
            with self.subTest(case=name):
                markup = xhtml(body)
                counted = content.parse_document(markup)[1]
                measured = sum(locations.text_node_lengths(markup))
                self.assertEqual(
                    counted, measured,
                    "the content walk counted %d and the locations walk "
                    "%d for %s — every position after the difference is "
                    "wrong by it" % (counted, measured, name))

    def test_a_section_with_text_always_contributes_at_least_one_location(
            self):
        for length in (1, 12, 1023, 1024, 1025):
            _chars, ends = locations.count_section([length], brk=1024)
            self.assertGreaterEqual(len(ends), 1, "%d chars gave none"
                                    % length)

    def test_the_tail_close_is_unconditional(self):
        """Verified against the source: epub.js closes with
        ``if (range && range.startContainer && prev)`` and never checks
        whether the range was already pushed, so a section landing exactly
        on a boundary contributes one more. Our total has to include it or
        it is smaller than the one other clients divide by."""
        # 1025 exactly completes a run started inside the node, leaving
        # counter at 0 — and epub.js still pushes.
        _chars, ends = locations.count_section([1025, 1024], brk=1024)
        self.assertEqual(len(ends), 2)


class TestIndex(unittest.TestCase):
    def index_for(self, chapters, linear=None):
        path = os.path.join(self.dir, "book.epub")
        build_epub(path, chapters, linear=linear)
        package = open_epub(path)
        return package, locations.build(package)

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = self._tmp.name

    def test_total_is_one_less_than_the_count(self):
        _pkg, index = self.index_for([paragraphs(20), paragraphs(20)])
        self.assertEqual(index.total, index.count - 1)

    def test_non_linear_documents_are_excluded_entirely(self):
        """epub.js enqueues only sections whose ``linear`` is truthy. A
        cover page counted here would shift every position in the book."""
        _pkg, both = self.index_for([paragraphs(30), paragraphs(30)])
        _pkg2, one = self.index_for([paragraphs(30), paragraphs(30)],
                                    linear={1})
        self.assertLess(one.count, both.count)
        self.assertEqual([s.spine_index for s in one.sections], [0])

    def test_a_position_in_an_excluded_document_does_not_read_as_the_start(
            self):
        """A non-linear document has no locations of its own, so the honest
        answer is where the reader last was — reporting 0 would tell the
        server the book went back to page one."""
        _pkg, index = self.index_for(
            [paragraphs(30), paragraphs(30), paragraphs(30)], linear={1})
        self.assertGreater(index.fraction(1, 0), 0.0)

    def test_fraction_never_goes_backwards_as_the_book_is_walked(self):
        """The multi-step property, and the one a single-step test cannot
        see: an index whose per-section bases were wrong still answers
        plausibly for any one position."""
        _pkg, index = self.index_for([paragraphs(25) for _i in range(6)])
        previous = -1.0
        for section in index.sections:
            for offset in range(0, section.chars, 250):
                fraction = index.fraction(section.spine_index, offset)
                self.assertGreaterEqual(
                    fraction, previous,
                    "position went backwards at spine %d offset %d"
                    % (section.spine_index, offset))
                previous = fraction
        self.assertAlmostEqual(previous, 1.0, delta=0.05)

    def test_a_stored_fraction_round_trips(self):
        _pkg, index = self.index_for([paragraphs(40) for _i in range(4)])
        for tenth in range(11):
            fraction = tenth / 10.0
            spine, offset = index.position_of(fraction)
            back = index.fraction(spine, offset)
            self.assertAlmostEqual(back, fraction, delta=1.5 / index.total)

    def test_resume_rounds_forward(self):
        """jellyfin-web's ``cfiFromPercentage`` is ``Math.ceil``: reopening
        at or just past where you stopped costs a sentence, while rounding
        back can leave a finished book unable to reach its end."""
        _pkg, index = self.index_for([paragraphs(40) for _i in range(4)])
        spine, offset = index.position_of(1.0)
        self.assertEqual(spine, index.sections[-1].spine_index)
        self.assertGreater(offset, 0)

    def test_the_index_survives_a_json_round_trip(self):
        _pkg, index = self.index_for([paragraphs(20), paragraphs(20)])
        clone = locations.LocationIndex.from_json(index.to_json())
        self.assertEqual(clone.count, index.count)
        self.assertEqual(clone.position_of(0.5), index.position_of(0.5))

    def test_an_index_built_with_a_different_break_is_refused(self):
        _pkg, index = self.index_for([paragraphs(20)])
        data = index.to_json()
        data["break"] = 150
        with self.assertRaises(ValueError):
            locations.LocationIndex.from_json(data)


class TestTextNodeLengths(unittest.TestCase):
    def test_only_body_text_is_counted(self):
        """epub.js builds its tree walker on ``<body>``; a title in the head
        is not a text node it ever sees."""
        markup = ('<html><head><title>A long title here</title></head>'
                  '<body><p>abc</p></body></html>')
        self.assertEqual(locations.text_node_lengths(markup), [3])

    def test_script_and_style_text_is_counted_even_though_it_is_not_drawn(
            self):
        """It is a text node in the DOM the walker walks. Our number has to
        match theirs, not be tidier than it."""
        markup = ('<html><body><style>p{color:red}</style>'
                  '<p>abc</p></body></html>')
        self.assertEqual(locations.text_node_lengths(markup),
                         [len("p{color:red}"), 3])

    def test_whitespace_between_tags_is_dropped(self):
        markup = "<html><body>\n  <p>abc</p>\n  <p>de</p>\n</body></html>"
        self.assertEqual(locations.text_node_lengths(markup), [3, 2])


if __name__ == "__main__":
    unittest.main()
