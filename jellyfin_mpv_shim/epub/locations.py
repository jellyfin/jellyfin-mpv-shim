"""epub.js's locations index, reimplemented so our position means theirs.

Jellyfin stores an epub's reading position as ``PlaybackPositionTicks``
against a ``RunTimeTicks`` of exactly one second, i.e. as a fraction (see
``jellyfin_mpv_shim/books.py``). That fraction is not "how far through the
text you are" in any sense you could derive from first principles — it is
``location / total`` over **epub.js's locations index**, the structure
jellyfin-web's book player builds with ``locations.generate(1024)`` before
it reports anything. Any other reading of the number disagrees with every
other Jellyfin client, which is the one thing a stored position must not
do.

So this module is a port, and it is a port of behaviour rather than of
code — no CFIs, no `EpubCFI`, no DOM. What epub.js is really doing is
cutting each linear spine document into runs of ~1024 characters of text
and counting the runs; the position is the number of complete runs before
you, and the total is the number of runs in the book, less one. Everything
below is that, with the four details that make the numbers agree:

1. **The counter resets at every section boundary, and each section closes
   its partial tail as a whole location.** This is the big divergence from
   ``chars_read / chars_total``: a book of many short sections inflates its
   total, one long novel barely at all.
2. **A run that starts inside a text node consumes 1025 characters, not
   1024** — ``dist`` is computed before the ``counter === 0`` branch does
   ``pos += 1``. A run continued from the previous node consumes exactly
   ``dist``.
3. **A text node that is entirely whitespace counts zero; any other counts
   its full length, whitespace included.**
4. **``total`` is ``len(locations) - 1``**, which puts the denominator one
   short. It only matters for a short book, where it matters a lot.

Non-linear spine items (``linear="no"`` — covers, footnote documents) are
excluded from the index entirely, as they are in epub.js, which walks only
sections whose ``linear`` is truthy.

**Granularity is absolute, not relative**: ~1024 characters per step in
every book. A 600 KB novel gets 0.17% steps; a 20 KB short story gets 5%.
That is a property of the format we are interoperating with, and the
reason the UI never shows the user this number as a percentage of anything
without also showing them a page.
"""

import bisect
import logging

log = logging.getLogger("epub.locations")

#: The break size jellyfin-web passes to ``locations.generate``. In
#: characters — not a location count, which is the reading that makes the
#: whole algorithm look wrong.
BREAK_CHARS = 1024


class SectionLocations:
    """Where the location boundaries fall inside one spine document."""

    __slots__ = ("spine_index", "chars", "ends", "first_location")

    def __init__(self, spine_index, chars, ends, first_location=0):
        self.spine_index = spine_index
        #: Counted characters in this document (rule 3 above).
        self.chars = chars
        #: Counted-character offset at which each location *ends*, in order.
        #: The last entry is the tail close, so ``len(ends)`` is this
        #: document's location count and is at least 1 for any document with
        #: text in it.
        self.ends = ends
        #: Index of this document's first location within the whole book.
        self.first_location = first_location

    @property
    def count(self):
        return len(self.ends)


class LocationIndex:
    """The whole book's index: the thing a position is measured against."""

    def __init__(self, sections):
        self.sections = sections
        location = 0
        for section in sections:
            section.first_location = location
            location += section.count
        #: Total locations, epub.js's way: one less than the number of
        #: entries in its flat array. Never negative.
        self.count = location
        self.total = max(0, location - 1)
        self._by_spine = {s.spine_index: s for s in sections}

    # -- forwards: where am I? --------------------------------------------

    def location_of(self, spine_index, char_offset):
        """Global location index for a position in the book.

        ``char_offset`` is in *counted* characters within that spine
        document — the number :mod:`.content` puts on every span, which is
        why the two walks must stay in step.
        """
        section = self._by_spine.get(spine_index)
        if section is None:
            # A non-linear document, or one with no text. It contributes no
            # locations, so the honest answer is the position of whatever
            # linear document precedes it: reporting 0 would tell the server
            # the reader went back to the start.
            return self._preceding_location(spine_index)
        # Locations completed *before* this offset. bisect_right, because a
        # position exactly on a boundary is at the start of the next
        # location, not at the end of the one that just closed.
        local = bisect.bisect_right(section.ends, char_offset)
        return section.first_location + min(local, section.count - 1)

    def _preceding_location(self, spine_index):
        best = 0
        for section in self.sections:
            if section.spine_index < spine_index:
                best = section.first_location + section.count - 1
        return best

    def fraction(self, spine_index, char_offset):
        """Position as the fraction Jellyfin stores. 0.0 when unknown."""
        if not self.total:
            return 0.0
        location = self.location_of(spine_index, char_offset)
        return max(0.0, min(location / float(self.total), 1.0))

    # -- backwards: put me back there -------------------------------------

    def position_of(self, fraction):
        """``(spine_index, char_offset)`` for a stored fraction.

        Rounds **forward**, as jellyfin-web's ``cfiFromPercentage`` does
        (``Math.ceil(total * p)``): reopening a book at or just past where
        you stopped is a re-read of at most a sentence, while rounding back
        can drop you a page behind and, on a book read to the end, never
        let you reach it.
        """
        if not self.sections:
            return 0, 0
        target = 0
        if self.total:
            import math

            target = int(math.ceil(max(0.0, min(fraction, 1.0)) * self.total))
        target = max(0, min(target, self.count - 1))
        for section in self.sections:
            if target < section.first_location + section.count:
                local = target - section.first_location
                # The location *starts* where the previous one ended.
                offset = section.ends[local - 1] if local > 0 else 0
                return section.spine_index, offset
        last = self.sections[-1]
        return last.spine_index, last.ends[-1] if last.ends else 0

    # -- serialization ----------------------------------------------------

    def to_json(self):
        return {"v": 1, "break": BREAK_CHARS,
                "sections": [[s.spine_index, s.chars, s.ends]
                             for s in self.sections]}

    @classmethod
    def from_json(cls, data):
        if not isinstance(data, dict) or data.get("v") != 1:
            raise ValueError("unknown locations index format")
        if data.get("break") != BREAK_CHARS:
            # A cache built against a different break size is not a cache,
            # it is a different index. Rebuilding is a second of work; using
            # it would silently report the wrong position forever.
            raise ValueError("locations index built with a different break")
        return cls([SectionLocations(spine, chars, list(ends))
                    for spine, chars, ends in data["sections"]])


def count_section(lengths, brk=BREAK_CHARS):
    """Location boundaries for one document, from its text-node lengths.

    ``lengths`` is every text node's length in document order, with
    whitespace-only nodes already dropped (rule 3). Returns
    ``(chars, ends)``.

    This is epub.js's ``Locations.parse`` with the CFI machinery removed:
    the loop below is its loop, including the ``pos += 1`` that makes a
    freshly started run cost one extra character. Do not "simplify" it into
    ``chars // brk`` — that is a different function, off by a location
    every ~1024 characters and by more at every node boundary.
    """
    ends = []
    counter = 0
    consumed = 0
    for length in lengths:
        if length <= 0:
            continue
        base = consumed
        consumed += length
        pos = 0
        dist = brk - counter
        # Node smaller than what is left of the break: swallow it whole and
        # carry the counter into the next node.
        if dist > length:
            counter += length
            continue
        while pos < length:
            dist = brk - counter
            if counter == 0:
                pos += 1
            if pos + dist >= length:
                counter += length - pos
                pos = length
            else:
                pos += dist
                ends.append(base + pos)
                counter = 0
    if consumed:
        # The tail close, and it is **unconditional** — verified against the
        # source, not inferred. epub.js's close is
        # ``if (range && range.startContainer && prev)``, and both are set
        # by any non-whitespace node, so a section whose text happens to end
        # exactly on a break boundary pushes its already-closed range a
        # second time. That degenerate location is why a section's count is
        # always "closed runs + 1" and never "closed runs"; dropping it
        # would make our total smaller than the total every other client
        # divides by.
        ends.append(consumed)
    return consumed, ends


def text_node_lengths(markup):
    """Every counted text-node length in a document, in document order.

    Walks ``<body>`` only, because epub.js's ``sprint`` builds its tree
    walker on the body element. Script and style text *is* included: those
    are text nodes in the DOM that walker sees, and matching it is the
    entire point.
    """
    from . import xmlish

    root = xmlish.parse(markup)
    body = root.find("body") or root
    out = []
    stack = [body]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            if node.strip():
                out.append(len(node))
            continue
        for child in reversed(node.children):
            stack.append(child)
    return out


def build(package, progress=None):
    """Build the index for a whole book. Reads every linear spine document.

    ``progress(done, total)`` is called after each document, for a UI that
    wants to say what it is doing — this is the one part of opening a book
    that takes a noticeable moment, because it is the only part that has to
    read all of it.
    """
    from .archive import EpubError

    linear = [(i, item) for i, item in enumerate(package.spine)
              if item.linear]
    sections = []
    for done, (index, _item) in enumerate(linear, 1):
        try:
            lengths = text_node_lengths(package.doc_bytes(index))
        except EpubError:
            log.info("spine %d unreadable; excluded from the index", index,
                     exc_info=True)
            lengths = []
        chars, ends = count_section(lengths)
        if ends:
            sections.append(SectionLocations(index, chars, ends))
        if progress is not None:
            progress(done, len(linear))
    return LocationIndex(sections)
