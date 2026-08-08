"""An open book: where the reader is, and what that page looks like.

This is the object the UI holds. It owns the archive, the parsed sections,
the locations index and the current position, and it is the only place in
the package that keeps state across a call.

**Position is `(spine_index, char_offset)`, and everything else is
derived.** The page number is derived (re-paginating after a resize
produces different ones), the fraction reported to the server is derived
(via ``locations.py``), the chapter title is derived. That is the shape
that makes a resize a non-event: re-measure, re-paginate, find the page
containing the offset, carry on. A reader that stored "page 74" would have
to answer what page 74 means at a different window size, and there is no
answer.

**Everything expensive is cached by content key, and the keys are exact.**
A parsed section keys on the spine index; its pagination keys additionally
on the layout (font, size, spacing, column width), so nothing survives a
change that would move a line. The caches are small and bounded — a reader
moves through a book one section at a time and never wants the whole of it
resident.

Thread safety is one lock around the mutable state, because the UI calls
into this from the loop thread (what page am I on) and from a worker (open
the book, build the index, paginate the next section) and a `ZipFile` has
one shared file handle underneath it.
"""

import logging
import threading

from . import archive, content, layout, locations, paint

log = logging.getLogger("epub.book")

#: Parsed sections to keep. Three is enough for "the one being read, the one
#: before it and the one after" — the working set of any page turn.
SECTION_CACHE = 3

#: Decoded illustrations to keep. Bounded by count rather than bytes; a
#: page holds a handful and the LANCZOS-resized copies are what get drawn.
IMAGE_CACHE = 24


class Chapter:
    """A TOC entry the UI can put in a menu."""

    __slots__ = ("title", "level", "spine_index", "anchor")

    def __init__(self, title, level, spine_index, anchor=""):
        self.title = title
        self.level = level
        self.spine_index = spine_index
        self.anchor = anchor


class EpubDocument:
    """One open epub, positioned somewhere in itself."""

    def __init__(self, path, style=None, script="latin"):
        self.path = path
        self.package = archive.open_epub(path)
        self.style = style or layout.ReaderStyle()
        self.measurer = layout.Measurer(self.style, script)
        self._lock = threading.RLock()
        self._sections = {}
        self._paginated = {}
        self._images = {}
        self._image_sizes = {}
        self._css_cache = {}
        self._index = None
        self._viewport = (600, 800)
        #: Current position. The page number is a cache of "which page of
        #: this section contains ``offset``", refreshed on every move.
        self._spine = self._first_readable_spine()
        self._offset = 0
        self._page = 0

    # -- identity ---------------------------------------------------------

    @property
    def title(self):
        return self.package.title

    @property
    def author(self):
        return self.package.author

    @property
    def spine_count(self):
        return len(self.package.spine)

    def chapters(self):
        return [Chapter(entry.title, entry.level, entry.spine_index)
                for entry in self.package.toc
                if entry.spine_index is not None]

    def chapter_title(self, spine_index=None):
        """The TOC title covering a spine document — the last entry at or
        before it, which is what a running header shows."""
        if spine_index is None:
            spine_index = self._spine
        title = ""
        for entry in self.package.toc:
            if entry.spine_index is None:
                continue
            if entry.spine_index <= spine_index:
                if entry.level == 0 or not title:
                    title = entry.title
            else:
                break
        return title

    def close(self):
        """Drop the parsed book, keeping the object usable.

        There is no handle to release — the archive holds none (see
        ``archive.EpubArchive``) — so this is only about memory: the parsed
        sections, their pagination and the decoded images, which for an
        illustrated book is the largest thing the reader owns. Everything
        it drops is rebuilt on demand, so calling it early costs a re-parse
        and nothing else. That is what makes it safe for the shell to call
        without knowing whether the page is coming back.
        """
        with self._lock:
            self._sections.clear()
            self._paginated.clear()
            self._images.clear()

    def _first_readable_spine(self):
        return 0 if self.package.spine else 0

    # -- layout -----------------------------------------------------------

    def set_viewport(self, width, height):
        """Set the text column's pixel size. Returns True if it changed.

        The caller passes the *column*, not the window: margins and the
        max-width cap are the shell's business because it is the thing that
        knows what else is on screen.
        """
        with self._lock:
            size = (max(80, int(width)), max(80, int(height)))
            if size == self._viewport:
                return False
            self._viewport = size
            self._paginated.clear()
            self._resync_page()
            return True

    def set_style(self, style):
        """Swap the typography. Invalidates every pagination."""
        with self._lock:
            self.style = style
            self.measurer = layout.Measurer(style, self.measurer.script)
            self._paginated.clear()
            self._resync_page()

    def _layout_key(self):
        return (self.style.key(), self._viewport)

    # -- sections ---------------------------------------------------------

    def _section(self, spine_index):
        """Parsed blocks for a spine document, cached."""
        hit = self._sections.get(spine_index)
        if hit is not None:
            return hit
        try:
            blocks, chars = content.parse_spine_item(
                self.package, spine_index, self._css_cache)
        except archive.EpubError:
            log.info("spine %d unreadable", spine_index, exc_info=True)
            blocks, chars = [], 0
        self._sections[spine_index] = (blocks, chars)
        self._trim(self._sections, SECTION_CACHE, spine_index)
        return blocks, chars

    def pages(self, spine_index):
        """Laid-out pages of a spine document, cached per layout."""
        with self._lock:
            key = (spine_index, self._layout_key())
            hit = self._paginated.get(key)
            if hit is not None:
                return hit
            blocks, _chars = self._section(spine_index)
            width, height = self._viewport
            pages = layout.paginate(blocks, width, height, self.measurer,
                                    self._image_size, spine_index)
            self._paginated[key] = pages
            self._trim(self._paginated, SECTION_CACHE * 2, key)
            return pages

    @staticmethod
    def _trim(cache, limit, keep):
        """Drop oldest entries. Insertion-ordered dicts make this an LRU
        good enough for a cache whose working set is three."""
        while len(cache) > limit:
            for key in cache:
                if key != keep:
                    del cache[key]
                    break
            else:
                return

    # -- images -----------------------------------------------------------

    def _image_bytes(self, src):
        return self.package.archive.read(src, archive.MAX_IMAGE_BYTES)

    def _image_size(self, src):
        if src in self._image_sizes:
            return self._image_sizes[src]
        size = None
        try:
            size = paint.image_size(self._image_bytes(src))
        except archive.EpubError:
            log.debug("image %s unreadable", src, exc_info=True)
        self._image_sizes[src] = size
        return size

    def _load_image(self, src):
        if src in self._images:
            return self._images[src]
        picture = None
        try:
            picture = paint.decode_image(self._image_bytes(src))
        except archive.EpubError:
            log.debug("image %s unreadable", src, exc_info=True)
        self._images[src] = picture
        self._trim(self._images, IMAGE_CACHE, src)
        return picture

    # -- the locations index ----------------------------------------------

    def index(self):
        return self._index

    def build_index(self, progress=None):
        """Build the locations index. Slow-ish; call it off the UI thread.

        Until this has run, :meth:`fraction` answers None and the reader
        shows a page position without a book position — which is the right
        degradation, because the alternative is either blocking the open or
        reporting a number that is wrong.
        """
        index = locations.build(self.package, progress)
        with self._lock:
            self._index = index
        return index

    def set_index(self, index):
        with self._lock:
            self._index = index

    # -- position ---------------------------------------------------------

    @property
    def spine_index(self):
        return self._spine

    @property
    def char_offset(self):
        return self._offset

    @property
    def page_number(self):
        return self._page

    def page_count(self):
        """Pages in the current section. Not in the book — see the module
        docstring in ``layout.py``."""
        return len(self.pages(self._spine))

    def fraction(self):
        """Position as the fraction Jellyfin stores, or None if the index
        has not been built yet."""
        with self._lock:
            if self._index is None:
                return None
            return self._index.fraction(self._spine, self._offset)

    def current_page(self):
        with self._lock:
            pages = self.pages(self._spine)
            self._page = max(0, min(self._page, len(pages) - 1))
            return pages[self._page]

    def _resync_page(self):
        """Find the page holding the current offset after a re-layout.

        **The FIRST page of an equal-offset run, not the last.** Offsets are
        per token now (``layout._tokenize``), but two pages can still start
        at the same one — an image and the line under it share theirs, and a
        page whose first item carries no text of its own inherits the
        previous. Taking the last of such a run walks the reader forward
        every time the page size changes, which is what a resize is; taking
        the first leaves them where they were, and paging on from there
        costs at most the one press they would have made anyway.
        """
        pages = self.pages(self._spine)
        self._page = 0
        for i, page in enumerate(pages):
            if page.start_offset > self._offset:
                break
            if i and page.start_offset == pages[self._page].start_offset:
                continue        # same offset: keep the earlier page
            self._page = i

    def goto(self, spine_index, char_offset=0):
        with self._lock:
            self._spine = max(0, min(int(spine_index),
                                     len(self.package.spine) - 1))
            self._offset = max(0, int(char_offset))
            self._resync_page()

    def goto_fraction(self, fraction):
        """Resume from a stored position. No-op without an index."""
        with self._lock:
            if self._index is None or fraction is None:
                return False
            spine, offset = self._index.position_of(fraction)
            self.goto(spine, offset)
            return True

    def goto_page(self, number):
        with self._lock:
            pages = self.pages(self._spine)
            self._page = max(0, min(int(number), len(pages) - 1))
            self._offset = pages[self._page].start_offset

    def next_page(self):
        """Forward one page, crossing into the next section at the end.
        False when there is nowhere further to go."""
        with self._lock:
            pages = self.pages(self._spine)
            if self._page + 1 < len(pages):
                self.goto_page(self._page + 1)
                return True
            if self._spine + 1 >= len(self.package.spine):
                return False
            self.goto(self._spine + 1, 0)
            return True

    def prev_page(self):
        with self._lock:
            if self._page > 0:
                self.goto_page(self._page - 1)
                return True
            if self._spine == 0:
                return False
            # Landing on the *last* page of the previous section, which is
            # what going back means; goto() would put us on its first.
            self._spine -= 1
            pages = self.pages(self._spine)
            self._page = len(pages) - 1
            self._offset = pages[self._page].start_offset
            return True

    def next_section(self):
        with self._lock:
            if self._spine + 1 >= len(self.package.spine):
                return False
            self.goto(self._spine + 1, 0)
            return True

    def prev_section(self):
        with self._lock:
            if self._spine == 0:
                return False
            self.goto(self._spine - 1, 0)
            return True

    # -- text ---------------------------------------------------------------

    def page_text(self):
        """The paragraphs on the visible page, as text.

        **The paragraphs on it, each whole** — not the exact characters
        drawn. A paragraph the page ends in the middle of is copied
        entire, which is the same answer :meth:`paragraph_at` gives and
        for the same reason: half a paragraph is a fragment starting
        mid-sentence, and nobody pastes one on purpose.

        It also happens to be the only version that can be *right*.
        Rebuilding the visible text from the laid-out lines cannot put the
        spaces back: a space is not a run — the breaker drops space tokens
        and justification turns them into a gap between two pieces' x — so
        the lines yield their words joined together. Going back to the
        blocks asks the layer that still has the author's text.
        """
        with self._lock:
            return "\n\n".join(
                part for part in (_block_text(b)
                                  for b in self._blocks_on_page())
                if part.strip())

    def _blocks_on_page(self):
        """The distinct blocks the visible page draws, in order."""
        out = []
        for item in self.current_page().items:
            if type(item).__name__ != "Line" or item.block is None:
                continue
            if not out or out[-1] is not item.block:
                out.append(item.block)
        return out

    def paragraph_at(self, y):
        """The whole paragraph at a height on the page, as text, or None.

        ``y`` is in physical pixels from the top of the **text column** —
        the same space :meth:`render` lays out in, so a caller converts
        from the window by subtracting the bitmap's origin and the column's
        own top. There is no text selection in this reader (the page is one
        bitmap), so a pointer can ask for a paragraph and nothing finer;
        this is the whole of what it can ask.

        A height, not a point: every line spans the whole measure, so the
        horizontal position says nothing a paragraph could be picked by.
        Taking an x as well would be a parameter that is quietly ignored,
        and the first caller to compute it wrongly would never find out.

        The paragraph, not the line: a line is an artefact of this window
        at this type size, and pasting one would produce a fragment that
        means nothing on its own.
        """
        with self._lock:
            block = self._block_at(y)
            return _block_text(block) if block is not None else None

    def _block_at(self, y):
        page = self.current_page()
        for item in page.items:
            if type(item).__name__ != "Line" or item.block is None:
                continue
            if item.y <= y < item.y + item.height:
                return item.block
        # Nothing directly at that height — a point in the gutter between
        # paragraphs, or below the last line. Fall back to the nearest line
        # above, which is the paragraph the eye is on.
        best = None
        for item in page.items:
            if type(item).__name__ != "Line" or item.block is None:
                continue
            if item.y <= y:
                best = item.block
        return best

    # -- drawing ----------------------------------------------------------

    def render(self, size, colors, origin=None):
        """Draw the current page into a PIL image of ``size``."""
        return self.render_keyed(size, colors, origin)[1]

    def render_keyed(self, size, colors, origin=None):
        """``(page_key, image)`` — the page drawn, and what it IS.

        The key has to come from the same locked read as the pixels. A
        caller that computes the key first and renders afterwards is naming
        the page it *asked* for, not the one it got: rendering happens on a
        worker, the reader turns pages on the loop thread, and a turn
        between the two stores one page's pixels under another page's name.
        In a cache that keeps the first entry for a key, that is permanent —
        the correct image is freed and the wrong one is served for the life
        of the entry.
        """
        with self._lock:
            page = self.current_page()
            key = self.page_key()
            image = paint.render_page(page, size, self.style, self.measurer,
                                      colors, self._load_image, origin)
            return key, image

    def page_key(self):
        """A content key for the current page's bitmap.

        Everything that changes what is drawn is in it: which page, of which
        section, at which layout. The colours are the caller's to add,
        because the caller is the thing that knows the theme.
        """
        with self._lock:
            return (self.path, self._spine, self._page, self._layout_key())



def _block_text(block):
    """A whole paragraph, including the part of it on the next page.

    From the block rather than from the page's lines: a paragraph broken
    across a page break is still one paragraph, and copying the visible
    half of it would silently truncate a quotation.
    """
    text = (block.plain_text() if hasattr(block, "plain_text")
            else block.text())
    if getattr(block, "pre", False):
        return text
    text = " ".join(text.split())
    marker = getattr(block, "marker", "")
    return ("%s %s" % (marker, text)).strip() if marker else text
