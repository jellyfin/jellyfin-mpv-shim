"""Content-area primitives: the busy and error states, body text, widths.

Pure functions — data in, widget tree out — so every page can reach them
through ``PageContext`` instead of the shell. See
``docs/archive/ARCHITECTURE_TARGET.md`` §1.4 for the test being applied: a component
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
    search box. Not a dialog: the user is already looking here.

    Top-aligned, because it stands in for *content* and content starts at
    the top. ``direction="row"`` makes ``align`` the vertical axis, so this
    used to read ``align="center"`` and floated the line in the middle of
    whatever space was left — under the Live TV tab bar that put "Nothing is
    scheduled to record." halfway down an otherwise empty screen, attached
    to nothing. ``flex=1`` stays: the message still owns the content area,
    it just no longer hovers in it.
    """
    return Box([Text(msg, size="large", color=theme.SUBTLE_FG)],
               pad=24, flex=1, align="start", direction="row")


def wrap_row(items, avail, gap=8, align="center", row_gap=None):
    """``items`` in a Row, broken onto further rows when one will not fit.

    A Row does not wrap: it lays its children out end to end and lets the
    tail run off the window. That is invisible at 1x on a wide window and
    routine at 200%, where a 1280px window is a 640px page (see
    ``mpvtk/scaling.py`` -- view code is logical, so the UI scale is a width
    problem). It is equally reachable at 100% by making the window small,
    and by translation: "Servers & Users" is "Server und Benutzer" in German.

    Measured rather than switched on a width constant, for the same reason
    ``GridPage._fit_bar`` is: what fits depends on these particular items.

    Returns the plain Row when everything fits on one, so the common case
    produces the same tree it always did.
    """
    from ...mpvtk.layout import measure

    one_row = Row(items, gap=gap, align=align)
    if not items or avail <= 0:
        return one_row
    # A flexible Spacer is "push these apart", which is what pins a trailing
    # group to the right edge -- and so is what puts those particular buttons
    # off the window. It has nothing left to say once the row is full, so it
    # is dropped on the way to wrapping (and only then: a row that fits keeps
    # the tree it always had).
    packable = [i for i in items
                if not (isinstance(i, Spacer) and getattr(i, "flex", 0))]
    try:
        widths = [measure(i)[0] for i in packable]
    except Exception:
        # Never fail a render over a decoration: an unmeasurable item keeps
        # the old behaviour, which is one row that may be too wide.
        return one_row
    rows, cur, used = [], [], 0.0
    for item, iw in zip(packable, widths):
        need = iw if not cur else used + gap + iw
        if cur and need > avail:
            rows.append(cur)
            cur, used = [item], iw
        else:
            cur.append(item)
            used = need
    rows.append(cur)
    if len(rows) == 1:
        return one_row
    return Column([Row(r, gap=gap, align=align) for r in rows],
                  gap=gap if row_gap is None else row_gap, align="start")


def header_body(banner, blocks, pad=CONTENT_PAD, gap=16, full_bleed=False):
    """The scrollable column of a page that opens with a backdrop header.

    ``Column([banner] + blocks, pad=pad, gap=gap)`` -- the padded shape all
    three detail-ish pages used to build by hand -- unless ``full_bleed``,
    in which case the banner comes OUT of the padding and the rest of the
    page keeps it.

    The nesting is what makes that possible: a single column has one padding
    for every child. So the banner and an inner, horizontally-padded column
    become the two children of an unpadded outer one. ``align="stretch"``
    there is for the inner column, which has no width of its own; the banner
    is an Image with an explicit ``w`` and layout leaves a fixed cross size
    alone, so it keeps exactly the width ``banner_box`` gave it.

    The trailing Spacer is the bottom padding the outer column's ``pad=0``
    gives up. There is no top padding to replace, which is the point.

    ``pad`` may be an ``(x, y)`` pair, which is what a page whose body is a
    GRID passes: the grid's own horizontal padding comes from
    ``grid_layout`` and is not CONTENT_PAD (see SeasonPage). Only the y half
    is the one full bleed gives up.
    """
    px, py = pad if isinstance(pad, tuple) else (pad, pad)
    if not full_bleed:
        return Column([banner] + list(blocks), pad=(px, py), gap=gap)
    body = Column(list(blocks) + [Spacer(h=py)], pad=(px, 0), gap=gap)
    return Column([banner, body], gap=gap, align="stretch")


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
