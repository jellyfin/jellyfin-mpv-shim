"""The guide grid: channels down, time across.

A component in the ``components/`` sense (render resources and callbacks, no
``nav``/``source``/``route``) but its own module rather than a file in there,
because nothing else draws anything like it — putting a 200-line grid next to
``busy()`` and ``action_btn()`` would swamp them.

**Why the window is paged rather than scrolled.** jellyfin-web renders a full
24-hour grid and scrolls it horizontally, with the channel column and the
time header scroll-synced to it by hand. Two things make that the wrong shape
here: mpvtk's scroll containers are owned by the renderer (Python is told
about a scroll after the fact, debounced), so keeping three of them in step
would mean a Python round-trip in the wheel path and visible tearing between
the header and the grid; and this UI is driven as often by a remote as by a
mouse, where "page forward two hours" is a better verb than "scroll right".

So the grid draws one window — two hours by default, fewer on a narrow
surface — and the page moves it. Everything else the guide does (date
selection, category filtering, indicators, colour coding, click-to-watch on
what is airing) is unchanged.
"""

from ..i18n import _
from ..mpvtk.widgets import Box, Column, Icon, Row, Spacer, Text, VScroll
from . import live_tv, theme
from .components import chrome

#: Width of the fixed channel column.
#:
#: 30% wider than it was, because it was ellipsizing names it had room for:
#: at 168 the label got 114px after the logo and the padding, and anything
#: past about "Channel 4 +1" was cut — "Sky Sports Main Event" needs 142.
#: It costs the grid nothing. The window is a whole number of 30-minute
#: cells capped at MAX_CELLS, so what the grid does with the extra 50px is
#: make each cell 5-7px narrower, not drop one: the count is unchanged from
#: 800px up (see live_tv.cells_for_width, floor MIN_CELL_W = 120).
CHANNEL_W = 218

#: Height of one channel row, and the gap between rows/cells.
ROW_H = 62
GAP = 3

#: Height of the time-slot header.
HEADER_H = 26


def grid_width(size):
    """Px available to the 30-minute columns in a content area of ``size``.

    Exported because the page decides how many columns to draw and this
    module decides how wide they are, and the two subtracting slightly
    different things (one forgot the inter-column gap) is how a window ends
    up with one more column than fits.
    """
    return max(120, max(200, size[0] - 2 * chrome.CONTENT_PAD)
               - CHANNEL_W - GAP)

#: Channel-logo edge. Small enough that a screenful of rows stays well inside
#: mpv's 63-overlay budget (each logo is one overlay), large enough to
#: recognise. Rows outside the virtual window draw a placeholder instead and
#: composite nothing at all.
LOGO = 34

#: Narrowest cell that gets a second line of text. Below this the programme
#: name alone barely fits, and a truncated time under a truncated title reads
#: as noise.
SUBLINE_MIN_W = 150

#: Narrowest cell that gets any text at all. A five-minute filler in a
#: two-hour window is ~10px wide; an ellipsis in it is worse than nothing.
TEXT_MIN_W = 34


def _category_bg(name):
    """The wash behind a colour-coded cell.

    Derived from the palette rather than hardcoded so a theme's guide matches
    the rest of it — a fixed green on a light theme is unreadable.
    """
    base = theme.CARD_BG
    return {
        "kids": theme.mix(base, theme.ACCENT_SOFT, 0.55),
        "sports": theme.mix(base, theme.OK_GREEN, 0.35),
        "news": theme.mix(base, theme.FAV_RED, 0.30),
        "movie": theme.mix(base, theme.WARN_AMBER, 0.30),
    }.get(name)


def _slot(child, width):
    """Give ``child`` a gutter without moving it.

    The cells' widths come from ``row_segments`` and sum to *exactly* the
    grid width; a ``gap`` on the Row would add to that and slide every cell
    right of the first out from under its time-header column. So each cell
    occupies its full computed width and paints a slightly narrower box
    inside it — the gutter comes out of the cell, not out of the layout.
    """
    return Box([child], w=width, h=ROW_H, direction="row")


def _cell(program, width, prefs, airing, on_click, categories=(),
          on_context=None):
    """One programme in a channel row."""
    inner_w = max(1, width - GAP)
    if program is None:
        # Dead air. Drawn as a recessed box rather than skipped, so the row
        # still spans the window and the cells after it stay aligned with
        # the time header.
        return _slot(Box(w=inner_w, h=ROW_H,
                         bg=theme.mix(theme.WINDOW_BG, theme.CARD_BG, 0.4),
                         radius=4), width)
    # Filtered out by the category selection: the cell stays — with its
    # size, its place in the row and its click — but says nothing. That is
    # jellyfin-web's ``displayInnerContent``, and it is what keeps a
    # filtered guide legible as a grid: dropping the programmes instead
    # leaves a row of gaps with no way to tell a filtered showing from a
    # hole in the listings.
    shown = live_tv.program_displayed(program, categories)
    bg = theme.CARD_BG
    if prefs.get("color_coded") and shown:
        bg = _category_bg(live_tv.program_category(program)) or bg
    if airing and shown:
        # What is on right now is the one cell you look for, so it gets the
        # accent wash — not the category colour, which would hide it.
        bg = theme.ACCENT_SOFT
    children = []
    if inner_w >= TEXT_MIN_W and shown:
        state = live_tv.timer_state(program)
        title_bits = []
        if state:
            title_bits.append(Icon(
                live_tv.STATE_ICONS[state], 13,
                color=(theme.SUBTLE_FG if state == "series_inactive"
                       else theme.FAV_RED)))
        if prefs.get("indicators", {}).get("hd") and program.get("IsHD"):
            title_bits.append(Text(_("HD"), size="micro", color=theme.SUBTLE_FG))
        title_bits.append(Text(live_tv.program_title(program), size="small",
                               bold=airing))
        children.append(Row(title_bits, gap=4, align="center"))
        if inner_w >= SUBLINE_MIN_W:
            badge = live_tv.program_indicators(program,
                                               prefs.get("indicators"))
            sub = live_tv.program_subtitle(program)
            line = [Text(live_tv.air_time_label(program, end=False), size="micro",
                         color=theme.SUBTLE_FG)]
            if badge:
                line.append(Text(badge, size="micro", color=theme.ACCENT))
            if sub:
                line.append(Text(sub, size="micro", color=theme.SUBTLE_FG))
            children.append(Row(line, gap=6, align="center"))
    return _slot(
        Box(children, w=inner_w, h=ROW_H, bg=bg, radius=4, pad=(8, 6),
            direction="column", justify="center",
            hover={"fill": theme.BUTTON_ACTIVE},
            on_click=(lambda p=program: on_click(p)),
            # The same context menu a tile has, so a recording can be set
            # from the listing rather than only from the programme's own
            # page — which is what the guide is for, and what jellyfin-web's
            # guide offers.
            on_context=(None if on_context is None
                        else (lambda x, y, p=program: on_context(p, x, y)))),
        width)


def _channel_cell(channel, tiles, on_channel=None, on_context=None):
    """The fixed left column: logo, number, name.

    Clickable, like jellyfin-web's ``guide-channelHeaderCell`` — which is a
    button with ``data-action="link"``, i.e. it opens the channel rather than
    tuning to it. Same destination the channel grid's tiles have.

    Only ever called for a row inside the virtual window. That is not an
    optimisation: an art cell composites a bitmap into the strip cache as it
    is *built*, so building one per channel of a 900-channel line-up evicts
    the rows actually on screen — the trap ``track_list`` documents.
    """
    art = tiles.art_cell(channel, size=LOGO)
    number = live_tv.channel_number(channel)
    label = [Text(channel.get("Name") or "", size="caption")]
    if number:
        label.insert(0, Text(number, size="micro", color=theme.SUBTLE_FG))
    return Row([art, Column(label, gap=1)], w=CHANNEL_W, h=ROW_H, gap=8,
               pad=(6, 6), align="center",
               id=("guide-ch-" + str(channel.get("Id") or "")
                   if on_channel else None),
               radius=4,
               hover={"fill": theme.BUTTON_BG} if on_channel else None,
               on_click=(None if on_channel is None
                         else (lambda c=channel: on_channel(c))),
               on_context=(None if on_context is None
                           else (lambda x, y, c=channel: on_context(c, x, y))))


def time_header(start, end, width):
    """The time-slot row above the grid, aligned with the cells below it.

    Built from the same ``row_segments`` maths as a channel row (one
    pseudo-programme per slot) so the two cannot drift apart: laying the
    header out by dividing the width evenly and the cells out by clipping
    against the window is exactly how a guide ends up with the 20:30 label
    over the 20:00 column.
    """
    slots = live_tv.timeslots(start, end)
    if not slots:
        return Spacer(h=HEADER_H)
    cells = []
    for i, slot in enumerate(slots):
        left = int(round(width * i / len(slots)))
        right = int(round(width * (i + 1) / len(slots)))
        cells.append(Box([Text(live_tv.fmt_time(slot), size="caption",
                               color=theme.SUBTLE_FG)],
                         w=right - left, h=HEADER_H, direction="row",
                         align="center"))
    # No gap, for the same reason the cells have none: these widths sum to
    # exactly ``width`` and the columns below are laid out against them.
    return Row(cells, h=HEADER_H)


def guide_grid(channels, programs_by_channel, start, end, size, prefs, tiles,
               scroll, scroll_id, on_program, offset=None, on_scroll=None,
               categories=(), on_context=None, on_channel=None):
    """The whole grid: time header plus one row per channel.

    ``programs_by_channel`` maps channel id -> that channel's programmes;
    grouping is the caller's because it also holds the raw fetch.

    ``categories`` is the session's category filter. It reaches the cells
    rather than the fetch on purpose — see ``_cell`` and
    ``LibrarySource.get_guide``.

    ``size`` is the content area. Rows are virtualized against ``scroll``:
    a 900-channel line-up is 900 rows, and compositing a logo for each would
    blow both the strip cache and mpv's overlay budget long before the user
    scrolled to any of them.
    """
    grid_w = grid_width(size)
    header = Row([Spacer(w=CHANNEL_W + GAP), time_header(start, end, grid_w)])

    # The columns are sized for a padded width, so the Column has to carry
    # that padding: laid out from x=0 the whole grid sat flush against the
    # left edge — out of line with the date controls above it — and stopped
    # two pads short of the right.
    pad = (chrome.CONTENT_PAD, 0)

    if not channels:
        return Column([header, chrome.error(_("No channels available."))],
                      gap=GAP, flex=1, align="stretch", pad=pad)

    # Which rows may composite artwork this frame. Same window arithmetic
    # grid_of uses: a screen either side of the viewport, so a scroll lands
    # on rows that are already drawn.
    pitch = ROW_H + GAP
    view_h = max(240.0, float(size[1]))
    top = max(0.0, scroll.offset(scroll_id))
    first = int(max(0.0, top - view_h) // pitch)
    last = int((top + 2 * view_h) // pitch)

    rows = []
    for index, channel in enumerate(channels):
        if not (first <= index <= last):
            rows.append(Spacer(h=ROW_H))
            continue
        programs = programs_by_channel.get(channel.get("Id")) or []
        segments = live_tv.row_segments(programs, start, end, grid_w)
        cells = [_channel_cell(channel, tiles, on_channel, on_context),
                 Spacer(w=GAP)]
        for program, seg_w in segments:
            if seg_w <= 0:
                continue
            cells.append(_cell(program, seg_w, prefs,
                               program is not None
                               and live_tv.is_airing(program),
                               on_program, categories, on_context))
        rows.append(Row(cells, h=ROW_H))
    return Column([
        header,
        VScroll(Column(rows, gap=GAP), id=scroll_id, flex=1,
                offset=offset, snap=pitch, on_scroll=on_scroll),
    ], gap=GAP, flex=1, align="stretch", pad=pad)
