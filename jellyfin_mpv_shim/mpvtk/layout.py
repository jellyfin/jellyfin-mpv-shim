"""Layout engine: element tree -> flat scene node list.

Coordinates are OSD pixels, origin top-left. Children of a scroll
container are laid out in content space as if the offset were 0; the
renderer subtracts the live scroll offset and clips to the viewport.

Text measurement is an approximation (per-char advance as a fraction of
the font size); the same table lives in renderer.lua for cursor
positioning — keep them in sync. Real glyph metrics are a known
limitation of the spike.
"""

from .widgets import (
    Box,
    Dropdown,
    Element,
    Image,
    Scroll,
    Text,
    TextBox,
)

# Keep in sync with CHAR_W in renderer.lua.
_NARROW = set("iIljtfr.,:;!|'`()[]\"")
_WIDE = set("mwMW@%&")
_SPACE_W = 0.30
_NARROW_W = 0.34
_WIDE_W = 0.85
_DEFAULT_W = 0.54

LINE_H = 1.25  # text node height as a multiple of font size


def char_w(ch):
    if ch == " ":
        return _SPACE_W
    if ch in _NARROW:
        return _NARROW_W
    if ch in _WIDE:
        return _WIDE_W
    return _DEFAULT_W


def text_width(s, size, bold=False):
    w = sum(char_w(c) for c in s) * size
    return w * 1.04 if bold else w


def ellipsize(s, size, bold, max_w):
    if text_width(s, size, bold) <= max_w:
        return s
    ell = text_width("…", size, bold)
    out = []
    w = 0.0
    for c in s:
        cw = char_w(c) * size * (1.04 if bold else 1.0)
        if w + cw + ell > max_w:
            break
        out.append(c)
        w += cw
    return "".join(out) + "…"


SCROLLBAR_W = 10


def measure(el):
    """Natural (width, height) of an element, ignoring flex."""
    if isinstance(el, Text):
        return (
            el.w if el.w is not None else text_width(el.text, el.size, el.bold),
            el.h if el.h is not None else el.size * LINE_H,
        )
    if isinstance(el, Image):
        return el.w, el.h
    if isinstance(el, TextBox):
        return el.w or 240, el.h or el.size * 1.9
    if isinstance(el, Dropdown):
        if el.w is None:
            widest = max(
                (text_width(i, el.size) for i in el.items), default=40
            )
            w = widest + 44
        else:
            w = el.w
        return w, el.h or el.size * 1.9
    if isinstance(el, Scroll):
        cw, ch = measure(el.child)
        return el.w if el.w is not None else cw, (
            el.h if el.h is not None else ch
        )
    if isinstance(el, Box):
        row = el.direction == "row"
        main = 0.0
        cross = 0.0
        for c in el.children:
            cw, ch = measure(c)
            cm, cc = (cw, ch) if row else (ch, cw)
            # fixed size wins over natural
            fixed = c.w if row else c.h
            if fixed is not None:
                cm = fixed
            main += cm
            cross = max(cross, cc)
        if el.children:
            main += el.gap * (len(el.children) - 1)
        main += 2 * el.pad
        cross += 2 * el.pad
        w, h = (main, cross) if row else (cross, main)
        return el.w if el.w is not None else w, (
            el.h if el.h is not None else h
        )
    return el.w or 0, el.h or 0


class _Ctx:
    def __init__(self):
        self.nodes = []
        self.handlers = {}


def layout(root, w, h):
    """Returns (nodes, handlers): flat paint-ordered scene nodes and an
    ``{id: {event: callback}}`` registry for the pushed tree."""
    ctx = _Ctx()
    _arrange(ctx, root, 0.0, 0.0, float(w), float(h), None, "r")
    seen = set()
    for node in ctx.nodes:
        if node["id"] in seen:
            import logging

            logging.getLogger("mpvtk").warning(
                "duplicate node id %r: renderer state and events will "
                "target only the last occurrence",
                node["id"],
            )
        seen.add(node["id"])
    return ctx.nodes, ctx.handlers


def _reg(ctx, id, event, fn):
    if fn is not None:
        ctx.handlers.setdefault(id, {})[event] = fn


def _round(v):
    return round(v, 1)


def _base(el, t, x, y, w, h, sc, path):
    node = {
        "t": t,
        "id": el.id or path,
        "x": _round(x),
        "y": _round(y),
        "w": _round(w),
        "h": _round(h),
    }
    if sc:
        node["sc"] = sc
    return node


def _arrange(ctx, el, x, y, w, h, sc, path):
    if isinstance(el, Text):
        node = _base(el, "text", x, y, w, h, sc, path)
        node["text"] = ellipsize(el.text, el.size, el.bold, w)
        node["size"] = el.size
        node["c"] = el.color
        node["align"] = el.align
        if el.bold:
            node["bold"] = True
        if el.on_click:
            node["click"] = True
            _reg(ctx, node["id"], "click", el.on_click)
        if el.hover:
            node["hover"] = el.hover
        ctx.nodes.append(node)
        return

    if isinstance(el, Image):
        node = _base(el, "img", x, y, w, h, sc, path)
        node["src"] = el.src
        node["iw"] = el.iw
        node["ih"] = el.ih
        if el.on_click:
            node["click"] = True
            _reg(ctx, node["id"], "click", el.on_click)
        if el.hover:
            node["hover"] = el.hover
        ctx.nodes.append(node)
        return

    if isinstance(el, TextBox):
        node = _base(el, "textbox", x, y, w, h, sc, path)
        node["text"] = el.text
        node["ph"] = el.placeholder
        node["size"] = el.size
        if el.force:
            node["force"] = True
        _reg(ctx, node["id"], "change", el.on_change)
        _reg(ctx, node["id"], "submit", el.on_submit)
        ctx.nodes.append(node)
        return

    if isinstance(el, Dropdown):
        node = _base(el, "dropdown", x, y, w, h, sc, path)
        node["items"] = el.items
        node["sel"] = el.selected
        node["size"] = el.size
        if el.force:
            node["force"] = True
        _reg(ctx, node["id"], "select", el.on_select)
        ctx.nodes.append(node)
        return

    if isinstance(el, Scroll):
        node = _base(el, "scroll", x, y, w, h, sc, path)
        node["axis"] = el.axis
        inner_w, inner_h = w, h
        if el.scrollbar and el.axis == "y":
            inner_w -= SCROLLBAR_W
            node["bar"] = True
        cw, ch = measure(el.child)
        if el.axis == "x":
            cw, ch = max(cw, inner_w), inner_h
        else:
            cw, ch = inner_w, max(ch, inner_h)
        node["cw"] = _round(cw)
        node["ch"] = _round(ch)
        ctx.nodes.append(node)
        _arrange(ctx, el.child, x, y, cw, ch, node["id"], path + ".0")
        return

    if isinstance(el, Box):
        if el.bg or el.border or el.on_click:
            node = _base(el, "rect", x, y, w, h, sc, path)
            if el.bg:
                node["fill"] = el.bg
            if el.alpha != 255:
                node["a"] = el.alpha
            if el.radius:
                node["radius"] = el.radius
            if el.border:
                node["bc"] = el.border
                node["bw"] = el.border_w
            if el.on_click:
                node["click"] = True
                _reg(ctx, node["id"], "click", el.on_click)
            if el.hover:
                node["hover"] = el.hover
            ctx.nodes.append(node)
        _arrange_children(ctx, el, x, y, w, h, sc, path)
        return

    # Bare Element (Spacer): nothing to paint.


def _arrange_children(ctx, box, x, y, w, h, sc, path):
    row = box.direction == "row"
    n = len(box.children)
    if n == 0:
        return
    inner_main = (w if row else h) - 2 * box.pad - box.gap * (n - 1)
    inner_cross = (h if row else w) - 2 * box.pad

    sizes = []
    flex_total = 0
    for c in box.children:
        fixed = c.w if row else c.h
        if c.flex > 0:
            sizes.append(None)
            flex_total += c.flex
        elif fixed is not None:
            sizes.append(float(fixed))
        else:
            mw, mh = measure(c)
            sizes.append(float(mw if row else mh))

    leftover = inner_main - sum(s for s in sizes if s is not None)
    for i, c in enumerate(box.children):
        if sizes[i] is None:
            sizes[i] = max(0.0, leftover * c.flex / flex_total)

    main_pos = (x if row else y) + box.pad
    for idx, (c, size) in enumerate(zip(box.children, sizes)):
        fixed_cross = c.h if row else c.w
        if box.align == "stretch" and fixed_cross is None:
            cross = inner_cross
        elif fixed_cross is not None:
            cross = float(fixed_cross)
        else:
            mw, mh = measure(c)
            cross = float(mh if row else mw)
            cross = min(cross, inner_cross)

        if box.align == "center":
            cross_pos = (y if row else x) + box.pad + (inner_cross - cross) / 2
        elif box.align == "end":
            cross_pos = (y if row else x) + box.pad + inner_cross - cross
        else:
            cross_pos = (y if row else x) + box.pad

        cpath = "%s.%d" % (path, idx)
        if row:
            _arrange(ctx, c, main_pos, cross_pos, size, cross, sc, cpath)
        else:
            _arrange(ctx, c, cross_pos, main_pos, cross, size, sc, cpath)
        main_pos += size + box.gap
