"""A small epub reader: parse, paginate, draw, and report where you are.

Why the reader has to do all of this locally, and the argument behind each
layer's shape — see ``docs/readers.md`` §4.

The layers, bottom up. Each knows only about the ones below it, and none
of them imports the UI:

``xmlish``      one tolerant markup reader for every file in the book, on
                ``html.parser`` rather than expat — entity expansion and
                malformed markup, both answered in one place.

``archive``     the zip, the package document, the spine, the TOC. Every
                read is bounded.
``css``         the eight declarations that decide whether a line is a
                chapter title.
``content``     XHTML -> blocks and styled runs, carrying the character
                offsets progress is measured in.
``locations``   epub.js's locations index, so our stored position means
                what every other Jellyfin client's does.
``fonts``       real faces for bold/italic/mono, with script fallback.
``layout``      blocks -> pages. Line breaking, justification, images.
``paint``       a page -> one Pillow bitmap.
``book``        an open book that knows where it is.

Optional-dependency rule (CONTRIBUTING.md): Pillow, which the browser
already requires, and nothing else outside the standard library.
"""


from .archive import EpubError, TooLarge, open_epub
from .book import EpubDocument
from .layout import ReaderStyle
from .paint import PALETTES, palette

__all__ = ["EpubDocument", "EpubError", "ReaderStyle", "TooLarge",
           "PALETTES", "open_epub", "palette"]
