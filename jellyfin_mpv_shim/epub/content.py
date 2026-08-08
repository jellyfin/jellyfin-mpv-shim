"""XHTML -> the small document model this reader can draw.

The model is deliberately shallow: a spine document becomes a flat list of
:class:`Block`\\ s (paragraph, heading, image, rule), and a text block is a
list of :class:`Span`\\ s (a run of characters with one set of style flags).
There is no box model, no float, no table layout and no CSS cascade.

**That is the scope decision, not a shortcut taken under time pressure.**
Novels and published non-fiction are paragraphs, headings, emphasis, block
quotes, lists and pictures; the layouts that need more than this — poetry
with hanging indents, technical books with side notes, comics-as-epub,
anything typeset in a table — are exactly the books whose authors will be
better served by a real reader, which is the button next to this one. A
renderer that half-implements CSS produces a worse result than one that
declines to, because the failure is silent and looks like the book.

**Character offsets ride along with the spans, and the walk that builds them
is epub.js's, not ours.** Progress is a position in the locations index (see
``locations.py``), which counts *raw text nodes in document order* with one
odd rule: a node whose text is entirely whitespace counts zero, and any
other node counts its full length, whitespace included. That number cannot
be recovered from the normalized text in a `Span` — normalization is
lossy — so it is recorded during this walk, when both are in hand. Every
span knows the count that preceded it, which is what lets a page on screen
name a position the rest of Jellyfin agrees with.

What is dropped, and why it is safe to drop:

* ``<script>``/``<style>`` content is not drawn, but *is* counted, because
  epub.js's tree walker sees those text nodes and our number has to match
  the client that wrote it.
* ``<table>`` is flattened to one paragraph per row. A wrong-looking table
  beats a missing one, and beats a layout engine nobody will maintain.
* Of CSS, only the eight declarations in ``css.USED_PROPERTIES`` are read
  — size, weight, style, family, alignment, decoration, indent and the
  vertical margins. That is not "some CSS support": it is the set that
  decides whether a line is a chapter title or a sentence, and everything
  outside it is decoration this reader would not draw anyway.
"""

import re

#: Block kinds.
PARA = "para"
HEADING = "heading"
IMAGE = "image"
RULE = "rule"

#: Tags that end the current block and start a new one.
_BLOCK_TAGS = frozenset({
    "p", "div", "section", "article", "header", "footer", "aside", "nav",
    "blockquote", "figure", "figcaption", "li", "dd", "dt", "dl", "ul", "ol",
    "table", "tr", "td", "th", "tbody", "thead", "h1", "h2", "h3", "h4",
    "h5", "h6", "pre", "center", "main", "body", "hgroup", "caption",
})

_HEADINGS = {"h1": 1, "h2": 2, "h3": 3, "h4": 4, "h5": 5, "h6": 6}

#: Inline tags -> the style flag they set.
_STYLE_TAGS = {
    "b": "bold", "strong": "bold",
    "i": "italic", "em": "italic", "cite": "italic", "dfn": "italic",
    "u": "underline", "ins": "underline",
    "s": "strike", "strike": "strike", "del": "strike",
    "code": "mono", "tt": "mono", "kbd": "mono", "samp": "mono", "var": "mono",
}

#: Subtrees whose text is counted for progress but never drawn.
_INVISIBLE = frozenset({"script", "style", "head", "title", "meta", "link"})

#: How far one level of ``blockquote``/list nesting indents, in ems of the
#: body text. Ems rather than pixels so it tracks the reader's font size.
INDENT_EM = 1.6

#: How much larger than the body a paragraph's first character has to be
#: before it is read as a drop capital. Two ems is already twice the height
#: of the line it would otherwise sit on; books typically set three or four.
DROPCAP_SCALE = 2.0

#: ``list-style-type`` values this reader can draw, mapped to how a marker
#: is made. Anything else in a stylesheet falls back to the tag's default,
#: which is what a browser does with a keyword it does not know.
_BULLETS = {"disc": "\u2022", "circle": "\u25e6", "square": "\u25aa",
            "none": ""}

#: Bullets a nested ``<ul>`` cycles through, as every browser does. The
#: depth matters in a book: a list of exceptions under a list of rules is
#: unreadable when both levels wear the same dot.
_UL_DEPTH = ("\u2022", "\u25e6", "\u25aa")

#: ``<ol type=...>`` and the equivalent ``list-style-type`` keywords.
_NUMBERING = {
    "1": "decimal", "a": "lower-alpha", "A": "upper-alpha",
    "i": "lower-roman", "I": "upper-roman",
}

_WS = re.compile(r"\s+")


class Style:
    """The style flags a run of text carries. Immutable; ``with_`` copies."""

    __slots__ = ("bold", "italic", "underline", "strike", "mono", "scale",
                 "rise", "smallcaps")

    def __init__(self, bold=False, italic=False, underline=False,
                 strike=False, mono=False, scale=1.0, rise=0.0,
                 smallcaps=False):
        self.bold = bold
        self.italic = italic
        self.underline = underline
        self.strike = strike
        self.mono = mono
        #: Multiplier on the body font size. Headings carry it, and so do
        #: ``<small>``/``<sup>``; it is a float rather than a font size so a
        #: reader-set body size scales the whole document.
        self.scale = scale
        #: Baseline shift in ems of the body size, positive = up. What makes
        #: a footnote marker a superscript rather than a stray digit; the
        #: layer that turns it into pixels is ``layout._place_line``, so
        #: nothing here has to know the font size.
        self.rise = rise
        #: Small capitals. Not a face — Pillow has no small-caps variant and
        #: no way to synthesise one — so it is carried here and *acted on*
        #: in the walk below, which is the only place that still has the
        #: text to split into "already capital" and "made capital".
        self.smallcaps = smallcaps

    def with_(self, **kw):
        out = Style(self.bold, self.italic, self.underline, self.strike,
                    self.mono, self.scale, self.rise, self.smallcaps)
        for key, value in kw.items():
            setattr(out, key, value)
        return out

    def key(self):
        return (self.bold, self.italic, self.underline, self.strike,
                self.mono, round(self.scale, 3), round(self.rise, 3))

    def __eq__(self, other):
        return isinstance(other, Style) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def __repr__(self):
        on = [n for n in ("bold", "italic", "underline", "strike", "mono",
                          "smallcaps") if getattr(self, n)]
        return "<Style %s x%.2f>" % ("+".join(on) or "plain", self.scale)


class Span:
    """A run of text with one style, and where it sits in the count."""

    __slots__ = ("text", "style", "char_offset")

    def __init__(self, text, style, char_offset=0):
        self.text = text
        self.style = style
        #: Counted characters *before* this span, within its spine document.
        #: See the module docstring: this is epub.js's count, not len() of
        #: anything here.
        self.char_offset = char_offset

    def __repr__(self):
        return "<Span %r %r @%d>" % (self.text[:24], self.style,
                                     self.char_offset)


class Block:
    """A paragraph, heading, image or rule."""

    __slots__ = ("kind", "spans", "level", "align", "indent", "indent_right",
                 "marker", "src", "alt", "char_offset", "pre", "anchors",
                 "space_before", "space_after", "first_indent", "page_break",
                 "dropcap", "box")

    def __init__(self, kind, spans=None, level=0, align="", indent=0.0,
                 indent_right=0.0, marker="", src=None, alt="",
                 char_offset=0, pre=False, space_before=None,
                 space_after=None, first_indent=None, page_break=False,
                 box=None):
        self.kind = kind
        self.spans = spans or []
        self.level = level
        #: "", "center", "right", "justify". Empty means the reader's
        #: default, which is not the same as "left" — a reader set to
        #: justify should justify a paragraph that said nothing.
        self.align = align
        #: Inset from the left, in **ems of the body size** — not a nesting
        #: level. Ems because the two things that produce it are measured
        #: that way: one step of ``blockquote``/list nesting is INDENT_EM,
        #: and a stylesheet's ``margin-left`` is whatever it says. A level
        #: count cannot express the second, and a verse set at 2em would
        #: have to be rounded to a level or dropped.
        self.indent = indent
        #: Inset from the right. Almost always 0 — but a block quote is
        #: inset on *both* sides in every book ever printed, and one that
        #: is only moved in from the left reads as a paragraph that lost
        #: its indent rather than as a quotation.
        self.indent_right = indent_right
        #: Whether this paragraph opens with a drop capital. Decided in
        #: :meth:`_Walker._flush` from what the first span turned out to
        #: be; the letter itself stays in the spans, because it is text
        #: the reader is counting.
        self.dropcap = False
        self.marker = marker
        self.src = src
        self.alt = alt
        #: For an IMAGE, the size the AUTHOR asked for — see
        #: :func:`_image_box`. Empty when the book said nothing, which
        #: means "whatever the file happens to be". A browser does that
        #: too, but only in the same case: a book that draws a 256 px arrow
        #: at 1em has said something, and drawing the file's own size there
        #: puts a quarter of a page of arrow in the middle of a recipe.
        self.box = box or {}
        self.char_offset = char_offset
        #: Preformatted: whitespace is significant and lines do not reflow.
        self.pre = pre
        #: ``id``s of elements that begin at this block, for resolving a
        #: TOC link that points into the middle of a document.
        self.anchors = []
        #: Vertical space around the block, in ems of the body size, or
        #: None for "the reader's default". None and 0 are different
        #: answers: a stylesheet that sets ``margin: 0`` and indents the
        #: first line instead is asking for continuous typeset prose, and
        #: overriding it with a default gap undoes the author's design.
        self.space_before = space_before
        self.space_after = space_after
        #: First-line indent in ems (``text-indent``).
        self.first_indent = first_indent
        #: ``page-break-before: always`` — start a fresh page here.
        self.page_break = page_break

    def text(self):
        """What is drawn, run by run — including any synthetic transform."""
        return "".join(s.text for s in self.spans)

    def plain_text(self):
        """What the author wrote, for copying out of the reader.

        Small capitals are the one place the two differ: Pillow has no
        small-caps face, so the walk uppercases what was lowercase and sets
        it smaller. Copied verbatim that comes out SHOUTING, and books use
        small caps for proper nouns and chapter openings — exactly the
        sentences someone quotes. The reduced runs still carry the flag, so
        undoing it is exact rather than a guess.
        """
        return "".join(s.text.lower() if s.style.smallcaps else s.text
                       for s in self.spans)

    def is_empty(self):
        return self.kind in (PARA, HEADING) and not self.text().strip()

    def dropcap_span(self):
        """The drop capital's span, or None. Always ``spans[0]``."""
        return self.spans[0] if self.dropcap and self.spans else None

    def __repr__(self):
        if self.kind == IMAGE:
            return "<Block image %s>" % self.src
        return "<Block %s%s %r>" % (
            self.kind, self.level or "", self.text()[:40])


class _Walker:
    def __init__(self, base_href, resolve, sheet=None):
        self.base_href = base_href
        self.resolve = resolve
        #: A :class:`~.css.Stylesheet`, or None to read only inline styles.
        self.sheet = sheet
        self.blocks = []
        #: epub.js's running count over this document's text nodes.
        self.counted = 0
        self._block = None
        self._pending_anchors = []

    # -- block plumbing ---------------------------------------------------

    def _flush(self):
        block = self._block
        self._block = None
        if block is None:
            return
        if block.kind in (PARA, HEADING):
            # Trim the edges: leading and trailing whitespace in the source
            # is markup indentation, not text the author wrote.
            while block.spans and not block.spans[0].text.strip():
                block.spans.pop(0)
            while block.spans and not block.spans[-1].text.strip():
                block.spans.pop()
            if block.spans:
                block.spans[0].text = block.spans[0].text.lstrip()
                block.spans[-1].text = block.spans[-1].text.rstrip()
            if not any(s.text for s in block.spans) and not block.marker:
                return
            self._mark_dropcap(block)
        self.blocks.append(block)

    @staticmethod
    def _mark_dropcap(block):
        """Is this paragraph's first letter a drop capital?

        Decided by what it *is* rather than by ``float: left``, which is how
        a book says so in CSS. Two reasons. A drop cap is one or two
        characters set several times the body size at the start of a
        paragraph — nothing else in a novel looks like that, so the shape is
        diagnostic on its own. And the alternative reading, if this is not
        recognised, is not "no drop cap": it is a 3.4em letter left inline,
        which inflates the first line to three times its height and leaves a
        band of white across the top of the chapter. Guessing wrong here
        costs a slightly odd capital; not guessing costs every chapter
        opening in the book.
        """
        if block.kind != PARA or block.marker or not block.spans:
            return
        first = block.spans[0]
        text = first.text.strip()
        # Two characters, because an opening quotation mark is routinely set
        # with the capital it belongs to.
        if 1 <= len(text) <= 2 and first.style.scale >= DROPCAP_SCALE:
            block.dropcap = True

    def _open(self, kind, **kw):
        self._flush()
        self._block = Block(kind, char_offset=self.counted, **kw)
        if self._pending_anchors:
            self._block.anchors += self._pending_anchors
            self._pending_anchors = []
        return self._block

    def _emit(self, node, kind, **kw):
        """A standalone block (image, rule) that interrupts the flow."""
        self._flush()
        block = Block(kind, char_offset=self.counted, **kw)
        if self._pending_anchors:
            block.anchors += self._pending_anchors
            self._pending_anchors = []
        self.blocks.append(block)
        return block

    # -- the walk ---------------------------------------------------------

    def walk(self, node, style, align, indent, pre, drawn=True,
             right=0.0):
        from .xmlish import Node

        for child in node.children:
            if isinstance(child, str):
                self._text(child, style, align, indent, pre, drawn)
                continue
            if not isinstance(child, Node):
                continue
            self._element(child, style, align, indent, pre, drawn, right)

    def _text(self, text, style, align, indent, pre, drawn):
        # THE counting rule, and it is epub.js's, verbatim: a node that is
        # all whitespace contributes nothing; any other contributes its full
        # length including its whitespace. Anything cleverer here (counting
        # the normalized text, say) produces a number that no other client
        # agrees with, which is worse than no number at all.
        offset = self.counted
        if text.strip():
            # UTF-16 units, as epub.js counts (locations.utf16_len). The two
            # walks must agree with each other AND with the original.
            from .locations import utf16_len

            self.counted += utf16_len(text)
        if not drawn:
            return
        if not pre:
            text = _WS.sub(" ", text)
            if not text.strip() and (self._block is None
                                     or not self._block.spans):
                # Whitespace between block tags. Emitting it would open an
                # empty paragraph for every newline in the source.
                return
        if self._block is None:
            self._open(PARA, align=align, indent=indent, pre=pre)
        if style.smallcaps:
            self._block.spans += _smallcaps_spans(text, style, offset)
            return
        self._block.spans.append(Span(text, style, offset))

    def _decls_for(self, node):
        """Every declaration that applies to ``node``, weakest first.

        Sheet then inline, which is the whole cascade this reader
        implements — an inline ``style`` beats any selector short of
        ``!important``, and ``!important`` is not implemented (see
        ``css.py``).
        """
        decls = {}
        if self.sheet is not None:
            decls.update(self.sheet.match(node))
        decls.update(_decls(node.get("style")))
        return decls

    def _element(self, node, style, align, indent, pre, drawn,
                 right=0.0):
        tag = node.tag
        if tag in _INVISIBLE:
            # Counted, never drawn — see the module docstring.
            self.walk(node, style, align, indent, pre, drawn=False)
            return
        if node.get("id"):
            self._pending_anchors.append(node.get("id"))
        decls = self._decls_for(node)
        if (node.get("hidden") is not None
                or decls.get("display") == "none"
                or decls.get("visibility") == "hidden"):
            self.walk(node, style, align, indent, pre, drawn=False)
            return

        if tag == "br":
            if drawn:
                if self._block is None:
                    self._open(PARA, align=align, indent=indent, pre=pre)
                # A hard break inside a paragraph, expressed as a newline in
                # the span text. The line breaker treats it as one; nothing
                # downstream needs a second concept for it.
                self._block.spans.append(Span("\n", style, self.counted))
            return
        if tag == "hr":
            if drawn:
                self._emit(node, RULE, indent=indent)
            return
        if tag in ("img", "image"):
            if drawn:
                self._image(node, align, indent)
            # Counted, never drawn. These are the only branches of this
            # function that return without recursing, and the count has to
            # agree with `locations.text_node_lengths`, which walks every
            # text node there is — as epub.js's own tree walker does. An
            # `<svg><title>` or a `<text>` label in a diagram is text to
            # the count and to no one else, and a document that skips it
            # here reports every position after it short.
            self.walk(node, style, align, indent, pre, drawn=False)
            return
        if tag == "svg":
            # An SVG wrapper around a raster image is how most epub covers
            # are built. The vector case is out of scope; the wrapper case is
            # most of them, and is just an <image> inside.
            if drawn:
                inner = node.find("image")
                if inner is not None:
                    self._image(inner, align or "center", indent)
            self.walk(node, style, align, indent, pre, drawn=False)
            return

        inner_style = style
        flag = _STYLE_TAGS.get(tag)
        if flag:
            inner_style = inner_style.with_(**{flag: True})
        if tag in ("small", "sub", "sup"):
            inner_style = inner_style.with_(
                scale=inner_style.scale * (0.8 if tag == "small"
                                           else _SMALL_SCALE))
        if tag == "sup":
            inner_style = inner_style.with_(rise=SUPERSCRIPT_RISE)
        elif tag == "sub":
            inner_style = inner_style.with_(rise=SUBSCRIPT_RISE)
        elif tag == "dt":
            # A definition list's term is bold in every browser and in
            # every book that sets one; without it a glossary is two
            # indistinguishable paragraphs per entry.
            inner_style = inner_style.with_(bold=True)
        inner_style = _apply_decls(inner_style, decls)

        inner_align = align
        if tag == "center":
            inner_align = "center"
        if node.get("align"):
            inner_align = node.get("align").lower()
        declared_align = decls.get("text-align", "")
        if declared_align in ("left", "right", "center", "justify"):
            inner_align = declared_align

        inner_indent = indent
        inner_right = right
        if tag == "blockquote":
            # Both sides. A quotation moved in from the left only is a
            # paragraph that has lost its first-line indent; what makes it
            # read as quoted matter is the narrower measure.
            inner_indent += INDENT_EM
            inner_right += INDENT_EM
        elif tag == "dd":
            inner_indent += INDENT_EM
        elif tag in ("ul", "ol", "dl") and self._list_depth(node) > 0:
            inner_indent += INDENT_EM
        # The stylesheet's own inset, on top of the structural one. This is
        # what sets verse in from the prose around it, and what a book uses
        # for a letter or a newspaper clipping quoted mid-chapter.
        inner_indent += _margin_em(decls, "margin-left")
        inner_right += _margin_em(decls, "margin-right")

        box = _box(decls)
        if tag in _HEADINGS:
            level = _HEADINGS[tag]
            # The tag's own size is a *default*: a stylesheet that sizes its
            # h2s explicitly has said what it wants, and overriding that with
            # our scale is how a book ends up with headings in a hierarchy
            # its author did not write.
            scale = inner_style.scale
            if "font-size" not in decls:
                scale = _heading_scale(level)
            self._open(HEADING, level=level, align=inner_align or "",
                       indent=inner_indent, indent_right=inner_right, **box)
            self.walk(node, inner_style.with_(bold=True, scale=scale),
                      inner_align, inner_indent, pre, drawn, inner_right)
            self._flush()
            return
        if tag == "li":
            # At least one step in, whatever the CSS said: the marker hangs
            # in that gutter, and at zero it would be set over the text.
            item_indent = max(INDENT_EM, inner_indent)
            self._open(PARA, align=inner_align, indent=item_indent,
                       indent_right=inner_right, marker=self._marker(node),
                       **box)
            self.walk(node, inner_style, inner_align, item_indent,
                      pre, drawn, inner_right)
            self._flush()
            return
        if tag == "pre":
            self._open(PARA, align=inner_align, indent=inner_indent,
                       indent_right=inner_right, pre=True, **box)
            self.walk(node, inner_style.with_(mono=True), inner_align,
                      inner_indent, True, drawn, inner_right)
            self._flush()
            return
        if tag in _BLOCK_TAGS:
            # Opened rather than merely flushed, so this element's own box
            # properties land on the block its text goes into — that is what
            # makes `<p class="chaptertitle">` a chapter title. A wrapper
            # element with no text of its own opens a block that its first
            # block-level child immediately flushes away empty, which is
            # what `_flush` drops.
            self._open(PARA, align=inner_align, indent=inner_indent,
                       indent_right=inner_right, pre=pre, **box)
            self.walk(node, inner_style, inner_align, inner_indent, pre,
                      drawn, inner_right)
            self._flush()
            return
        self.walk(node, inner_style, inner_align, inner_indent, pre, drawn,
                  inner_right)

    # -- pieces -----------------------------------------------------------

    def _image(self, node, align, indent):
        src = node.get("src") or node.get("href") or node.get("xlink:href")
        if not src:
            return
        resolved = self.resolve(self.base_href, src)
        if not resolved:
            return
        self._emit(node, IMAGE, src=resolved, alt=node.get("alt") or "",
                   align=align or "center", indent=indent,
                   box=_image_box(node, self._decls_for(node)))

    @staticmethod
    def _list_depth(node):
        depth = 0
        parent = node.parent
        while parent is not None:
            if parent.tag in ("ul", "ol", "dl"):
                depth += 1
            parent = parent.parent
        return depth

    def _marker(self, node):
        """The bullet or number in front of a list item.

        Numbers are counted from the item's position among its siblings
        rather than tracked through the walk: a nested list restarts, which
        is what an author means by nesting one, and an `<ol start="3">`
        keeps its offset.

        **The style is asked of three places in the order CSS resolves
        them**: the item itself, its list, and then the tag's own default.
        Books really do use all three — a cast of characters set as a
        ``<ul>`` with ``list-style-type: none``, an appendix numbered
        ``<ol type="i">``, a nested list left to the defaults — and reading
        only the last one puts dots down the side of a page the author
        deliberately left clean.
        """
        parent = node.parent
        kind = self._list_style(node, parent)
        if kind == "none":
            return ""
        ordered = parent is not None and parent.tag == "ol"
        if kind in _BULLETS:
            return _BULLETS[kind]
        if not ordered and kind not in _NUMBERING.values():
            # An unordered list, and nothing said otherwise: cycle the
            # bullet by depth the way every browser does. Minus one because
            # `_list_depth` counts from the item, whose own list is the
            # first ancestor it finds — a top-level list is depth 1 there
            # and wants the first bullet, not the second.
            return _UL_DEPTH[min(max(0, self._list_depth(node) - 1),
                                 len(_UL_DEPTH) - 1)]
        try:
            start = int((parent.get("start") if parent else None) or 1)
        except (TypeError, ValueError):
            start = 1
        number = start
        if parent is not None:
            index = 0
            for child in parent.children:
                if getattr(child, "tag", None) == "li":
                    if child is node:
                        break
                    index += 1
            number = start + index
        return _ordinal(number, kind) + "."

    def _list_style(self, node, parent):
        """The resolved ``list-style-type``, or "" for the tag default."""
        own = self._decls_for(node).get("list-style-type")
        if own:
            return own
        if parent is None:
            return ""
        declared = self._decls_for(parent).get("list-style-type")
        if declared:
            return declared
        if parent.tag == "ol":
            return _NUMBERING.get(parent.get("type") or "", "decimal")
        return ""


#: Baseline shifts for ``<sup>`` and ``<sub>``, in ems of the body size.
#: A third of an em up is what a text face's own superscript sits at; a
#: sixth down is the matching subscript, which is shallower because it has
#: descenders to clear rather than ascenders.
SUPERSCRIPT_RISE = 0.34
SUBSCRIPT_RISE = -0.16

#: How much smaller a raised or lowered run is set.
_SMALL_SCALE = 0.72

#: How much smaller a "capital" that was lowercase is set, in small caps.
#: Real small caps are a separate cut of the face with its own weight; this
#: is the synthetic approximation every renderer without one falls back to.
SMALLCAPS_SCALE = 0.78

_ROMAN = ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"),
          (90, "xc"), (50, "l"), (40, "xl"), (10, "x"), (9, "ix"),
          (5, "v"), (4, "iv"), (1, "i"))


def _ordinal(number, kind):
    """``(3, "lower-alpha")`` -> ``"c"``. Decimal for anything unknown."""
    if kind in ("lower-alpha", "upper-alpha", "lower-latin", "upper-latin"):
        text = _alpha(number)
    elif kind in ("lower-roman", "upper-roman"):
        text = _roman(number)
    else:
        return "%d" % number
    return text.upper() if kind.startswith("upper") else text


def _alpha(number):
    """1 -> a, 26 -> z, 27 -> aa, as CSS counts."""
    out = ""
    number = max(1, number)
    while number > 0:
        number, rest = divmod(number - 1, 26)
        out = chr(ord("a") + rest) + out
    return out


def _roman(number):
    if not 1 <= number < 4000:
        return "%d" % number
    out = []
    for value, glyph in _ROMAN:
        while number >= value:
            out.append(glyph)
            number -= value
    return "".join(out)


def _smallcaps_spans(text, style, offset):
    """Split ``text`` into the runs small caps needs.

    Pillow has no small-caps variant and no way to synthesise one, so the
    approximation is the classic one: uppercase everything, and set what
    *was* lowercase smaller. That has to happen here rather than at paint
    time because it changes where the runs begin and end, and the layer
    below deals in runs.

    Every run keeps the *same* character offset. They came out of one text
    node, and the offset is a count of characters before that node — see
    the module docstring. Splitting a node does not create new positions in
    epub.js's index, and inventing some here would report a position no
    other client can resolve.
    """
    out = []
    # The reduced run KEEPS the flag, which is what lets `plain_text` undo
    # this exactly: those are precisely the characters that were lowercase,
    # so lowercasing them again restores the author's text. Nothing keys a
    # font off the flag (it is not in `Style.key`), so carrying it costs
    # nothing.
    small = style.with_(scale=style.scale * SMALLCAPS_SCALE)
    plain = style.with_(smallcaps=False)
    run = []
    run_small = None
    for char in text:
        is_small = char.islower()
        if run_small is not None and is_small != run_small:
            out.append(Span("".join(run).upper() if run_small
                            else "".join(run),
                            small if run_small else plain, offset))
            run = []
        run_small = is_small
        run.append(char)
    if run:
        out.append(Span("".join(run).upper() if run_small else "".join(run),
                        small if run_small else plain, offset))
    return out


#: What a percentage margin is a percentage OF, in ems.
#:
#: CSS says the containing block's width, which this reader does not model
#: — but it does cap the measure, and that cap is the width of every block
#: on the page (``layout.ReaderStyle.max_measure``). Reading `12.5%` as
#: `0.125em`, which is what the font-size path returns for a percentage,
#: makes a sidebar's inset a fifth of one character: not wrong-looking so
#: much as absent, which is the failure the property exists to prevent.
MEASURE_EM = 34.0


def _margin_em(decls, prop):
    """A horizontal margin in ems, or 0. Negative margins are dropped —
    pulling a block out past the reader's own margin puts it off the page,
    and the books that do it are compensating for a box model this reader
    does not have."""
    from .css import length_em

    value = decls.get(prop)
    if not value or value in ("auto", "0"):
        return 0.0
    em = length_em(value)
    if em is None:
        return 0.0
    if value.strip().endswith("%"):
        em *= MEASURE_EM
    return max(0.0, min(em, 8.0))


#: An HTML presentation attribute (``width="16"``) is a bare number of
#: pixels. A CSS value never is, so the two cannot be read the same way.
_BARE_NUMBER = re.compile(r"^[+-]?[\d.]+$")


def _declared_size(node, decls, prop):
    """One declared length -> ``(value, is_fraction)``, or None.

    The value is in ems of the body size; ``is_fraction`` marks a
    percentage, which is of the measure and so cannot be folded into it.

    Both spellings are read, because the attribute is what an old book
    carries and the stylesheet what a new one does, and the cascade puts
    CSS above the attribute. Ems rather than pixels for the same reason the
    measure is capped in them: the reader's body size is a setting, and an
    icon the author sized against the text should stay sized against the
    text. Absolute units go through css.py's nominal 16px body, which is
    what every epub was styled against.
    """
    from .css import length_em

    value = decls.get(prop)
    if not value and prop in ("width", "height"):
        # A presentation attribute (`width="16"`) is a bare number of
        # pixels. A CSS value never is, so the two cannot be read alike.
        value = node.get(prop)
        if value and _BARE_NUMBER.match(value.strip()):
            value = value.strip() + "px"
    if not value:
        return None
    value = value.strip().lower()
    if value in ("auto", "inherit", "initial", "unset", "none", "0"):
        return None
    em = length_em(value)
    if em is None or em <= 0:
        return None
    return (em, value.endswith("%"))


def _image_box(node, decls):
    """What the book says about how big an image should be drawn.

    **Books say something more often than they look like they do**, and the
    natural pixel size of the file is a poor stand-in when they do — the
    case that prompted this was a cookbook whose step arrows are 256 px
    PNGs set to about a line tall, drawn at their own size and swallowing
    the page. ``max-width``/``max-height`` are read as well as the plain
    ones, and are the commoner spelling for exactly these little marks.

    Empty when nothing was said, which is what makes "use the file's size"
    still the default.
    """
    box = {}
    for prop in ("width", "height", "max-width", "max-height"):
        declared = _declared_size(node, decls, prop)
        if declared is not None:
            box[prop] = declared
    return box


def _heading_scale(level):
    """Heading sizes, as multiples of the body size.

    The web's defaults (2.0 down to 0.67) are for a page with wide margins
    and a lot of chrome; in a single-column reader an h1 at 2em swallows a
    third of the page. These are the same shape, compressed.
    """
    return {1: 1.7, 2: 1.45, 3: 1.25, 4: 1.12, 5: 1.05, 6: 1.0}.get(level, 1.0)


_STYLE_DECL = re.compile(r"([\w-]+)\s*:\s*([^;]+)")


def _decls(style_attr):
    if not style_attr:
        return {}
    return {m.group(1).lower(): m.group(2).strip().lower()
            for m in _STYLE_DECL.finditer(style_attr)}


def _apply_decls(style, decls):
    """Fold the declarations this reader understands into a :class:`Style`.

    Every property here is applied in **both** directions where CSS allows
    one — ``font-weight: normal`` inside a ``<b>`` turns the bold off. A
    one-way reading looks harmless until a book styles its emphasis by
    class and wraps it in ``<strong>`` for older readers, at which point
    everything is bold.
    """
    if not decls:
        return style
    weight = decls.get("font-weight") or ""
    if weight in ("bold", "bolder") or (weight.isdigit()
                                        and int(weight) >= 600):
        style = style.with_(bold=True)
    elif weight in ("normal", "lighter") or (weight.isdigit()
                                             and int(weight) < 600):
        style = style.with_(bold=False)
    font_style = decls.get("font-style")
    if font_style in ("italic", "oblique"):
        style = style.with_(italic=True)
    elif font_style == "normal":
        style = style.with_(italic=False)
    decoration = decls.get("text-decoration")
    if decoration is not None:
        style = style.with_(underline="underline" in decoration,
                            strike="line-through" in decoration)
    family = decls.get("font-family") or ""
    if "monospace" in family or "courier" in family or "consolas" in family:
        style = style.with_(mono=True)
    variant = (decls.get("font-variant-caps")
               or decls.get("font-variant") or "")
    if "small-caps" in variant:
        style = style.with_(smallcaps=True)
    elif variant in ("normal", "none"):
        style = style.with_(smallcaps=False)
    align = decls.get("vertical-align")
    if align in ("super", "sub"):
        # Scale it too, unless the sheet also said a size — a superscript
        # set at the body size is a footnote marker that looks like a typo,
        # and every face's own superscript glyphs are around 0.7em.
        style = style.with_(rise=SUPERSCRIPT_RISE if align == "super"
                            else SUBSCRIPT_RISE)
        if "font-size" not in decls:
            style = style.with_(scale=style.scale * _SMALL_SCALE)
    elif align in ("baseline", "initial"):
        style = style.with_(rise=0.0)
    size = decls.get("font-size")
    if size:
        from .css import font_scale

        scale = font_scale(size, style.scale)
        if scale is not None:
            # Clamped, because a book that sets 0.1em on a class we then
            # apply to a whole chapter should be unreadable-small, not
            # zero-height — and a page of 40em text is a rendering loop
            # with a very large bitmap at the end of it.
            style = style.with_(scale=max(0.4, min(scale, 4.0)))
    return style


def _box(decls):
    """The block-level properties, as :class:`Block` keyword arguments."""
    from .css import length_em

    out = {}
    for prop, name in (("margin-top", "space_before"),
                       ("margin-bottom", "space_after")):
        value = decls.get(prop)
        if value is not None:
            em = 0.0 if value in ("0", "auto") else length_em(value)
            if em is not None:
                out[name] = max(0.0, min(em, 4.0))
    indent = decls.get("text-indent")
    if indent is not None:
        em = length_em(indent)
        if em is not None:
            out["first_indent"] = max(-4.0, min(em, 8.0))
    if (decls.get("page-break-before") or "") == "always":
        out["page_break"] = True
    return out


def parse_document(markup, base_href="", resolve=None, sheet=None):
    """Parse one spine document into ``(blocks, counted_chars)``.

    ``resolve(base_href, href)`` turns a link in the document into an
    archive path; :meth:`~.archive.EpubPackage.resolve` is the one to pass.
    ``sheet`` is a :class:`~.css.Stylesheet` (see :func:`parse_spine_item`).
    ``counted_chars`` is this document's contribution to the locations
    index — the total the last span's offset is measured against.
    """
    from . import xmlish

    root = xmlish.parse(markup)
    body = root.find("body") or root
    walker = _Walker(base_href, resolve or (lambda _b, h: h), sheet)
    walker.walk(body, Style(), "", 0, False)
    walker._flush()
    return walker.blocks, walker.counted


def parse_spine_item(package, index, css_cache=None):
    """Parse spine document ``index`` of ``package``, stylesheets included.

    The one-call form, and the one every caller in the app wants: it reads
    the document, finds and parses the stylesheets it links, and walks it.
    ``css_cache`` is a dict the caller keeps across documents — a book's
    chapters share two or three sheets, and re-parsing them per chapter is
    most of the cost of opening the book.
    """
    from . import css, xmlish

    href = package.spine[index].href
    root = xmlish.parse(package.doc_bytes(index))
    sheet = css.sheet_for(package, href, root,
                          css_cache if css_cache is not None else {})
    body = root.find("body") or root
    walker = _Walker(href, package.resolve, sheet)
    walker.walk(body, Style(), "", 0, False)
    walker._flush()
    return walker.blocks, walker.counted
