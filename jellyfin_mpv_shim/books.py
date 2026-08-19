"""What a book *is*, in the two senses Jellyfin means by the word.

`CollectionType.books` holds two unrelated entity types, and almost every
rule below exists because of that split:

* **`AudioBook : Audio`** is an ordinary audio item — real duration, real
  `MediaSources`, real progress reporting. It needs nothing from this
  module beyond being recognised.
* **`Book : BaseItem`** is *not* `IHasMediaSources`: no `MediaSources`, no
  `Container`, no size under any `Fields` value, `/Items/{id}/Download` as
  the only path to its bytes, and `Path` as the only statement of its
  format. See ``docs/readers.md`` §1.

The awkward half is progress. `RunTimeTicks` is overloaded as a fake unit
and the encoding depends on the *format*, so a single number means three
different things:

===========  ==============================  =========================
format       `RunTimeTicks`                  `PlaybackPositionTicks`
===========  ==============================  =========================
comic / pdf  ``page_count * 10000``          ``page_index * 10000``
epub         ``10_000_000`` (one second)     ``location / locations``
mobi / azw   absent                          meaningless
===========  ==============================  =========================

Treat that table as a wire format to interoperate with, never as a design
to build on. Which server and web-client code each spelling is pinned
against, why an epub's fraction cannot be asked of the user (which is what
``progress_settable`` answers, about the *manual* control only), and why the
built-in reader is not subject to that rule: ``docs/readers.md`` §2 and §2.1.

Kept out of the browser package because the download manager needs the same
answers and must not import a UI module.
"""

import os

from .i18n import _

#: The two item types a books library holds.
BOOK_TYPE = "Book"
AUDIOBOOK_TYPE = "AudioBook"

#: Extensions `BookResolver` accepts. `.cba` is deliberately absent: it
#: exists only as a MIME mapping on the server and is invisible to the
#: library, so a file with that extension is not a Book at all.
BOOK_EXTENSIONS = (
    "azw", "azw3", "cb7", "cbr", "cbt", "cbz", "epub", "mobi", "pdf",
)

#: Formats whose progress is a page index. The server counts archive
#: entries for the comic ones and renders the PDF to count its pages.
PAGED_FORMATS = frozenset({"cb7", "cbr", "cbt", "cbz", "pdf"})

#: The one format whose progress is a proportion rather than a count.
PERCENT_FORMATS = frozenset({"epub"})

#: Ticks per page, for the paged formats. The server multiplies a page
#: count by this and jellyfin-web divides a position by it.
TICKS_PER_PAGE = 10000

#: What an epub's ``RunTimeTicks`` always is: one second, against which the
#: stored position is read as a proportion.
EPUB_FULL_TICKS = 10_000_000

#: Progress modes :func:`progress_of` answers with.
PROGRESS_PAGES = "pages"
PROGRESS_PERCENT = "percent"
PROGRESS_NONE = "none"


def is_book(item):
    """A `Book` — the download-and-open kind, with no media source."""
    return (item or {}).get("Type") == BOOK_TYPE


def is_audiobook(item):
    """An `AudioBook` — an ordinary audio item that happens to be a book."""
    return (item or {}).get("Type") == AUDIOBOOK_TYPE


def book_format(item):
    """Lowercase extension of a book's file (``"epub"``), or ``None``.

    Read from `Path`, because a `Book` DTO states its format nowhere else
    and `Path` *is* served to non-admins (``docs/readers.md`` §1). Returns
    None rather than guessing for a path we cannot read: inventing ``.epub``
    puts the wrong extension on a downloaded file and hands the wrong
    application a PDF.
    """
    path = (item or {}).get("Path") or ""
    ext = os.path.splitext(path)[1].lstrip(".").lower()
    return ext or None


def format_label(fmt):
    """How a format is named to the user. Uppercase for the acronyms,
    because "Epub" and "Pdf" read as typos."""
    if not fmt:
        return _("Unknown format")
    return fmt.upper()


def progress_mode(item):
    """Which of the three progress encodings this item uses.

    Decided by *format*, not by the value: a book that has never been opened
    has a position of 0 under every encoding, and a mobi with no runtime at
    all is not "at 0%" — it has no notion of progress to be at.
    """
    fmt = book_format(item)
    if fmt in PAGED_FORMATS:
        return PROGRESS_PAGES
    if fmt in PERCENT_FORMATS:
        return PROGRESS_PERCENT
    return PROGRESS_NONE


def page_count(item):
    """Pages in a paged book, or ``None``.

    ``None`` covers two different things on purpose — a format with no
    pages, and a paged format the server could not count (page counts need
    the `PDFtoImage` probe that landed in Jellyfin 12.0). Both mean the same
    thing to a caller: there is no denominator to show.
    """
    if progress_mode(item) != PROGRESS_PAGES:
        return None
    ticks = (item or {}).get("RunTimeTicks")
    if not ticks:
        return None
    return int(ticks // TICKS_PER_PAGE) or None


def progress_of(item):
    """This book's stored position, as ``(mode, value, total)``.

    ``value`` and ``total`` are in the mode's own units: 1-based pages for
    ``pages`` (a page index of 0 is page 1 — the stored value is an index,
    but nobody says "I am on page zero"), whole percent for ``percent``.
    Both are ``None`` under ``none``.
    """
    mode = progress_mode(item)
    ticks = ((item or {}).get("UserData") or {}).get(
        "PlaybackPositionTicks") or 0
    if mode == PROGRESS_PAGES:
        total = page_count(item)
        page = int(ticks // TICKS_PER_PAGE) + 1
        if total:
            page = max(1, min(page, total))
        return mode, page, total
    if mode == PROGRESS_PERCENT:
        pct = int(round(ticks * 100.0 / EPUB_FULL_TICKS))
        return mode, max(0, min(pct, 100)), 100
    return mode, None, None


def fraction_of(item):
    """An epub's stored position as a raw fraction in [0, 1], or ``None``.

    :func:`progress_of` rounds to whole percent, which is the right unit to
    *show* and the wrong one to resume from: one percent of a novel is a
    dozen locations, i.e. several pages. The reader wants the number the
    server actually holds.
    """
    if progress_mode(item) != PROGRESS_PERCENT:
        return None
    ticks = ((item or {}).get("UserData") or {}).get(
        "PlaybackPositionTicks") or 0
    return max(0.0, min(ticks / float(EPUB_FULL_TICKS), 1.0))


def ticks_for_fraction(fraction):
    """Ticks for a raw fraction, in the server's epub encoding.

    The inverse of :func:`fraction_of`, and what the built-in reader
    reports: jellyfin-web sends ``10000 * (progress * 1000)``, i.e. the
    fraction times 10^7 with no rounding, so writing whole percent here
    would put us a few pages off from the client that wrote it last.
    """
    value = max(0.0, min(float(fraction), 1.0))
    return int(round(value * EPUB_FULL_TICKS))


#: Formats that open in the window, and the route kind each one opens.
#: The single place that answers "can we draw this ourselves", so the
#: book's own page and the tile menu cannot drift apart about it.
IN_WINDOW_FORMATS = {"epub": "reader", "cbz": "comic", "cbt": "comic"}


def reader_route(item):
    """The route kind that reads this book here, or None for the desktop.

    ``None`` is the answer for pdf, mobi, azw, cbr and cb7, for reasons
    that differ per format (``docs/readers.md`` §1). All of them still Read
    — the button means "open this book", and off the machine is where it
    opens.
    """
    return IN_WINDOW_FORMATS.get(book_format(item))


def progress_settable(item):
    """Whether the user can meaningfully *state* where they are in this book.

    A page is a unit the user can see and the stored value is exactly that
    page; an epub's fraction is neither (``docs/readers.md`` §2.1), so the
    manual Progress… dialog is offered for paged formats alone.

    Not the same question as :func:`progress_mode`: an epub's fraction is
    still *read* and still shown, and this does not gate the built-in
    reader, which reports through :func:`ticks_for_fraction`.
    """
    return progress_mode(item) == PROGRESS_PAGES


def ticks_for_page(page):
    """Ticks for a 1-based page number, in the server's paged encoding.

    The stored value is a zero-based *index*, so page 1 is 0 ticks. Getting
    this off by one would put every book one page behind on every other
    client, which is the kind of wrong that looks like a rounding quirk.
    """
    return max(0, int(page) - 1) * TICKS_PER_PAGE


def ticks_for_percent(percent):
    """Ticks for a whole percentage, in the server's epub encoding.

    The inverse of :func:`progress_of`'s percent mode, in the units that
    mode shows. Anything reporting a real position uses
    :func:`ticks_for_fraction`, which does not round.

    Not dead: the round trip is pinned by tests/test_books.py and the live
    position is set through it by tests/e2e/test_books.py.
    """
    pct = max(0.0, min(float(percent), 100.0))
    return int(round(pct * EPUB_FULL_TICKS / 100.0))


def progress_label(item):
    """One line describing where the reader is, or ``None`` when the format
    has no progress to describe."""
    mode, value, total = progress_of(item)
    if mode == PROGRESS_PAGES:
        if total:
            return _("Page %(page)d of %(total)d") % {"page": value,
                                                      "total": total}
        return _("Page %d") % value
    if mode == PROGRESS_PERCENT:
        return _("%d%% read") % value
    return None
