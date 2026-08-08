"""What a book's numbers mean.

Every assertion here is a claim about someone else's wire format, so each
one names where the format is defined:

* **Durations** come from ``MediaBrowser.Providers/MediaInfo/ProbeProvider.cs``
  ``FetchAsync(Book)`` — comics and PDFs store ``pageCount * 10000``, epub
  stores ``TimeSpan.TicksPerSecond``, and mobi/azw fall through the switch
  and store nothing.
* **Positions** come from jellyfin-web's players through
  ``playbackmanager.js``'s ``PositionTicks = 10000 * player.currentTime()``,
  where ``comicsPlayer``/``pdfPlayer`` report a zero-based page index and
  ``bookPlayer`` reports ``fraction * 1000``.

Both were verified against the live server as well: pushing page 4 to a
six-page PDF stores 30000 ticks and reads back as page 4 of 6.

The off-by-one is the whole risk. The stored value is an *index* and every
human-facing number is a page, so a version of this that got it wrong would
put the shim exactly one page behind every other client — which looks like
a rounding quirk rather than a bug, and would be believed.
"""

import unittest

from jellyfin_mpv_shim import books


def book(path, ticks=None, position=None, **extra):
    item = {"Type": "Book", "Path": path, **extra}
    if ticks is not None:
        item["RunTimeTicks"] = ticks
    if position is not None:
        item["UserData"] = {"PlaybackPositionTicks": position}
    return item


class FormatTest(unittest.TestCase):

    def test_extension_comes_from_the_path(self):
        # There is nowhere else it could: a Book DTO carries no Container
        # and no MediaSources, measured against a live server under
        # Fields=Container, Fields=MediaSources and Fields=Size alike.
        self.assertEqual(books.book_format(book("/l/A Novel.epub")), "epub")

    def test_extension_is_lowercased(self):
        self.assertEqual(books.book_format(book("/l/SHOUTY.PDF")), "pdf")

    def test_a_path_with_no_extension_is_unknown(self):
        # None rather than a guess. Inventing ".epub" would name the
        # downloaded file wrongly and hand a PDF to the wrong application.
        self.assertIsNone(books.book_format(book("/l/no-extension")))

    def test_a_missing_path_is_unknown(self):
        self.assertIsNone(books.book_format({"Type": "Book"}))

    def test_labels_are_uppercase(self):
        self.assertEqual(books.format_label("epub"), "EPUB")
        self.assertEqual(books.format_label("pdf"), "PDF")


class ModeTest(unittest.TestCase):
    """Which encoding applies is decided by FORMAT, never by the value.

    A book nobody has opened stores 0 under all three, and a mobi with no
    runtime is not "at 0%" — it has no notion of progress to be at.
    """

    def test_comics_and_pdfs_are_paged(self):
        for ext in ("cbz", "cbr", "cb7", "cbt", "pdf"):
            self.assertEqual(books.progress_mode(book("/l/x." + ext)),
                             books.PROGRESS_PAGES, ext)

    def test_epub_is_a_percentage(self):
        self.assertEqual(books.progress_mode(book("/l/x.epub")),
                         books.PROGRESS_PERCENT)

    def test_kindle_formats_have_no_progress_at_all(self):
        # BookResolver accepts these, so they appear in a library; the probe
        # switch has no case for them, so they carry no RunTimeTicks and
        # there is no unit to express a position in.
        for ext in ("mobi", "azw", "azw3"):
            self.assertEqual(books.progress_mode(book("/l/x." + ext)),
                             books.PROGRESS_NONE, ext)


class PageCountTest(unittest.TestCase):

    def test_pages_are_the_runtime_over_ten_thousand(self):
        self.assertEqual(books.page_count(book("/l/x.pdf", ticks=60000)), 6)

    def test_a_paged_book_the_server_could_not_count_has_no_total(self):
        # Page counts need the PDFtoImage probe that landed in Jellyfin
        # 12.0; a 10.11 server returns no runtime for PDFs or comics at all.
        # The caller's question is "is there a denominator", and there is not.
        self.assertIsNone(books.page_count(book("/l/x.pdf")))
        self.assertIsNone(books.page_count(book("/l/x.pdf", ticks=0)))

    def test_an_epub_has_no_page_count(self):
        # Its runtime is one second, which is not 1000 pages.
        self.assertIsNone(books.page_count(
            book("/l/x.epub", ticks=books.EPUB_FULL_TICKS)))


class ProgressTest(unittest.TestCase):

    def test_an_unopened_paged_book_is_on_page_one(self):
        # The stored index is 0 and nobody says "page zero".
        mode, value, total = books.progress_of(
            book("/l/x.cbz", ticks=400000, position=0))
        self.assertEqual((mode, value, total),
                         (books.PROGRESS_PAGES, 1, 40))

    def test_the_stored_index_is_one_less_than_the_page(self):
        _mode, value, _total = books.progress_of(
            book("/l/x.pdf", ticks=60000, position=3 * 10000))
        self.assertEqual(value, 4)

    def test_a_page_past_the_end_is_clamped_to_the_last(self):
        # A stale position survives a re-scan that found fewer pages, and
        # "page 900 of 6" is a worse answer than the last page.
        _mode, value, _total = books.progress_of(
            book("/l/x.pdf", ticks=60000, position=899 * 10000))
        self.assertEqual(value, 6)

    def test_a_paged_book_with_no_total_still_reports_its_page(self):
        _mode, value, total = books.progress_of(
            book("/l/x.pdf", position=11 * 10000))
        self.assertEqual((value, total), (12, None))

    def test_epub_position_is_a_percentage_of_one_second(self):
        _mode, value, total = books.progress_of(
            book("/l/x.epub", ticks=books.EPUB_FULL_TICKS,
                 position=3_700_000))
        self.assertEqual((value, total), (37, 100))

    def test_a_format_with_no_progress_reports_none(self):
        mode, value, total = books.progress_of(
            book("/l/x.mobi", position=12345))
        self.assertEqual((mode, value, total),
                         (books.PROGRESS_NONE, None, None))


class TicksTest(unittest.TestCase):
    """The push half. These are the numbers that go back to the server, so
    getting them wrong is visible in every other client."""

    def test_page_one_is_zero_ticks(self):
        self.assertEqual(books.ticks_for_page(1), 0)

    def test_pages_round_trip(self):
        item = book("/l/x.pdf", ticks=60000)
        for page in range(1, 7):
            item["UserData"] = {
                "PlaybackPositionTicks": books.ticks_for_page(page)}
            self.assertEqual(books.progress_of(item)[1], page)

    def test_a_page_below_one_cannot_go_negative(self):
        self.assertEqual(books.ticks_for_page(0), 0)
        self.assertEqual(books.ticks_for_page(-5), 0)

    def test_percentages_round_trip(self):
        item = book("/l/x.epub", ticks=books.EPUB_FULL_TICKS)
        for pct in (0, 1, 37, 50, 99, 100):
            item["UserData"] = {
                "PlaybackPositionTicks": books.ticks_for_percent(pct)}
            self.assertEqual(books.progress_of(item)[1], pct)

    def test_percentages_are_clamped_to_the_range(self):
        self.assertEqual(books.ticks_for_percent(-10), 0)
        self.assertEqual(books.ticks_for_percent(400), books.EPUB_FULL_TICKS)

    def test_a_full_epub_is_exactly_one_second_of_ticks(self):
        # The value the server writes as the DURATION of every epub, so a
        # push of 100% has to land on it exactly or the book reads as
        # not-quite-finished forever.
        self.assertEqual(books.ticks_for_percent(100), 10_000_000)


class LabelTest(unittest.TestCase):

    def test_paged_labels_name_both_numbers(self):
        self.assertEqual(
            books.progress_label(book("/l/x.pdf", ticks=60000,
                                      position=3 * 10000)),
            "Page 4 of 6")

    def test_a_paged_book_with_no_total_drops_the_denominator(self):
        self.assertEqual(
            books.progress_label(book("/l/x.pdf", position=3 * 10000)),
            "Page 4")

    def test_percent_labels(self):
        self.assertEqual(
            books.progress_label(book("/l/x.epub",
                                      ticks=books.EPUB_FULL_TICKS,
                                      position=3_700_000)),
            "37% read")

    def test_a_format_with_no_progress_has_no_label(self):
        self.assertIsNone(books.progress_label(book("/l/x.mobi")))


class TypeTest(unittest.TestCase):

    def test_the_two_types_are_told_apart(self):
        # They share a library and nothing else: a Book has no media source
        # at all, an AudioBook is an ordinary Audio item.
        self.assertTrue(books.is_book({"Type": "Book"}))
        self.assertFalse(books.is_book({"Type": "AudioBook"}))
        self.assertTrue(books.is_audiobook({"Type": "AudioBook"}))
        self.assertFalse(books.is_audiobook({"Type": "Book"}))

    def test_neither_predicate_trips_on_nothing(self):
        self.assertFalse(books.is_book(None))
        self.assertFalse(books.is_audiobook({}))

    def test_cba_is_not_a_book_extension(self):
        # It exists only as a MIME mapping on the server; BookResolver does
        # not accept it, so a .cba file is invisible to the library and is
        # not something this client should claim to understand.
        self.assertNotIn("cba", books.BOOK_EXTENSIONS)


if __name__ == "__main__":
    unittest.main()
