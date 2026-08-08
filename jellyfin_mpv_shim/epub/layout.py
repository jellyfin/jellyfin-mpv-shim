"""Blocks in, pages out. The whole layout engine, and none of the drawing.

This is where the reader's typography lives: line breaking, justification,
indents, list markers, image fitting and the page break itself. It has no
Pillow drawing in it and no toolkit imports — it measures text through
:class:`Measurer` and emits geometry, which is what makes it testable
without a window and what keeps it out of ``renderer.lua``.

**Pagination is per spine document, and a document always starts a page.**
That is what every reader does (it is also what a ``page-break-before`` on
a chapter would force anyway), and it is what makes opening a book cheap:
only the current document is parsed, measured and paginated, so a 1.2 MB
book opens in the time its first chapter takes. The cost is that "page 4 of
312" cannot be answered without paginating everything — which is why the
reader shows progress as a fraction of the book (from ``locations.py``, a
much cheaper index) and a page number within the chapter.

**The unit of position is a character offset, not a page.** Every line
carries the offset it starts at, so a resize — which invalidates every page
boundary in the book — can be answered by re-paginating and finding the
page containing the offset the reader was on. Pages are derived; the offset
is the state.
"""

import logging

from .content import HEADING, IMAGE, INDENT_EM, PARA, RULE

log = logging.getLogger("epub.layout")

#: Space between paragraphs when the book's CSS says nothing at all, in ems.
DEFAULT_PARA_SPACE = 0.55

#: Space above a heading, in ems, when the CSS says nothing.
DEFAULT_HEADING_SPACE = 1.1

#: How far a justified line may stretch before it is left ragged instead,
#: as a multiple of the natural space width. Generous on purpose: a ragged
#: line in the middle of a justified paragraph reads as a rendering fault,
#: while a loose one reads as a book without hyphenation — which is what
#: this is. Only a line that is mostly space (one long word on a line of
#: its own) trips it.
MAX_JUSTIFY_STRETCH = 5.0

#: Characters after which a line may break even with no space — CJK, and the
#: punctuation that must not start a line is handled by never breaking
#: *before* it.
_CJK_RANGES = (
    (0x1100, 0x11FF), (0x2E80, 0x303E), (0x3041, 0x33FF),
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xA000, 0xA4CF),
    (0xAC00, 0xD7A3), (0xF900, 0xFAFF), (0xFF00, 0xFF60),
)

#: Never the first character on a line.
_NO_LINE_START = "、。，．・：；？！）」』】〉》”’%,.;:!?)]}"


def _is_cjk(char):
    code = ord(char)
    return any(lo <= code <= hi for lo, hi in _CJK_RANGES)


class ReaderStyle:
    """The knobs that change how a book is set. All in physical pixels."""

    def __init__(self, font_px=21, font_kind="serif", line_spacing=1.5,
                 margin_x=64, margin_y=48, max_width=760, justify=True,
                 paragraph_indent=True):
        self.font_px = font_px
        self.font_kind = font_kind
        self.line_spacing = line_spacing
        self.margin_x = margin_x
        self.margin_y = margin_y
        #: Cap on the text column. A line of prose is readable up to about
        #: 75 characters and painful past it, and a maximised window on a
        #: 4K screen is several hundred; every reader caps this.
        self.max_width = max_width
        self.justify = justify
        #: Whether a paragraph with no CSS opinion gets a first-line indent
        #: (the book convention) or a blank line (the web convention). A
        #: book that states either in its CSS keeps what it stated.
        self.paragraph_indent = paragraph_indent

    def key(self):
        return (self.font_px, self.font_kind, round(self.line_spacing, 3),
                self.margin_x, self.margin_y, self.max_width, self.justify,
                self.paragraph_indent)


class Measurer:
    """Fonts and string widths for one :class:`ReaderStyle`.

    Widths are cached per (style, string). A chapter is tens of thousands
    of words drawn from a few thousand distinct ones, and re-measuring
    through FreeType for each occurrence is most of the time pagination
    takes.
    """

    def __init__(self, style, script="latin"):
        self.style = style
        self.script = script
        self._widths = {}
        self._fonts = {}

    def size_for(self, span_style):
        return max(8, int(round(self.style.font_px * span_style.scale)))

    def font(self, span_style):
        key = (span_style.key(), self.script)
        hit = self._fonts.get(key)
        if hit is None:
            from . import fonts

            kind = "mono" if span_style.mono else self.style.font_kind
            hit = fonts.face(kind, self.size_for(span_style),
                             span_style.bold, span_style.italic, self.script)
            self._fonts[key] = hit
        return hit

    def width(self, text, span_style):
        if not text:
            return 0.0
        key = (text, span_style.key())
        hit = self._widths.get(key)
        if hit is None:
            font = self.font(span_style)
            try:
                hit = float(font.getlength(text))
            except (AttributeError, OSError, ValueError):
                # Pillow's bitmap default, or a face that cannot measure a
                # string it has no glyphs for. Half an em per character is
                # crude and is only ever reached on a system with no usable
                # fonts at all, where the page is going to look wrong
                # regardless — but it keeps the line breaker terminating.
                hit = len(text) * self.size_for(span_style) * 0.5
            if len(self._widths) > 80000:
                # Bounded rather than LRU: the working set is one chapter,
                # and a clear is cheaper than tracking recency for a cache
                # that is refilled in milliseconds.
                self._widths.clear()
            self._widths[key] = hit
        return hit

    def line_height(self, span_style):
        from . import fonts

        ascent, descent = fonts.metrics(self.font(span_style))
        return int(round((ascent + descent) * self.style.line_spacing))

    def ascent(self, span_style):
        from . import fonts

        return fonts.metrics(self.font(span_style))[0]


class Piece:
    """A run of text placed on a line, ready to draw."""

    __slots__ = ("x", "text", "style")

    def __init__(self, x, text, style):
        self.x = x
        self.text = text
        self.style = style


class Line:
    __slots__ = ("y", "height", "ascent", "pieces", "char_offset")

    def __init__(self, y, height, ascent, pieces, char_offset):
        self.y = y
        self.height = height
        #: Distance from the line's top to its baseline. Pieces of mixed
        #: sizes on one line share it, which is what puts a superscript on
        #: the same baseline as the text around it.
        self.ascent = ascent
        self.pieces = pieces
        self.char_offset = char_offset

    def text(self):
        return "".join(p.text for p in self.pieces)


class ImageItem:
    __slots__ = ("x", "y", "w", "h", "src", "alt", "char_offset")

    def __init__(self, x, y, w, h, src, alt="", char_offset=0):
        self.x = x
        self.y = y
        self.w = w
        self.h = h
        self.src = src
        self.alt = alt
        self.char_offset = char_offset


class RuleItem:
    __slots__ = ("x", "y", "w", "char_offset")

    def __init__(self, x, y, w, char_offset=0):
        self.x = x
        self.y = y
        self.w = w
        self.char_offset = char_offset


class Page:
    """One screenful of a spine document."""

    __slots__ = ("spine_index", "number", "items", "start_offset",
                 "end_offset")

    def __init__(self, spine_index, number, items, start_offset, end_offset):
        self.spine_index = spine_index
        #: Zero-based page number *within this document*.
        self.number = number
        self.items = items
        #: Counted-character offset of the first thing on the page. This is
        #: the position the reader reports, matching jellyfin-web, which
        #: reports ``locations.start.cfi`` — the start of the visible page.
        self.start_offset = start_offset
        self.end_offset = end_offset

    def __repr__(self):
        return "<Page %d.%d %d items @%d>" % (self.spine_index, self.number,
                                              len(self.items),
                                              self.start_offset)


class _Flow:
    """Fills pages with items, and knows when to start a new one."""

    def __init__(self, spine_index, width, height):
        self.spine_index = spine_index
        self.width = width
        self.height = height
        self.pages = []
        self._items = []
        self._y = 0
        self._start = 0
        self._end = 0
        self._pending_space = 0

    def space(self, amount):
        """Vertical space before the next item, collapsed CSS-style.

        Collapsing rather than summing is what stops a paragraph's bottom
        margin and the next one's top margin adding up to a blank line the
        author did not ask for — the same rule the web applies, and books
        are styled against the web.
        """
        if self._items:
            self._pending_space = max(self._pending_space, amount)

    def _take(self, height):
        """Reserve ``height`` px, breaking the page first if it will not
        fit. Returns the y to place at."""
        gap = self._pending_space if self._items else 0
        if self._items and self._y + gap + height > self.height:
            self.flush()
            gap = 0
        self._pending_space = 0
        y = self._y + gap
        self._y = y + height
        return y

    def add_line(self, line):
        line.y = self._take(line.height)
        self._place(line, line.char_offset)

    def add_item(self, item, height):
        item.y = self._take(height)
        self._place(item, item.char_offset)

    def _place(self, item, char_offset):
        if not self._items:
            self._start = char_offset
        self._items.append(item)
        self._end = max(self._end, char_offset)

    def break_page(self):
        """Force a page break (``page-break-before``)."""
        if self._items:
            self.flush()

    def flush(self):
        if not self._items:
            return
        self.pages.append(Page(self.spine_index, len(self.pages),
                               self._items, self._start, self._end))
        self._items = []
        self._y = 0
        self._pending_space = 0

    def finish(self):
        self.flush()
        if not self.pages:
            # A document with nothing drawable still needs a page, or the
            # reader has nowhere to be while it is on this spine item and
            # paging forward walks off the end of an empty list.
            self.pages.append(Page(self.spine_index, 0, [], 0, 0))
        return self.pages


def paginate(blocks, width, height, measurer, image_size=None,
             spine_index=0):
    """Lay ``blocks`` out into pages of ``width`` x ``height`` pixels.

    ``image_size(src)`` returns an image's ``(w, h)`` in pixels, or None if
    it cannot be read — a picture whose size is unknown is skipped rather
    than guessed at, because a guess that is wrong pushes every page
    boundary after it.
    """
    style = measurer.style
    em = style.font_px
    flow = _Flow(spine_index, width, height)
    previous_after = 0.0
    for block in blocks:
        if block.page_break:
            flow.break_page()
        indent = int(block.indent * INDENT_EM * em)
        avail = max(em, width - indent)
        before, after = _block_space(block, style)
        flow.space(int(max(before, previous_after) * em))
        previous_after = after
        if block.kind == RULE:
            flow.add_item(RuleItem(indent + avail // 8, 0,
                                   avail - avail // 4,
                                   block.char_offset),
                          max(2, em // 6) * 2)
            continue
        if block.kind == IMAGE:
            _place_image(flow, block, indent, avail, height, image_size)
            continue
        for line in _wrap(block, avail, measurer, indent):
            flow.add_line(line)
    return flow.finish()


def _block_space(block, style):
    """``(before, after)`` in ems for one block, honouring its CSS."""
    default = DEFAULT_PARA_SPACE
    if block.kind == HEADING:
        default = DEFAULT_HEADING_SPACE
    elif block.marker:
        # List items sit close together; the gap belongs around the list,
        # not between its bullets.
        default = 0.2
    elif style.paragraph_indent:
        # Indented prose is set solid — the indent is what marks the
        # paragraph, and a gap as well is the mistake that makes a novel
        # look like a web page.
        default = 0.0
    before = default if block.space_before is None else block.space_before
    after = default if block.space_after is None else block.space_after
    return before, after


def _place_image(flow, block, indent, avail, page_height, image_size):
    size = image_size(block.src) if image_size else None
    if not size or not size[0] or not size[1]:
        return
    natural_w, natural_h = size
    scale = min(1.0, avail / float(natural_w))
    width = int(natural_w * scale)
    height = int(natural_h * scale)
    if height > page_height:
        # Fit the page rather than spilling: an oversized illustration that
        # cannot be paged onto anything is otherwise simply never seen.
        scale = page_height / float(height)
        width = int(width * scale)
        height = page_height
    x = indent
    if block.align in ("center", ""):
        x = indent + (avail - width) // 2
    elif block.align == "right":
        x = indent + avail - width
    flow.add_item(ImageItem(x, 0, width, height, block.src, block.alt,
                            block.char_offset), height)


# -- line breaking ---------------------------------------------------------


class _Token:
    __slots__ = ("text", "style", "space", "width", "offset", "hard")

    def __init__(self, text, style, space, width, offset, hard=False):
        self.text = text
        self.style = style
        #: Whether this token is whitespace, which is what may be dropped at
        #: a line end and stretched by justification.
        self.space = space
        self.width = width
        self.offset = offset
        #: A hard break (``<br>``), which ends the line wherever it falls.
        self.hard = hard


def _tokenize(block, measurer):
    """Spans -> tokens, one per word, space or CJK character."""
    tokens = []
    for span in block.spans:
        text = span.text
        if not text:
            continue
        offset = span.char_offset
        if block.pre:
            # Preformatted: every line is its own hard-broken run and the
            # spaces are content.
            for i, chunk in enumerate(text.split("\n")):
                if i:
                    tokens.append(_Token("", span.style, False, 0.0, offset,
                                         hard=True))
                if chunk:
                    tokens.append(_Token(chunk, span.style, False,
                                         measurer.width(chunk, span.style),
                                         offset))
            continue
        for piece, is_space, hard in _split_words(text):
            if hard:
                tokens.append(_Token("", span.style, False, 0.0, offset,
                                     hard=True))
                continue
            tokens.append(_Token(piece, span.style, is_space,
                                 measurer.width(piece, span.style), offset))
    return tokens


def _split_words(text):
    """``"a b"`` -> ``[("a", False, False), (" ", True, False), …]``.

    CJK gets one token per character, because there are no spaces to break
    at and a paragraph of Japanese would otherwise be one unbreakable
    token as wide as the chapter.
    """
    out = []
    buffer = []
    for char in text:
        if char == "\n":
            if buffer:
                out.append(("".join(buffer), buffer[0].isspace(), False))
                buffer = []
            out.append(("", False, True))
            continue
        if _is_cjk(char):
            if buffer:
                out.append(("".join(buffer), buffer[0].isspace(), False))
                buffer = []
            out.append((char, False, False))
            continue
        if buffer and buffer[0].isspace() != char.isspace():
            out += _hyphen_split("".join(buffer), buffer[0].isspace())
            buffer = []
        buffer.append(char)
    if buffer:
        out += _hyphen_split("".join(buffer), buffer[0].isspace())
    return out


#: A word may break *after* one of these, keeping it on the upper line.
_BREAK_AFTER = "-–—/"


def _hyphen_split(word, is_space):
    """``"shrink-wrapped"`` -> ``["shrink-", "wrapped"]``.

    Without this, an already-hyphenated compound is one unbreakable token,
    and a greedy breaker that cannot place it leaves the line before it
    short. On a justified page that short line either stretches visibly or
    (worse) gives up and sets ragged in the middle of a paragraph, which
    reads as a rendering fault rather than as typography. Breaking at a
    hyphen the author already wrote is the one hyphenation that needs no
    dictionary and can never be wrong.

    Both halves must be worth having: splitting "e-mail" after one letter
    saves nothing and looks like a mistake.
    """
    if is_space or len(word) < 6 or not any(c in word for c in _BREAK_AFTER):
        return [(word, is_space, False)]
    parts = []
    start = 0
    for i, char in enumerate(word):
        if char in _BREAK_AFTER and i - start >= 2 and len(word) - i - 1 >= 3:
            parts.append((word[start:i + 1], False, False))
            start = i + 1
    if not parts:
        return [(word, is_space, False)]
    if start < len(word):
        parts.append((word[start:], False, False))
    return parts


def _wrap(block, avail, measurer, indent):
    """Break one text block into placed :class:`Line`\\ s (y unset)."""
    tokens = _tokenize(block, measurer)
    if not tokens:
        return []
    style = measurer.style
    em = style.font_px
    align = block.align or ("justify" if style.justify else "left")
    if block.kind == HEADING and not block.align:
        align = "left"
    first_indent = block.first_indent
    if first_indent is None:
        first_indent = (0.0 if block.kind == HEADING or block.marker
                        or not style.paragraph_indent
                        else (1.2 if block.indent == 0 else 0.0))
    marker_style = block.spans[0].style if block.spans else None

    lines = []
    current = []
    width_used = 0.0
    line_start = int(first_indent * em)
    first = True
    for token in tokens:
        if token.hard:
            lines.append((current, width_used, line_start, True))
            current, width_used, first = [], 0.0, False
            line_start = 0
            continue
        if token.space and not current:
            continue                      # no leading space on a wrapped line
        if (current and width_used + token.width > avail - line_start
                and not token.space):
            if _may_break_before(token, current):
                lines.append((current, width_used, line_start, False))
                current, width_used, first = [], 0.0, False
                line_start = 0
        current.append(token)
        width_used += token.width
    if current:
        lines.append((current, width_used, line_start, True))

    out = []
    for tokens_on_line, used, start_x, last in lines:
        while tokens_on_line and tokens_on_line[-1].space:
            used -= tokens_on_line[-1].width
            tokens_on_line.pop()
        if not tokens_on_line:
            # An empty line still occupies its height: it is a `<br>` on its
            # own, which is how books space out scene changes.
            blank = block.spans[0].style if block.spans else None
            if blank is not None:
                out.append(Line(0, measurer.line_height(blank),
                                measurer.ascent(blank), [],
                                block.char_offset))
            continue
        out.append(_place_line(tokens_on_line, used, start_x + indent, avail,
                               align, last, measurer))
    if out and block.marker and marker_style is not None:
        _add_marker(out[0], block.marker, indent, measurer, marker_style)
    return out


def _may_break_before(token, current):
    """Whether a line may end just before ``token``.

    Two rules, both about CJK: a closing bracket or full stop may not start
    a line, and a line may not be broken so early that it is empty.
    """
    if not current:
        return False
    return not (token.text and token.text[0] in _NO_LINE_START)


def _place_line(tokens, used, start_x, avail, align, last, measurer):
    height = max(measurer.line_height(t.style) for t in tokens)
    ascent = max(measurer.ascent(t.style) for t in tokens)
    slack = max(0.0, avail - start_x - used)
    x = float(start_x)
    stretch = 0.0
    if align == "right":
        x += slack
    elif align == "center":
        x += slack / 2.0
    elif align == "justify" and not last:
        spaces = [t for t in tokens if t.space]
        if spaces:
            per = slack / len(spaces)
            # A line stretched past this is one the breaker should have
            # broken differently; setting it ragged is the lesser evil and
            # is what happens at the end of a paragraph anyway.
            if per <= MAX_JUSTIFY_STRETCH * max(
                    1.0, sum(t.width for t in spaces) / len(spaces)):
                stretch = per
    pieces = []
    for token in tokens:
        if token.space:
            x += token.width + stretch
            continue
        pieces.append(Piece(int(round(x)), token.text, token.style))
        x += token.width
    return Line(0, height, ascent, pieces,
                tokens[0].offset if tokens else 0)


def _add_marker(line, marker, indent, measurer, style):
    """Hang a list marker in the gutter to the left of the first line."""
    width = measurer.width(marker + " ", style)
    x = max(0, indent - int(width))
    line.pieces.insert(0, Piece(x, marker, style))
