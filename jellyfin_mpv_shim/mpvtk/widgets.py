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
    def __init__(self, id=None, w=None, h=None, flex=0):
        self.id = id
        self.w = w
        self.h = h
        self.flex = flex


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
    def __init__(
        self,
        text,
        size=22,
        color="eeeeee",
        bold=False,
        align="left",
        on_click=None,
        hover=None,
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
    def __init__(self, label, on_click=None, size=20, fg="eeeeee", **kw):
        kw.setdefault("bg", "333333")
        kw.setdefault("hover", {"fill": "4a4a4a"})
        kw.setdefault("radius", 6)
        kw.setdefault("pad", 10)
        kw.setdefault("align", "center")
        kw.setdefault("direction", "row")
        super().__init__(
            [Text(label, size=size, color=fg, align="center")],
            on_click=on_click,
            **kw,
        )


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
