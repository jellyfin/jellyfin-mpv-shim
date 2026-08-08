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

_WS = re.compile(r"\s+")


class Style:
    """The style flags a run of text carries. Immutable; ``with_`` copies."""

    __slots__ = ("bold", "italic", "underline", "strike", "mono", "scale")

    def __init__(self, bold=False, italic=False, underline=False,
                 strike=False, mono=False, scale=1.0):
        self.bold = bold
        self.italic = italic
        self.underline = underline
        self.strike = strike
        self.mono = mono
        #: Multiplier on the body font size. Headings carry it, and so do
        #: ``<small>``/``<sup>``; it is a float rather than a font size so a
        #: reader-set body size scales the whole document.
        self.scale = scale

    def with_(self, **kw):
        out = Style(self.bold, self.italic, self.underline, self.strike,
                    self.mono, self.scale)
        for key, value in kw.items():
            setattr(out, key, value)
        return out

    def key(self):
        return (self.bold, self.italic, self.underline, self.strike,
                self.mono, round(self.scale, 3))

    def __eq__(self, other):
        return isinstance(other, Style) and self.key() == other.key()

    def __hash__(self):
        return hash(self.key())

    def __repr__(self):
        on = [n for n in ("bold", "italic", "underline", "strike", "mono")
              if getattr(self, n)]
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

    __slots__ = ("kind", "spans", "level", "align", "indent", "marker",
                 "src", "alt", "char_offset", "pre", "anchors",
                 "space_before", "space_after", "first_indent", "page_break")

    def __init__(self, kind, spans=None, level=0, align="", indent=0,
                 marker="", src=None, alt="", char_offset=0, pre=False,
                 space_before=None, space_after=None, first_indent=None,
                 page_break=False):
        self.kind = kind
        self.spans = spans or []
        self.level = level
        #: "", "center", "right", "justify". Empty means the reader's
        #: default, which is not the same as "left" — a reader set to
        #: justify should justify a paragraph that said nothing.
        self.align = align
        self.indent = indent
        self.marker = marker
        self.src = src
        self.alt = alt
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
        return "".join(s.text for s in self.spans)

    def is_empty(self):
        return self.kind in (PARA, HEADING) and not self.text().strip()

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
        self.blocks.append(block)

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

    def walk(self, node, style, align, indent, pre, drawn=True):
        from .xmlish import Node

        for child in node.children:
            if isinstance(child, str):
                self._text(child, style, align, indent, pre, drawn)
                continue
            if not isinstance(child, Node):
                continue
            self._element(child, style, align, indent, pre, drawn)

    def _text(self, text, style, align, indent, pre, drawn):
        # THE counting rule, and it is epub.js's, verbatim: a node that is
        # all whitespace contributes nothing; any other contributes its full
        # length including its whitespace. Anything cleverer here (counting
        # the normalized text, say) produces a number that no other client
        # agrees with, which is worse than no number at all.
        offset = self.counted
        if text.strip():
            self.counted += len(text)
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

    def _element(self, node, style, align, indent, pre, drawn):
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
            return
        if tag == "svg":
            # An SVG wrapper around a raster image is how most epub covers
            # are built. The vector case is out of scope; the wrapper case is
            # most of them, and is just an <image> inside.
            if drawn:
                inner = node.find("image")
                if inner is not None:
                    self._image(inner, align or "center", indent)
            return

        inner_style = style
        flag = _STYLE_TAGS.get(tag)
        if flag:
            inner_style = inner_style.with_(**{flag: True})
        if tag in ("small", "sub", "sup"):
            inner_style = inner_style.with_(scale=inner_style.scale * 0.8)
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
        if tag in ("blockquote", "dd"):
            inner_indent += 1
        elif tag in ("ul", "ol", "dl") and self._list_depth(node) > 0:
            inner_indent += 1

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
            self._open(HEADING, level=level,
                       align=inner_align or "", indent=inner_indent, **box)
            self.walk(node, inner_style.with_(bold=True, scale=scale),
                      inner_align, inner_indent, pre, drawn)
            self._flush()
            return
        if tag == "li":
            self._open(PARA, align=inner_align, indent=max(1, inner_indent),
                       marker=self._marker(node), **box)
            self.walk(node, inner_style, inner_align, max(1, inner_indent),
                      pre, drawn)
            self._flush()
            return
        if tag == "pre":
            self._open(PARA, align=inner_align, indent=inner_indent, pre=True,
                       **box)
            self.walk(node, inner_style.with_(mono=True), inner_align,
                      inner_indent, True, drawn)
            self._flush()
            return
        if tag in _BLOCK_TAGS:
            # Opened rather than merely flushed, so this element's own box
            # properties land on the block its text goes into — that is what
            # makes `<p class="chaptertitle">` a chapter title. A wrapper
            # element with no text of its own opens a block that its first
            # block-level child immediately flushes away empty, which is
            # what `_flush` drops.
            self._open(PARA, align=inner_align, indent=inner_indent, pre=pre,
                       **box)
            self.walk(node, inner_style, inner_align, inner_indent, pre,
                      drawn)
            self._flush()
            return
        self.walk(node, inner_style, inner_align, inner_indent, pre, drawn)

    # -- pieces -----------------------------------------------------------

    def _image(self, node, align, indent):
        src = node.get("src") or node.get("href") or node.get("xlink:href")
        if not src:
            return
        resolved = self.resolve(self.base_href, src)
        if not resolved:
            return
        self._emit(node, IMAGE, src=resolved, alt=node.get("alt") or "",
                   align=align or "center", indent=indent)

    @staticmethod
    def _list_depth(node):
        depth = 0
        parent = node.parent
        while parent is not None:
            if parent.tag in ("ul", "ol", "dl"):
                depth += 1
            parent = parent.parent
        return depth

    @staticmethod
    def _marker(node):
        """The bullet or number in front of a list item.

        Numbers are counted from the item's position among its siblings
        rather than tracked through the walk: a nested list restarts, which
        is what an author means by nesting one, and an `<ol start="3">`
        keeps its offset.
        """
        parent = node.parent
        if parent is None or parent.tag != "ol":
            return "•"
        try:
            start = int(parent.get("start") or 1)
        except ValueError:
            start = 1
        index = 0
        for child in parent.children:
            if getattr(child, "tag", None) == "li":
                if child is node:
                    return "%d." % (start + index)
                index += 1
        return "%d." % start


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
