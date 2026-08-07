"""What a book *is*, in the two senses Jellyfin means by the word.

`CollectionType.books` holds two unrelated entity types, and almost every
rule below exists because of that split:

* **`AudioBook : Audio`** is an ordinary audio item. Real duration, real
  `MediaSources`, real transcode negotiation, real progress reporting. It
  needs nothing from this module beyond being recognised, and the rest of
  the app already knows how to play it.
* **`Book : BaseItem`** is *not* `IHasMediaSources`. Its DTO carries no
  `MediaSources`, no `Container`, and no size under any `Fields` value —
  measured, not assumed. The only path to its bytes is
  `/Items/{id}/Download`, and the only statement of its format is `Path`,
  which is exactly what jellyfin-web reads (`bookPlayer` gates on
  ``item.Path?.endsWith('epub')``).

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

Both spellings are pinned against the two implementations that define
them: `ProbeProvider.FetchAsync(Book)` writes the durations, and
jellyfin-web's players write the positions through
``PositionTicks = 10000 * player.currentTime()`` — where `comicsPlayer`
and `pdfPlayer` report a page index and `bookPlayer` reports
``fraction * 1000``. The server's own comments call this a placeholder for
"multiple progress types"; treat it as a wire format to interoperate with,
never as a design to build on.

**An epub's number is not a percentage anyone can name, which is why it
cannot be set here.** It is ``location / total`` over epub.js's *locations
index* — the book's text cut into ~1024-character runs, counted per spine
section with a partial tail at each boundary. Two consequences, and both
are fatal to a "type where you are" control:

* the denominator is a property of how the book was *typeset into sections*,
  not of its length, so the same fraction means different amounts of reading
  in different books, and it is not ``chars_read / chars_total`` either;
* there is nothing on screen in any reader that shows it. A page number is
  something a PDF viewer puts in front of you. A location index is an
  implementation detail of one JavaScript library, so the user has no
  number to type and no way to check the one we would show them.

So progress here is **readable and not writable** for epub: the stored
fraction is still shown, because it is the same figure every other client
shows and it does say roughly how far in you are, but there is no honest
way to offer a control that sets it. See ``progress_settable``.

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

    Read from `Path`, because a `Book` DTO states its format nowhere else —
    there is no `Container`, and `MediaSources` is empty. `Path` is served
    to ordinary users under `Fields=Path` (verified against a non-admin
    account), which is what makes this viable rather than an admin-only
    trick.

    Returns None rather than guessing for a path we cannot read: every
    caller has a sane "unknown format" branch, and inventing ``.epub`` would
    put the wrong extension on a downloaded file and hand the wrong app a
    PDF.
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

    ``None`` covers two different things on purpose — a format that has no
    pages, and a paged format the server could not count (page counts need
    the `PDFtoImage` probe that landed in Jellyfin 12.0, so a 10.11 server
    returns no runtime for PDFs or comics at all). Both mean the same thing
    to a caller: there is no denominator to show.
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


def progress_settable(item):
    """Whether the user can meaningfully *state* where they are in this book.

    Reading is the only case in the app where the app cannot observe its own
    progress: the file is opened in some other application, which reports to
    nobody. So the position has to come from the user — and that only works
    where the unit is one they can see.

    A page is: a PDF viewer and a comic reader both put the page number in
    front of you, and the stored value is exactly that page. An epub's is
    not — see the module docstring — so a box asking for a percentage would
    be asking for a number nobody has, against a scale that is not the one
    they think it is. Answering "no" here is what leaves that control out
    rather than shipping a plausible-looking way to record the wrong place.

    Not the same question as :func:`progress_mode`. The stored fraction is
    still *read* for an epub and still shown; only setting it is refused.
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

    **Nothing in the UI calls this**, and that is deliberate: the value it
    produces is a position in epub.js's locations index, which no reader
    shows the user a number for (see :func:`progress_settable`). It stays
    because it is the inverse of the read path and documents the encoding
    in executable form — if the server ever gains a real progress type, or
    a local epub indexer is ever written, this is the half that already
    agrees with :func:`progress_of`.
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
