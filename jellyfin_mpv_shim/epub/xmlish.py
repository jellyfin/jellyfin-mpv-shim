"""One tolerant markup reader, used for every file inside an epub.

An epub is four kinds of markup — `META-INF/container.xml`, the OPF package
document, the EPUB 3 nav document, and the XHTML of the book itself — and
this module reads all four with :class:`html.parser.HTMLParser` rather than
with an XML parser. That is a deliberate choice on two grounds.

**Entity expansion.** ``xml.etree.ElementTree`` is expat underneath, and
expat expands internal entities: a fourteen-line "billion laughs" document
measured here on CPython 3.13 parses to a 3000-character string at three
levels of nesting and to gigabytes at nine. The file comes off a media
server, which got it from whatever the user put in their library, so it is
not ours to trust. ``html.parser`` never processes a DTD at all — a
``<!DOCTYPE …>`` internal subset arrives as one opaque string through
``handle_decl`` and is dropped — so the whole class of attack is absent
rather than mitigated. It also cannot fetch an external DTD, which is the
other half of the same problem.

**Real epubs are not well-formed.** Unclosed ``<p>``, bare ``&``, stray
``<br>``, mismatched nesting and the occasional Windows-1252 byte are all
routine in shipped books, and an XML parser is *required* to stop dead on
each of them. A reader that refuses a quarter of a library is not a reader.
`html.parser` recovers from all of it, which is the same bet every browser
makes and the same one jellyfin-web makes by handing the file to one.

What this costs: no namespace resolution, no CDATA sections, no XML
validation. Namespaces are handled by matching on the *local* name
(``package``, ``item``, ``itemref``), which is what every epub in the wild
uses anyway and what an epub with a prefixed namespace needs regardless.
"""

import re
from html.parser import HTMLParser

#: Elements that never have content, so a document that closes them anyway
#: (``<br/></br>``) and one that never does must produce the same tree.
VOID_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input", "link",
    "meta", "param", "source", "track", "wbr",
})


class Node:
    """One element. Attributes are lowercased and namespace-stripped."""

    __slots__ = ("tag", "attrs", "children", "parent")

    def __init__(self, tag, attrs=None, parent=None):
        self.tag = tag
        self.attrs = attrs or {}
        #: Element children and text, interleaved in document order — text is
        #: a `str`, an element is a :class:`Node`. Order matters: an inline
        #: run reads as "text, <em>, text" and losing the interleaving loses
        #: the sentence.
        self.children = []
        self.parent = parent

    def get(self, name, default=None):
        return self.attrs.get(name, default)

    def find_all(self, tag):
        """Every descendant with this local name, in document order."""
        out = []
        stack = [self]
        while stack:
            node = stack.pop()
            if node is not self and node.tag == tag:
                out.append(node)
            # Children are pushed reversed so the stack pops them first-first:
            # that makes the walk pre-order, and pre-order is document order.
            # Getting this backwards is not a subtle bug — it silently
            # reverses the spine, and a book opens at its last chapter.
            for child in reversed(node.children):
                if isinstance(child, Node):
                    stack.append(child)
        return out

    def find(self, tag):
        for node in self.find_all(tag):
            return node
        return None

    def text(self):
        """All descendant text, concatenated. Whitespace as written."""
        parts = []
        stack = [self]
        while stack:
            node = stack.pop()
            if isinstance(node, str):
                parts.append(node)
                continue
            for child in reversed(node.children):
                stack.append(child)
        return "".join(parts)

    def __repr__(self):
        return "<%s %r %d children>" % (self.tag, self.attrs,
                                        len(self.children))


def _local(name):
    """``opf:item`` -> ``item``. See the module docstring on namespaces."""
    return name.rsplit(":", 1)[-1].lower()


class _Builder(HTMLParser):
    def __init__(self):
        # convert_charrefs resolves the *standard* named and numeric
        # references (``&amp;``, ``&#8212;``) into text, which is what we
        # want. An entity the DTD would have had to define is not in that
        # table and is left as literal text — the one visible consequence of
        # never reading the DTD, and a far better failure than expanding it.
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self._stack = [self.root]

    # -- elements ---------------------------------------------------------

    def handle_starttag(self, tag, attrs):
        tag = _local(tag)
        node = Node(tag, {_local(k): (v if v is not None else "")
                          for k, v in attrs}, self._stack[-1])
        self._stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        # `<p/>` in XHTML is an empty paragraph, not the start of one. Going
        # through handle_starttag and popping immediately keeps one code path
        # for the attributes.
        self.handle_starttag(tag, attrs)
        if _local(tag) not in VOID_TAGS:
            self._stack.pop()

    def handle_endtag(self, tag):
        tag = _local(tag)
        if tag in VOID_TAGS:
            return
        # Close to the nearest matching ancestor, not blindly: `<b><i></b>`
        # should end the bold and leave the italic's content where it is,
        # and an end tag with no open counterpart at all is noise to drop.
        for depth in range(len(self._stack) - 1, 0, -1):
            if self._stack[depth].tag == tag:
                del self._stack[depth:]
                return

    def handle_data(self, data):
        if data:
            self._stack[-1].children.append(data)

    # -- everything we deliberately drop ----------------------------------

    def handle_decl(self, decl):
        """``<!DOCTYPE …>``, internal subset and all, as one string.

        Dropped without inspection. This is the hook expat would use to
        define entities; there is nothing here that could expand one.
        """

    def handle_pi(self, data):
        pass

    def unknown_decl(self, data):
        # A CDATA section arrives here rather than as data, which loses the
        # text inside one. That is a real gap and an acceptable one: CDATA in
        # an XHTML content document is almost exclusively used to wrap
        # embedded CSS and script, neither of which this reader draws.
        pass


def parse(data):
    """Parse markup into a :class:`Node` tree rooted at ``#document``.

    ``data`` may be `bytes` or `str`; bytes are decoded by
    :func:`decode`. Never raises on malformed input — that is the whole
    point of this parser — so callers check for the elements they need
    rather than catching.
    """
    if isinstance(data, (bytes, bytearray)):
        data = decode(data)
    # XML line-end normalization (XML 1.0 §2.11), which a conforming parser
    # is *required* to do and `html.parser` does not. It is not cosmetic
    # here: a CR left in a `<pre>` code listing has no glyph in any face, so
    # it draws as a tofu box at the end of every line — and books converted
    # on Windows are full of them. It also keeps the character count right,
    # because the DOM epub.js counts never contained the CR either.
    if "\r" in data:
        data = data.replace("\r\n", "\n").replace("\r", "\n")
    builder = _Builder()
    builder.feed(data)
    builder.close()
    return builder.root


#: ``<?xml version="1.0" encoding="…"?>`` and ``<meta charset=…>``, which are
#: the two places an epub states its encoding. Matched against the first
#: kilobyte as *bytes*, since the whole point is that we cannot decode yet.
_XML_DECL = re.compile(rb"""<\?xml[^>]*encoding\s*=\s*["']([\w.-]+)["']""",
                       re.I)
_META_CHARSET = re.compile(rb"""<meta[^>]*charset\s*=\s*["']?([\w.-]+)""", re.I)


def decode(data):
    """Bytes -> str, by the encoding the document declares.

    UTF-8 is the epub specification's answer and is what almost every file
    is, but "almost" is doing work: hand-built and converted books ship
    Windows-1252 and Latin-1 regularly. The order is declared encoding, then
    UTF-8, then cp1252 — and cp1252 last because it maps every byte, so it
    can never fail and would mask the two answers that are usually right.
    """
    if data[:3] == b"\xef\xbb\xbf":
        return data[3:].decode("utf-8", "replace")
    head = data[:1024]
    match = _XML_DECL.search(head) or _META_CHARSET.search(head)
    encodings = []
    if match:
        try:
            encodings.append(match.group(1).decode("ascii").lower())
        except UnicodeDecodeError:
            pass
    encodings += ["utf-8", "cp1252"]
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return data.decode("utf-8", "replace")
