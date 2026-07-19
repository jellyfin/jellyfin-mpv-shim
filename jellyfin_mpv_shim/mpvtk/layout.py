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
    Busy,
    Dialog,
    Dropdown,
    Element,
    Float,
    Icon,
    Image,
    ImageMap,
    Menu,
    Scroll,
    Slider,
    Stack,
    Text,
    TextBox,
)


def _icon_paths(names):
    """Resolve Material icon names to unit-canvas ASS paths ('' = no
    icon for that slot)."""
    from .vector import icon_ass

    return [icon_ass(n) if n else "" for n in names]

# Heuristic fallback — keep in sync with CHAR_W in renderer.lua.
# set_metrics() replaces it with measured advances (see metrics.py).
_NARROW = set("iIljtfr.,:;!|'`()[]\"")
_WIDE = set("mwMW@%&")
_SPACE_W = 0.30
_NARROW_W = 0.34
_WIDE_W = 0.85
_DEFAULT_W = 0.54

_measured = None  # {char: fraction} when metrics were applied
_kern = {}  # {2-char pair: fraction adjustment}

LINE_H = 1.25  # text node height as a multiple of font size


def set_metrics(widths, kern=None):
    """Install measured per-char width fractions and pair-kerning
    adjustments (metrics.measure_font). Pass None to revert to the
    heuristic table."""
    global _measured, _kern
    _measured = dict(widths) if widths else None
    _kern = dict(kern) if kern else {}


def char_w(ch):
    if _measured is not None:
        w = _measured.get(ch)
        if w is not None:
            return w
    if ord(ch) >= 0x2E80:  # CJK/fullwidth blocks render ~1em
        return 1.0
    if ch == " ":
        return _SPACE_W
    if ch in _NARROW:
        return _NARROW_W
    if ch in _WIDE:
        return _WIDE_W
    return _DEFAULT_W


def text_width(s, size, bold=False):
    w = 0.0
    prev = None
    for c in s:
        if prev is not None:
            w += _kern.get(prev + c, 0.0)
        w += char_w(c)
        prev = c
    w *= size
    return w * 1.04 if bold else w


def ellipsize(s, size, bold, max_w):
    if text_width(s, size, bold) <= max_w:
        return s
    ell = text_width("…", size, bold)
    out = []
    w = 0.0
    prev = None
    bf = 1.04 if bold else 1.0
    for c in s:
        cw = char_w(c) * size * bf
        if prev is not None:
            cw += _kern.get(prev + c, 0.0) * size * bf
        if w + cw + ell > max_w:
            break
        out.append(c)
        w += cw
        prev = c
    return "".join(out) + "…"


def _break_word(word, size, bold, max_w):
    """Hard-break a word wider than the line into fitting chunks."""
    out = []
    cur = ""
    for ch in word:
        if cur and text_width(cur + ch, size, bold) > max_w:
            out.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        out.append(cur)
    return out or [""]


def wrap_text(s, size, bold, max_w):
    """Greedy word wrap against the measured metrics. ``\\n`` starts a
    new paragraph (blank lines preserved); words wider than ``max_w``
    are hard-broken."""
    lines = []
    for para in s.split("\n"):
        cur = ""
        for word in para.split():
            trial = (cur + " " + word) if cur else word
            if not cur or text_width(trial, size, bold) <= max_w:
                cur = trial
            else:
                lines.append(cur)
                cur = word
            if text_width(cur, size, bold) > max_w:
                chunks = _break_word(cur, size, bold, max_w)
                lines.extend(chunks[:-1])
                cur = chunks[-1]
        lines.append(cur)
    return lines


def _wrap_lines(el, max_w):
    """Wrapped lines for a Text with wrap=True, honoring max_lines
    (the last kept line gets an ellipsis when lines were dropped)."""
    lines = wrap_text(el.text, el.size, el.bold, max_w)
    if el.max_lines is not None and len(lines) > el.max_lines:
        lines = lines[: max(1, el.max_lines)]
        lines[-1] = ellipsize(lines[-1] + "…", el.size, el.bold, max_w)
    return lines


SCROLLBAR_W = 10


def measure(el):
    """Natural (width, height) of an element, ignoring flex."""
    if isinstance(el, Text):
        if el.wrap and el.w is not None:
            n = len(_wrap_lines(el, el.w))
            return el.w, (
                el.h if el.h is not None else n * el.size * LINE_H
            )
        return (
            el.w if el.w is not None else text_width(el.text, el.size, el.bold),
            el.h if el.h is not None else el.size * LINE_H,
        )
    if isinstance(el, (Image, ImageMap)):
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
    if isinstance(el, (Menu, Dialog, Float)):
        return 0, 0  # floating: takes no space in flow
    if isinstance(el, Stack):
        mw, mh = 0.0, 0.0
        for c in el.children:
            cw, ch = measure(c)
            mw = max(mw, c.w if c.w is not None else cw)
            mh = max(mh, c.h if c.h is not None else ch)
        return (
            el.w if el.w is not None else mw,
            el.h if el.h is not None else mh,
        )
    if isinstance(el, (Slider, Busy, Icon)):
        return el.w, el.h
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
    def __init__(self, w=0.0, h=0.0):
        self.nodes = []
        self.handlers = {}
        self.root_w = w
        self.root_h = h


def layout(root, w, h):
    """Returns (nodes, handlers): flat paint-ordered scene nodes and an
    ``{id: {event: callback}}`` registry for the pushed tree."""
    ctx = _Ctx(float(w), float(h))
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
        if el.wrap:
            lh = el.size * LINE_H
            lines = _wrap_lines(el, w)
            # never overflow an assigned height that fits fewer lines
            fit = max(1, int(h / lh + 0.001))
            if len(lines) > fit:
                lines = lines[:fit]
                lines[-1] = ellipsize(
                    lines[-1] + "…", el.size, el.bold, w
                )
        else:
            lh = h
            lines = [ellipsize(el.text, el.size, el.bold, w)]
        base_id = el.id or path
        for i, ln in enumerate(lines):
            node = _base(el, "text", x, y + i * lh, w, lh, sc, path)
            node["id"] = base_id if i == 0 else "%s.l%d" % (base_id, i)
            node["text"] = ln
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
        # containers may assign a stretched size; images never stretch
        w, h = min(w, el.iw), min(h, el.ih)
        node = _base(el, "img", x, y, w, h, sc, path)
        node["src"] = el.src
        node["iw"] = el.iw
        node["ih"] = el.ih
        if el.v:
            node["v"] = el.v
        if el.on_click:
            node["click"] = True
            _reg(ctx, node["id"], "click", el.on_click)
        if el.hover:
            node["hover"] = el.hover
        ctx.nodes.append(node)
        return

    if isinstance(el, ImageMap):
        w, h = min(w, el.iw), min(h, el.ih)
        node = _base(el, "img", x, y, w, h, sc, path)
        node["src"] = el.src
        node["iw"] = el.iw
        node["ih"] = el.ih
        if el.v:
            node["v"] = el.v
        ctx.nodes.append(node)
        for i, reg in enumerate(el.regions):
            rid = reg.get("id") or "%s.r%d" % (node["id"], i)
            rnode = {
                "t": "rect",
                "id": rid,
                "x": _round(x + reg["x"]),
                "y": _round(y + reg["y"]),
                "w": _round(reg["w"]),
                "h": _round(reg["h"]),
                "ring": True,
            }
            if sc:
                rnode["sc"] = sc
            if reg.get("on_click"):
                rnode["click"] = True
                if reg.get("repeat"):
                    rnode["rpt"] = True
                _reg(ctx, rid, "click", reg["on_click"])
            if reg.get("on_context"):
                rnode["ctx"] = True
                _reg(ctx, rid, "context", reg["on_context"])
            rnode["hover"] = reg.get("hover", {"bc": "7aa2f7", "bw": 3})
            ctx.nodes.append(rnode)
        return

    if isinstance(el, TextBox):
        node = _base(el, "textbox", x, y, w, h, sc, path)
        node["text"] = el.text
        node["ph"] = el.placeholder
        node["size"] = el.size
        if el.mask:
            node["mask"] = True
        if el.force:
            node["force"] = True
        _reg(ctx, node["id"], "change", el.on_change)
        _reg(ctx, node["id"], "submit", el.on_submit)
        ctx.nodes.append(node)
        return

    if isinstance(el, Slider):
        node = _base(el, "slider", x, y, w, h, sc, path)
        node["min"] = el.min
        node["max"] = el.max
        node["value"] = el.value
        if el.force:
            node["force"] = True
        _reg(ctx, node["id"], "change", el.on_change)
        ctx.nodes.append(node)
        return

    if isinstance(el, Busy):
        ctx.nodes.append(_base(el, "busy", x, y, w, h, sc, path))
        return

    if isinstance(el, Icon):
        from .vector import icon_ass

        node = _base(el, "icon", x, y, w, h, sc, path)
        node["path"] = icon_ass(el.name)
        node["c"] = el.color
        if el.on_click:
            node["click"] = True
            _reg(ctx, node["id"], "click", el.on_click)
        if el.hover:
            node["hover"] = el.hover
        ctx.nodes.append(node)
        return

    if isinstance(el, (Dialog, Float)):
        # Out-of-flow top layer. Dialog centers itself and grabs input;
        # Float sits at its given position without grabbing.
        cw, ch = measure(el.child)
        if isinstance(el, Dialog):
            dx = (ctx.root_w - cw) / 2
            dy = max(0.0, (ctx.root_h - ch) / 2.5)
        else:
            dx, dy = float(el.x), float(el.y)
        meta = {
            "t": "layer",
            "kind": "modal" if isinstance(el, Dialog) else "float",
            "id": el.id or path,
            "x": _round(dx),
            "y": _round(dy),
            "w": _round(cw),
            "h": _round(ch),
            "top": True,
        }
        if isinstance(el, Dialog):
            meta["mod"] = True
            _reg(ctx, meta["id"], "dismiss", el.on_dismiss)
        ctx.nodes.append(meta)
        start = len(ctx.nodes)
        # floating content never scrolls with page content: sc=None
        _arrange(ctx, el.child, dx, dy, cw, ch, None, path + ".0")
        for n in ctx.nodes[start:]:
            n["top"] = True
            if isinstance(el, Dialog):
                n["mod"] = True
        return

    if isinstance(el, Dropdown):
        node = _base(el, "dropdown", x, y, w, h, sc, path)
        node["items"] = el.items
        node["sel"] = el.selected
        node["size"] = el.size
        if el.icons:
            node["icons"] = _icon_paths(el.icons)
        if el.force:
            node["force"] = True
        _reg(ctx, node["id"], "select", el.on_select)
        ctx.nodes.append(node)
        return

    if isinstance(el, Menu):
        # ignores the flow position: x/y are absolute screen coords
        widest = max(
            (text_width(i, el.size) for i in el.items), default=40
        )
        node = {
            "t": "menu",
            "id": el.id,
            "x": _round(el.x),
            "y": _round(el.y),
            "w": _round(widest + 36),
            "ih": _round(el.size * 1.9),
            "items": el.items,
            "size": el.size,
        }
        if el.icons:
            node["icons"] = _icon_paths(el.icons)
            node["w"] = _round(node["w"] + el.size * 1.5)
        _reg(ctx, el.id, "select", el.on_select)
        _reg(ctx, el.id, "dismiss", el.on_dismiss)
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
        if el.on_scroll:
            node["watch"] = True
            _reg(ctx, node["id"], "scroll", el.on_scroll)
        ctx.nodes.append(node)
        _arrange(ctx, el.child, x, y, cw, ch, node["id"], path + ".0")
        return

    if isinstance(el, Stack):
        for i, c in enumerate(el.children):
            mw, mh = measure(c)
            cw = c.w if c.w is not None else mw
            ch = c.h if c.h is not None else mh
            a = c.anchor
            if a is None or a == "fill":
                cx, cy, cw, ch = x, y, w, h
            else:
                cw, ch = min(cw, w), min(ch, h)
                if a in ("n", "c", "s"):
                    cx = x + (w - cw) / 2
                elif a in ("ne", "e", "se"):
                    cx = x + w - cw
                else:
                    cx = x
                if a in ("w", "c", "e"):
                    cy = y + (h - ch) / 2
                elif a in ("sw", "s", "se"):
                    cy = y + h - ch
                else:
                    cy = y
            cx += c.dx
            cy += c.dy
            cpath = "%s.%d" % (path, i)
            if c.occlude:
                # marker consumed by the renderer: this child's rect is
                # subtracted from IMAGE nodes earlier in paint order, so
                # ASS-drawn content can sit "over" a bitmap sibling
                occ = {
                    "t": "occ",
                    "id": cpath + ".occ",
                    "x": _round(cx),
                    "y": _round(cy),
                    "w": _round(cw),
                    "h": _round(ch),
                }
                if sc:
                    occ["sc"] = sc
                ctx.nodes.append(occ)
            _arrange(ctx, c, cx, cy, cw, ch, sc, cpath)
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
                if el.repeat:
                    node["rpt"] = True
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
        elif isinstance(c, Text) and c.wrap and not row:
            # a wrapped Text's height depends on the width it will get
            wrap_w = c.w if c.w is not None else max(1.0, inner_cross)
            n = len(_wrap_lines(c, wrap_w))
            sizes.append(float(n * c.size * LINE_H))
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
