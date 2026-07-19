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


class Element:
    """``anchor``/``dx``/``dy``/``occlude`` only apply to direct children
    of a :class:`Stack` (see its docstring); they are inert elsewhere."""

    def __init__(self, id=None, w=None, h=None, flex=0,
                 anchor=None, dx=0, dy=0, occlude=False):
        self.id = id
        self.w = w
        self.h = h
        self.flex = flex
        self.anchor = anchor
        self.dx = dx
        self.dy = dy
        self.occlude = occlude


class Box(Element):
    """Rectangular container; stacks children along ``direction``."""

    def __init__(
        self,
        children=None,
        direction="column",
        pad=0,
        gap=0,
        align="start",  # cross-axis: start | center | end | stretch
        bg=None,  # "rrggbb"
        alpha=255,
        radius=0,
        border=None,  # "rrggbb"
        border_w=1,
        on_click=None,
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
        self.bg = bg
        self.alpha = alpha
        self.radius = radius
        self.border = border
        self.border_w = border_w
        self.on_click = on_click
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
                 icon_size=None, gap=None, **kw):
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
            bg="7aa2f7" if checked else "2a2a2a",
            border=None if checked else "555555",
            radius=5,
            align="center",
            direction="row",
            children=(
                [Text("✓", size=15, color="101010", align="center", flex=1)]
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
    "on_click": fn}``. ``on_click`` may declare one required parameter
    to receive the click modifier dict ``{"shift": bool, "ctrl": bool}``
    for range/additive selection (see MpvtkApp click dispatch);
    zero-arg callables keep the bare call.
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
        selected_bg="2f4468",
        hover_bg="333333",
        gap=12,
        pad_x=10,
        **kw,
    ):
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
        body = []
        for i, row in enumerate(rows):
            body.append(
                Row(
                    [margin()]
                    + [cell(c, v, size, fg)
                       for c, v in zip(columns, row.get("cells", []))]
                    + [margin()],
                    id=row.get("id"),
                    h=row_h,
                    gap=gap,
                    align="stretch",
                    bg=selected_bg if row.get("selected") else None,
                    hover={"fill": hover_bg} if row.get("on_click") else None,
                    on_click=row.get("on_click"),
                    radius=4,
                )
            )
        super().__init__([header] + body, **kw)


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
    def __init__(
        self,
        id,
        items,
        selected=0,
        size=20,
        icons=None,  # optional per-item Material icon names (None ok)
        on_select=None,
        force=False,
        **kw,
    ):
        super().__init__(id=id, **kw)
        self.items = list(items)
        self.selected = selected
        self.size = size
        self.icons = icons
        self.on_select = on_select
        self.force = force


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
