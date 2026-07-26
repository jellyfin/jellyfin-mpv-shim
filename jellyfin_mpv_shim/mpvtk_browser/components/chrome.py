"""Content-area primitives: the busy and error states, body text, widths.

Pure functions — data in, widget tree out — so every page can reach them
through ``PageContext`` instead of the shell. See
``docs/ARCHITECTURE_TARGET.md`` §1.4 for the test being applied: a component
may need render resources and callbacks, never ``nav``, ``source`` or
``route``.

Moved from ``ViewsMixin`` / the shell. ``CONTENT_PAD`` travels with them
because it is a layout constant, not shell state.
"""

from ...mpvtk.widgets import Box, Busy, Column, Row, Spacer, Text
from .. import theme

#: Padding inside the content column. Lives here rather than on the browser
#: because every one of these functions needs it and none of them needs a
#: browser. Must stay equal to MpvtkBrowser.CONTENT_PAD until that alias is
#: retired -- tests/test_page_contract.py pins the two together.
CONTENT_PAD = 16


def busy():
    """The loading state: a spinner centred in the content area.

    Spacers rather than pad+align: the Row/Column nesting is what centres it
    on both axes, and it is copied verbatim from the original so the pixels
    do not move.
    """
    return Box(
        [Spacer(), Row([Spacer(), Busy(), Spacer()]), Spacer()],
        flex=1, direction="column", align="stretch",
    )


def error(msg):
    """A terminal message where content would be — a failed load, an empty
    search box. Not a dialog: the user is already looking here."""
    return Box([Text(msg, size=20, color=theme.SUBTLE_FG)],
               pad=24, flex=1, align="center", direction="row")


def body_width(w, pad=CONTENT_PAD):
    """Usable text width inside a padded, scrollable content column.

    The window width minus the content padding AND the scrollbar the
    scroll view reserves. Wrapping at ``w - 2*pad`` — the padding alone —
    makes every line 10px wider than the space it actually gets, so
    the tail of each line runs under the scrollbar, and which words land
    there changes with the window size. That is what made resizing look
    like the wrapping was unstable.
    """
    from ...mpvtk.layout import SCROLLBAR_W

    return max(120, w - 2 * pad - SCROLLBAR_W)


def paragraph(text, size, max_w, color=None):
    """Wrapped body text (overviews).

    The layout engine wraps *within* a paragraph, so blank-line breaks
    are handled here. The gap is a full line height: at anything less
    the paragraph break reads as tighter than the wrapped lines around
    it, which looks like a mistake rather than a break.
    """
    from ...mpvtk.layout import LINE_H

    paras = [p.strip() for p in (text or "").replace("\r", "").split("\n")
             if p.strip()]
    color = color or theme.TEXT_FG
    if len(paras) <= 1:
        return Text(paras[0] if paras else "", size=size, color=color,
                    wrap=True, w=max_w)
    return Column([Text(p, size=size, color=color, wrap=True, w=max_w)
                   for p in paras],
                  gap=round(size * LINE_H), w=max_w)
