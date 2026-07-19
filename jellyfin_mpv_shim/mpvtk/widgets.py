"""Declarative element tree for mpvtk.

Widgets are plain descriptions; nothing here talks to mpv. A tree is
turned into a flat paint-ordered scene by layout.layout(), pushed to the
in-mpv Lua renderer as JSON, and rebuilt from scratch on every render
(there is no retained widget state on the Python side — renderer-local
state like scroll offsets, textbox contents and dropdown selection
survives scene pushes keyed by element id).

Ids: elements get a stable tree-path id automatically. Stateful widgets
(scrolls, textboxes, dropdowns) should be given explicit ids so their
renderer-side state survives structural changes to the tree.
"""

from . import theme


class Element:
    """``anchor``/``dx``/``dy``/``occlude`` only apply to direct children
    of a :class:`Stack` (see its docstring); they are inert elsewhere.

    ``min_w``/``max_w``/``min_h``/``max_h`` bound the laid-out size: an
    int is pixels, a float in (0, 1] is a fraction of the available
    space (dialog children resolve fractions against the window). When
    a Box's fixed/natural children overflow it, they now shrink
    proportionally down to their min (bitmaps and icons floor at their
    natural size — pixels never squeeze)."""

    def __init__(self, id=None, w=None, h=None, flex=0,
                 anchor=None, dx=0, dy=0, occlude=False, tip=None,
                 min_w=None, max_w=None, min_h=None, max_h=None,
                 autofocus=False):
        self.id = id
        self.w = w
        self.h = h
        self.flex = flex
        self.anchor = anchor
        self.dx = dx
        self.dy = dy
        self.occlude = occlude
        self.tip = tip  # hover tooltip text (renderer-drawn, delayed)
        self.min_w = min_w
        self.max_w = max_w
        self.min_h = min_h
        self.max_h = max_h
        # Grabs spatial-nav focus when a key-summoned playback HUD's
        # first scene lands (renderer: phud want_focus). Inert outside
        # that flow — ordinary scenes never steal focus.
        self.autofocus = autofocus


class Box(Element):
    """Rectangular container; stacks children along ``direction``."""

    def __init__(
        self,
        children=None,
        direction="column",
        pad=0,  # uniform px, or (pad_x, pad_y)
        gap=0,
        align="start",  # cross-axis: start | center | end | stretch
        justify="start",  # main-axis: start | center | end | between
        bg=None,  # "rrggbb"
        alpha=255,
        radius=0,
        border=None,  # "rrggbb"
        border_w=1,
        on_click=None,
        on_dbl=None,  # double-click activation (fires after the clicks)
        hover=None,  # style overrides while hovered, e.g. {"fill": "334455"}
        repeat=False,  # hold-repeat: on_click refires while held down
        **kw,
    ):
        super().__init__(**kw)
        self.children = children or []
        self.direction = direction
        self.pad = pad
        self.gap = gap
        self.align = align
        self.justify = justify
        self.bg = bg
        self.alpha = alpha
        self.radius = radius
        self.border = border
        self.border_w = border_w
        self.on_click = on_click
        self.on_dbl = on_dbl
        self.hover = hover
        self.repeat = repeat


class Row(Box):
    def __init__(self, children=None, **kw):
        kw.setdefault("direction", "row")
        super().__init__(children, **kw)


class Column(Box):
    def __init__(self, children=None, **kw):
        kw.setdefault("direction", "column")
        super().__init__(children, **kw)


class Spacer(Element):
    """Flexible filler by default; a fixed-size stand-in (e.g. for
    virtualized content) when given an explicit w/h."""

    def __init__(self, flex=None, **kw):
        if flex is None:
            flex = 0 if ("w" in kw or "h" in kw) else 1
        super().__init__(flex=flex, **kw)


class Text(Element):
    """``wrap=True`` word-wraps to the laid-out width instead of
    ellipsizing, emitting one scene line per wrapped line (``\\n`` starts
    a new paragraph). ``max_lines`` caps the line count; the last kept
    line is ellipsized. A wrapped Text takes its width from the parent
    (give it ``w=`` or put it in a width-constrained column)."""

    def __init__(
        self,
        text,
        size=22,
        color="eeeeee",
        bold=False,
        align="left",
        on_click=None,
        hover=None,
        wrap=False,
        max_lines=None,
        **kw,
    ):
        super().__init__(**kw)
        self.text = text
        self.size = size
        self.color = color
        self.bold = bold
        self.align = align
        self.on_click = on_click
        self.hover = hover
        self.wrap = wrap
        self.max_lines = max_lines


class Image(Element):
    """A pre-rasterized BGRA image (see rawimage.write_bgra).

    ``src`` is the path to the raw file, ``iw``/``ih`` its pixel size.
    The display size is w/h; keep them equal to iw/ih (the renderer does
    not scale — pre-scale with Pillow when rasterizing). ``v`` is a
    content version: bump it when rewriting the same path in place so
    the renderer re-reads the file (content-keyed filenames don't need
    it).
    """

    def __init__(self, src, iw, ih, on_click=None, hover=None, v=0, **kw):
        kw.setdefault("w", iw)
        kw.setdefault("h", ih)
        super().__init__(**kw)
        self.src = src
        self.iw = iw
        self.ih = ih
        self.v = v
        self.on_click = on_click
        self.hover = hover


class ImageMap(Element):
    """A composited bitmap with interactive sub-regions.

    This is the scalable way to draw tile strips: Python bakes a whole
    row of posters — captions, progress bars, badges included — into
    ONE image (dodging both the 63-overlay budget and the
    bitmaps-above-ASS z-order), and declares the clickable tile areas
    as regions. Each region dict (image-local coords):

        {"id": ..., "x":, "y":, "w":, "h":,
         "on_click": fn, "on_context": fn, "hover": {"bc": ...}}

    Regions become transparent hit-rects whose hover ring draws OUTSIDE
    their bounds (the bitmap would cover an inline ring).
    """

    def __init__(self, src, iw, ih, regions=None, v=0, **kw):
        kw.setdefault("w", iw)
        kw.setdefault("h", ih)
        super().__init__(**kw)
        self.src = src
        self.iw = iw
        self.ih = ih
        self.v = v
        self.regions = regions or []


class Icon(Element):
    """Material vector icon (shared set with the Tk UI and the OSC),
    rendered as an ASS drawing — crisp at any size. Compose with Text
    in a Row for labelled buttons; Dropdown/Menu take per-item icons
    directly."""

    def __init__(self, name, size=20, color="eeeeee", on_click=None,
                 hover=None, **kw):
        kw.setdefault("w", size)
        kw.setdefault("h", size)
        super().__init__(**kw)
        self.name = name
        self.color = color
        self.on_click = on_click
        self.hover = hover


class Button(Box):
    """Labelled button, optionally with a leading Material icon.

    ``icon`` takes the same names as :class:`Icon`; it inherits the label's
    colour so accented/active buttons stay legible. An icon-only button is
    just ``label=""``."""

    def __init__(self, label, on_click=None, size=20, fg="eeeeee", icon=None,
                 icon_size=None, gap=None, flat=False, **kw):
        if flat:
            # transparent-at-rest, for controls over video/gradients
            # (playback HUD): no fill, a translucent hover wash
            kw.setdefault("bg", None)
            kw.setdefault("alpha", 70)
            kw.setdefault("hover", {"fill": "ffffff"})
        else:
            kw.setdefault("bg", "333333")
            kw.setdefault("hover", {"fill": "4a4a4a"})
        kw.setdefault("radius", 6)
        kw.setdefault("pad", 10)
        kw.setdefault("align", "center")
        kw.setdefault("direction", "row")
        children = []
        if icon:
            children.append(Icon(icon, icon_size or int(size * 0.95), color=fg))
            kw.setdefault("gap", gap if gap is not None else 7)
        if label:
            children.append(Text(label, size=size, color=fg, align="center"))
        super().__init__(children, on_click=on_click, **kw)


class TextBox(Element):
    def __init__(
        self,
        id,
        text="",
        placeholder="",
        size=20,
        mask=False,  # password entry: render bullets, value unchanged
        on_change=None,
        on_submit=None,
        force=False,  # override renderer-local edit state with ``text``
        **kw,
    ):
        kw.setdefault("w", 240)
        super().__init__(id=id, **kw)
        self.text = text
        self.placeholder = placeholder
        self.size = size
        self.mask = mask
        self.on_change = on_change
        self.on_submit = on_submit
        self.force = force


class Slider(Element):
    """Draggable value slider (volume, seek). on_change(value) fires
    throttled while dragging and once on release."""

    def __init__(
        self,
        id,
        value=0.0,
        min=0.0,
        max=100.0,
        on_change=None,
        force=False,
        **kw,
    ):
        kw.setdefault("w", 180)
        kw.setdefault("h", 28)
        super().__init__(id=id, **kw)
        self.value = value
        self.min = min
        self.max = max
        self.on_change = on_change
        self.force = force


class Busy(Element):
    """Indeterminate activity spinner (animated renderer-side)."""

    def __init__(self, **kw):
        kw.setdefault("w", 28)
        kw.setdefault("h", 28)
        super().__init__(**kw)


class Checkbox(Row):
    """Labelled toggle — pure composite sugar over Row/Box/Text."""

    def __init__(self, label, checked, on_toggle=None, size=20, **kw):
        box = Box(
            w=20,
            h=20,
            bg=theme.ACCENT if checked else "2a2a2a",
            border=None if checked else "555555",
            radius=5,
            align="center",
            direction="row",
            children=(
                [Text("✓", size=15, color=theme.ON_ACCENT, align="center",
                      flex=1)]
                if checked
                else []
            ),
        )
        kw.setdefault("gap", 10)
        kw.setdefault("align", "center")
        kw.setdefault("hover", {"c": "ffffff"})
        super().__init__(
            [box, Text(label, size=size)], on_click=on_toggle, **kw
        )


class Grid(Element):
    """Cells laid out on shared column tracks — the cure for sibling
    rows faking column alignment with magic fixed widths.

    ``cols``: list of track specs — ``{"w": px}`` fixed, ``{"flex": n}``
    share of leftover, ``{}`` auto (sized to the widest cell in that
    column). Optional ``"align"``: "left"/"center"/"right" (default
    left) positions cells inside their track.

    ``rows``: list of cell lists. A cell is an Element, a str (becomes
    a Text at ``size``/``fg``), or None (empty). Text cells and cells
    with ``flex>0`` stretch to the track width; other Elements keep
    their natural size, positioned by the track's align. Cells are
    vertically centered in their row (rows size to their tallest cell,
    or ``row_h`` if given).

    A row may also be a dict ``{"cells": [...], "id":, "bg":,
    "radius":, "hover":, "on_click":, "on_dbl":}`` — the styling/
    interaction draws as a full-width row rect behind the cells (list
    rows with card backgrounds, à la the servers/downloads panels).
    ``row_pad`` insets cells from the row rect on every side.
    """

    def __init__(self, rows, cols, gap=12, row_gap=8, row_h=None,
                 row_pad=0, size=18, fg="eeeeee", **kw):
        super().__init__(**kw)
        self.rows = rows
        self.cols = cols
        self.gap = gap
        self.row_gap = row_gap
        self.row_h = row_h
        self.row_pad = row_pad
        self.size = size
        self.fg = fg


class Form(Grid):
    """Label + input rows on shared tracks: the label column sizes to
    the widest label, the value column flexes. ``rows`` is a list of
    ``(label, element)`` pairs (label may be a str or an Element; a
    None element leaves the row's value cell empty)."""

    def __init__(self, rows, label_w=None, size=18,
                 label_fg="9a9a9a", **kw):
        cols = [
            {"w": label_w} if label_w else {},
            {"flex": 1},
        ]
        grid_rows = [
            [Text(l, size=size, color=label_fg)
             if isinstance(l, str) else l, v]
            for l, v in rows
        ]
        super().__init__(grid_rows, cols, size=size, **kw)


class Gradient(Element):
    """A vertical fade (ASS, so ordinary ASS content still draws on
    top — a bitmap gradient would cover everything). Drawn as one
    solid box with a gaussian-blurred fading edge, not stacked alpha
    bands (those show visible banding — the lua OSC's gradient learned
    this the hard way). The playback HUD's bottom scrim:
    ``Gradient(color="000000", top=0, bottom=200)`` fades from
    transparent at the top edge to mostly-opaque at the bottom.
    Opacities are 0–255. Non-interactive."""

    def __init__(self, color="000000", top=0, bottom=200, **kw):
        super().__init__(**kw)
        self.color = color
        self.top = top
        self.bottom = bottom


class Progress(Element):
    """Determinate progress bar (composite drawn by layout as two
    rects). ``frac`` in [0, 1]; give it a width or ``flex``."""

    def __init__(self, frac, fg=None, bg="2a2a2a", **kw):
        kw.setdefault("w", 180)
        kw.setdefault("h", 8)
        super().__init__(**kw)
        self.frac = min(1.0, max(0.0, frac))
        self.fg = fg or theme.ACCENT
        self.bg = bg


class Stack(Element):
    """Children share this element's rect; later children paint above
    earlier ones and everything scrolls with the page (unlike Float,
    which is screen-absolute). Per-child placement comes from the
    child's own ``anchor`` ("nw" "n" "ne" "w" "c" "e" "sw" "s" "se", or
    None to fill the whole rect) plus ``dx``/``dy`` pixel offsets.

    Z-order caveats (GUIDE §6): a bitmap child over a bitmap sibling
    works (the renderer keeps overlay slots in paint order). An
    ASS-drawn child (Text/Icon/Box) can NOT composite over an Image
    sibling directly — mark it ``occlude=True`` and its rect is
    subtracted from earlier image siblings, so it draws in the hole
    (give it an opaque bg; whatever the hole reveals is the window
    background). Without ``occlude`` the image wins.
    """

    def __init__(self, children=None, **kw):
        super().__init__(**kw)
        self.children = children or []


class Table(Column):
    """Header + body rows generated from ONE column spec, so header and
    cell geometry can never drift apart.

    ``columns``: list of dicts — ``{"label": str, "w": px}`` or
    ``{"label": str, "flex": n}``, optional ``"align"``
    ("left"/"center"/"right", default left).

    ``rows``: list of dicts —
    ``{"cells": [str | Element, ...], "id": optional, "selected": bool,
    "fg": row text color, "bg": row background (selected wins),
    "on_click": fn, "on_dbl": fn}``. ``on_click`` may declare one
    required parameter to receive the click modifier dict
    ``{"shift": bool, "ctrl": bool}`` for range/additive selection (see
    MpvtkApp click dispatch); zero-arg callables keep the bare call.
    ``on_dbl`` fires on double-click, after the two normal clicks.

    ``virtual``: optional ``{"offset": px, "height": px, "overscan":
    rows}`` — materialize only the rows intersecting the viewport
    (``offset`` from ``MpvtkApp.scroll_offsets()``), replacing the rest
    with two exact-height spacers. Give rows stable ``id``s so
    renderer-side state survives the window moving.
    """

    def __init__(
        self,
        columns,
        rows,
        row_h=36,
        header_h=30,
        size=18,
        header_size=15,
        header_fg="9a9a9a",
        fg="eeeeee",
        selected_bg=None,
        hover_bg="333333",
        gap=12,
        pad_x=10,
        virtual=None,
        **kw,
    ):
        selected_bg = selected_bg or theme.SOFT

        def cell(col, content, text_size, color):
            if isinstance(content, Element):
                inner = content
            else:
                inner = Text(
                    str(content),
                    size=text_size,
                    color=color,
                    align=col.get("align", "left"),
                    flex=1,
                )
            return Box(
                [inner],
                direction="row",
                align="center",
                w=col.get("w"),
                flex=col.get("flex", 0) if col.get("w") is None else 0,
            )

        def margin():
            return Spacer(w=pad_x, h=1)

        header = Row(
            [margin()]
            + [cell(c, c.get("label", ""), header_size, header_fg)
               for c in columns]
            + [margin()],
            h=header_h,
            gap=gap,
            align="stretch",
        )
        first, last = 0, len(rows)
        lead_h = tail_h = 0
        if virtual is not None and rows:
            over = virtual.get("overscan", 2)
            off = max(0.0, float(virtual.get("offset", 0)))
            view = float(virtual.get("height", 0))
            first = max(0, int(off // row_h) - over)
            last = min(len(rows), int((off + view) // row_h) + 1 + over)
            lead_h = first * row_h
            tail_h = (len(rows) - last) * row_h
        body = []
        if lead_h:
            body.append(Spacer(h=lead_h))
        for i in range(first, last):
            row = rows[i]
            row_fg = row.get("fg", fg)
            body.append(
                Row(
                    [margin()]
                    + [cell(c, v, size, row_fg)
                       for c, v in zip(columns, row.get("cells", []))]
                    + [margin()],
                    id=row.get("id"),
                    h=row_h,
                    gap=gap,
                    align="stretch",
                    bg=(selected_bg if row.get("selected")
                        else row.get("bg")),
                    hover={"fill": hover_bg} if row.get("on_click") else None,
                    on_click=row.get("on_click"),
                    on_dbl=row.get("on_dbl"),
                    radius=4,
                )
            )
        if tail_h:
            body.append(Spacer(h=tail_h))
        # rows must all stretch to the table width or flex columns
        # would re-distribute per-row and drift against the header
        kw.setdefault("align", "stretch")
        super().__init__([header] + body, **kw)
        if (virtual is not None and self.w is None and not self.flex
                and self.min_w is None):
            # A virtualized table's built rows depend on the scroll
            # offset, so its measured natural width would jitter with
            # scrolling — a trap for any non-stretch parent. Pin min_w
            # to the widest content across ALL rows (str cells only;
            # Element cells are fixed-width in practice).
            from .layout import text_width

            total = 2 * pad_x + gap * (len(columns) + 1)
            for ci, col in enumerate(columns):
                if col.get("w") is not None:
                    total += col["w"]
                    continue
                mx = text_width(str(col.get("label", "")), header_size)
                for r in rows:
                    cs = r.get("cells", [])
                    if ci < len(cs) and not isinstance(cs[ci], Element):
                        mx = max(mx, text_width(str(cs[ci]), size))
                total += mx
            self.min_w = total


class Float(Element):
    """Absolutely-positioned top-layer container (toasts, banners).
    Drawn above everything and occluding image overlays; does NOT grab
    input — content stays clickable underneath elsewhere."""

    def __init__(self, child, x, y, **kw):
        super().__init__(**kw)
        self.child = child
        self.x = x
        self.y = y


class Dialog(Element):
    """Modal dialog: centered floating container that grabs all input.
    Clicks outside it and ESC emit on_dismiss(); the app closes it by
    re-rendering without the Dialog. No dimmed backdrop (bitmaps render
    above ASS, so a scrim cannot cover posters — see README z-order)."""

    def __init__(self, id, child, on_dismiss=None, **kw):
        super().__init__(id=id, **kw)
        self.child = child
        self.on_dismiss = on_dismiss


class Dropdown(Element):
    """``trigger_icon`` replaces the boxed control with a bare Material
    icon (translucent hover wash, no border/arrow/label) — the playback
    HUD's track pickers open their popup from a transparent icon
    button. The popup then sizes to its items (not the trigger) and
    clamps to the screen edges."""

    def __init__(
        self,
        id,
        items,
        selected=0,
        size=20,
        icons=None,  # optional per-item Material icon names (None ok)
        on_select=None,
        force=False,
        trigger_icon=None,
        **kw,
    ):
        if trigger_icon:
            kw.setdefault("w", int(size * 1.9))
            kw.setdefault("h", int(size * 1.9))
        super().__init__(id=id, **kw)
        self.items = list(items)
        self.selected = selected
        self.size = size
        self.icons = icons
        self.on_select = on_select
        self.force = force
        self.trigger_icon = trigger_icon


class Menu(Element):
    """Floating context menu at absolute position (x, y) — out of flow.

    Presence in the tree = open. The renderer reports item choice via
    on_select(index, value) and click-away/ESC via on_dismiss(); the app
    responds by re-rendering without the Menu (the renderer hides it
    instantly on its own for responsiveness).
    """

    def __init__(
        self,
        id,
        items,
        x,
        y,
        size=20,
        icons=None,  # optional per-item Material icon names (None ok)
        on_select=None,
        on_dismiss=None,
        **kw,
    ):
        super().__init__(id=id, **kw)
        self.items = list(items)
        self.x = x
        self.y = y
        self.size = size
        self.icons = icons
        self.on_select = on_select
        self.on_dismiss = on_dismiss


class Scroll(Element):
    """``on_scroll(offset, max)`` fires debounced from the renderer when
    the user scrolls — the hook for windowed/infinite content."""

    def __init__(self, child, axis, scrollbar=False, on_scroll=None, **kw):
        super().__init__(**kw)
        self.child = child
        self.axis = axis
        self.scrollbar = scrollbar
        self.on_scroll = on_scroll


class HScroll(Scroll):
    def __init__(self, child, **kw):
        super().__init__(child, "x", **kw)


class VScroll(Scroll):
    def __init__(self, child, scrollbar=True, **kw):
        super().__init__(child, "y", scrollbar=scrollbar, **kw)
