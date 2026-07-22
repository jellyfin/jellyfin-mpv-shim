"""Layout engine: element tree -> flat scene node list.

Coordinates are OSD pixels, origin top-left. Children of a scroll
container are laid out in content space as if the offset were 0; the
renderer subtracts the live scroll offset and clips to the viewport.

Text measurement is an approximation (per-char advance as a fraction of
the font size); the same table lives in renderer.lua for cursor
positioning — keep them in sync. Real glyph metrics are a known
limitation of the spike.
"""

from . import theme
from .widgets import (
    Box,
    Busy,
    Dialog,
    Dropdown,
    Element,
    Float,
    Gradient,
    Grid,
    Icon,
    Image,
    ImageMap,
    Menu,
    Progress,
    Scroll,
    Slider,
    Stack,
    Text,
    TextBox,
)


def _pad2(box):
    """Box.pad as (pad_x, pad_y) — accepts a uniform int or a tuple."""
    p = box.pad
    if isinstance(p, (tuple, list)):
        return float(p[0]), float(p[1])
    return float(p), float(p)


def _icon_paths(names):
    """Resolve Material icon names to unit-canvas ASS paths ('' = no
    icon for that slot)."""
    from .vector import icon_ass

    return [icon_ass(n) if n else "" for n in names]

# Heuristic fallback — must match char_w() in renderer.lua, which measures
# the same text on the other side of the mpv boundary. Enforced by
# tests/test_python_lua_constants.py.
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


#: How much wider the bold face is than the regular one. Only the regular
#: face is measured (metrics.py), so bold is derived from it by this factor.
#:
#: Was 1.04, which is not close: measuring DejaVuSans against
#: DejaVuSans-Bold with Pillow gives 1.122 aggregate over ASCII, and
#: 1.119-1.130 across real UI strings. At size 17 the old value
#: under-measured a bold heading by ~14px, so whatever the layout put next
#: to it overlapped — the downloads screen's group titles ran into their
#: item counts. renderer.lua carries the same constant and
#: tests/test_python_lua_constants.py pins them together.
BOLD_FACTOR = 1.12


def text_width(s, size, bold=False):
    w = 0.0
    prev = None
    for c in s:
        if prev is not None:
            w += _kern.get(prev + c, 0.0)
        w += char_w(c)
        prev = c
    w *= size
    return w * BOLD_FACTOR if bold else w


def ellipsize(s, size, bold, max_w):
    # Half-pixel slop: a widget's natural width re-derived through the
    # arrange path can lose ~1e-14 to float association, and a strict
    # comparison then truncated exactly-fitting labels ("Up" -> "…").
    max_w = max_w + 0.5
    if text_width(s, size, bold) <= max_w:
        return s
    ell = text_width("…", size, bold)
    out = []
    w = 0.0
    prev = None
    bf = BOLD_FACTOR if bold else 1.0
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


# Wrapping is decided from *estimated* advances, but the text is drawn by
# libass with the real font, and the two disagree by a fraction of a pixel.
# A line that fits by less than that renders one word too long — measuring a
# sample paragraph across window widths, ~1.6% of lines land within 1px of
# the edge, which is exactly how often the overflow was showing up. Wrap
# against a slightly conservative limit so an estimate error can't push the
# last word past the edge. (ellipsize slops the other way on purpose: there,
# being conservative would truncate a label that does fit.)
WRAP_SLOP = 1.0


def wrap_text(s, size, bold, max_w):
    """Greedy word wrap against the measured metrics. ``\\n`` starts a
    new paragraph (blank lines preserved); words wider than ``max_w``
    are hard-broken."""
    limit = max(1.0, max_w - WRAP_SLOP)
    lines = []
    for para in s.split("\n"):
        cur = ""
        for word in para.split():
            trial = (cur + " " + word) if cur else word
            if not cur or text_width(trial, size, bold) <= limit:
                cur = trial
            else:
                lines.append(cur)
                cur = word
            if text_width(cur, size, bold) > limit:
                chunks = _break_word(cur, size, bold, limit)
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


def _res(v, avail=None):
    """Resolve a size constraint: int px, or a float in (0, 1] as a
    fraction of ``avail`` (unresolvable fractions are ignored)."""
    if v is None:
        return None
    if isinstance(v, float) and 0 < v <= 1:
        return v * avail if avail is not None else None
    return float(v)


def _clamp_wh(el, w, h, avail_w=None, avail_h=None):
    """Apply an element's min/max constraints to (w, h)."""
    lo = _res(el.min_w, avail_w)
    hi = _res(el.max_w, avail_w)
    if hi is not None:
        w = min(w, hi)
    if lo is not None:
        w = max(w, lo)
    lo = _res(el.min_h, avail_h)
    hi = _res(el.max_h, avail_h)
    if hi is not None:
        h = min(h, hi)
    if lo is not None:
        h = max(h, lo)
    return w, h


def measure(el):
    """Natural (width, height) of an element, ignoring flex, clamped
    to its min/max constraints (px only — fractions resolve at arrange
    time against the actual available space)."""
    return _clamp_wh(el, *_measure(el))


def natural_size(el):
    """Public build-time fit probe: the size an element tree would
    naturally take. Measure a candidate (say, the full chrome bar)
    against the window width to decide between layouts — no hardcoded
    breakpoints, no one-frame-late node_rect round-trip."""
    return measure(el)


def _measure(el):
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
    if isinstance(el, Gradient):
        return el.w or 0, el.h or 0
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
    if isinstance(el, Grid):
        cells = _grid_cells(el)
        widths = _grid_track_widths(el, cells, None)
        heights = _grid_row_heights(el, cells)
        n = len(el.cols)
        w = sum(widths) + el.gap * (n - 1 if n else 0) + 2 * el.row_pad
        h = (sum(hh + 2 * el.row_pad for hh in heights)
             + el.row_gap * (len(heights) - 1 if heights else 0))
        return (el.w if el.w is not None else w,
                el.h if el.h is not None else h)
    if isinstance(el, Box):
        row = el.direction == "row"
        px, py = _pad2(el)
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
        main += 2 * (px if row else py)
        cross += 2 * (py if row else px)
        w, h = (main, cross) if row else (cross, main)
        return el.w if el.w is not None else w, (
            el.h if el.h is not None else h
        )
    return el.w or 0, el.h or 0


def _grid_cells(el):
    """Normalize Grid rows to (cells, spec) pairs: str -> Text
    (track-aligned), None stays; dict rows split into their cell list
    and the row styling/interaction spec."""
    out = []
    for r in el.rows:
        spec = None
        if isinstance(r, dict):
            spec = r
            r = r.get("cells", [])
        row = []
        for ci in range(len(el.cols)):
            v = r[ci] if ci < len(r) else None
            if v is None or isinstance(v, Element):
                row.append(v)
            else:
                col = el.cols[ci]
                row.append(
                    Text(str(v), size=el.size, color=el.fg,
                         align=col.get("align", "left"))
                )
        out.append((row, spec))
    return out


def _grid_track_widths(el, cells, avail_w):
    """Column track widths. Fixed -> w; auto -> widest cell; flex ->
    share of the leftover when ``avail_w`` is known, else its natural
    (widest cell) so a shrink-wrapped Grid still measures sanely."""
    n = len(el.cols)
    widths = [None] * n
    for ci, col in enumerate(el.cols):
        if col.get("w") is not None:
            widths[ci] = float(col["w"])
    for ci, col in enumerate(el.cols):
        if widths[ci] is None and (avail_w is None
                                   or not col.get("flex")):
            mx = 0.0
            for row, _spec in cells:
                c = row[ci]
                if c is not None:
                    mx = max(mx, measure(c)[0])
            widths[ci] = mx
    if avail_w is not None:
        flex_total = sum(
            col.get("flex", 0)
            for ci, col in enumerate(el.cols) if widths[ci] is None
        )
        fixed = sum(wd for wd in widths if wd is not None)
        leftover = max(0.0, avail_w - fixed - el.gap * (n - 1)
                       - 2 * el.row_pad)
        for ci, col in enumerate(el.cols):
            if widths[ci] is None:
                widths[ci] = leftover * col.get("flex", 0) / flex_total
    return widths


def _grid_row_heights(el, cells):
    heights = []
    for row, _spec in cells:
        mx = float(el.row_h or 0)
        for c in row:
            if c is not None:
                mx = max(mx, measure(c)[1])
        heights.append(mx)
    return heights


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
    if getattr(el, "tip", None):
        node["tip"] = el.tip
    if getattr(el, "autofocus", False):
        node["af"] = True
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
        # containers may assign a stretched size; images never stretch.
        # Clamp against the LOGICAL footprint: iw/ih are physical, and at
        # any scale != 1 comparing them to a logical w/h mixes spaces.
        w, h = min(w, el.lw), min(h, el.lh)
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
        w, h = min(w, el.lw), min(h, el.lh)
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
            if reg.get("on_dbl"):
                rnode["dbl"] = True
                _reg(ctx, rid, "dbl", reg["on_dbl"])
            if reg.get("on_context"):
                rnode["ctx"] = True
                _reg(ctx, rid, "context", reg["on_context"])
            rnode["hover"] = reg.get(
                "hover", {"bc": theme.ACCENT, "bw": 3})
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
        _reg(ctx, node["id"], "commit", el.on_commit)
        ctx.nodes.append(node)
        return

    if isinstance(el, Slider):
        node = _base(el, "slider", x, y, w, h, sc, path)
        node["min"] = el.min
        node["max"] = el.max
        node["value"] = el.value
        if el.force:
            node["force"] = True
        if el.marks:
            node["marks"] = [round(float(m), 4) for m in el.marks]
        if el.ranges:
            node["ranges"] = [
                [round(float(a), 4), round(float(b), 4)]
                for a, b in el.ranges
            ]
        if el.on_hover is not None:
            node["hoverev"] = True
        if el.always_adjust:
            node["aadj"] = True
        _reg(ctx, node["id"], "change", el.on_change)
        _reg(ctx, node["id"], "commit", el.on_commit)
        _reg(ctx, node["id"], "cancel", el.on_cancel)
        _reg(ctx, node["id"], "hover", el.on_hover)
        _reg(ctx, node["id"], "hover_end", el.on_hover_end)
        ctx.nodes.append(node)
        return

    if isinstance(el, Busy):
        ctx.nodes.append(_base(el, "busy", x, y, w, h, sc, path))
        return

    if isinstance(el, Gradient):
        node = _base(el, "grad", x, y, w, h, sc, path)
        node["c"] = el.color
        node["a1"] = el.top
        node["a2"] = el.bottom
        ctx.nodes.append(node)
        return

    if isinstance(el, Progress):
        node = _base(el, "rect", x, y, w, h, sc, path)
        node["fill"] = el.bg
        node["radius"] = h / 2
        ctx.nodes.append(node)
        fw = w * el.frac
        if fw >= 1:
            fill = {
                "t": "rect",
                "id": node["id"] + ".fill",
                "x": _round(x),
                "y": _round(y),
                "w": _round(fw),
                "h": _round(h),
                "fill": el.fg,
                "radius": h / 2,
            }
            if sc:
                fill["sc"] = sc
            ctx.nodes.append(fill)
        return

    if isinstance(el, Grid):
        cells = _grid_cells(el)
        widths = _grid_track_widths(el, cells, w)
        heights = _grid_row_heights(el, cells)
        rp = float(el.row_pad)
        cy = y
        for ri, (row, spec) in enumerate(cells):
            if spec is not None and (spec.get("bg") or spec.get("id")
                                     or spec.get("on_click")
                                     or spec.get("on_dbl")
                                     or spec.get("hover")):
                rid = spec.get("id") or "%s.gr%d" % (path, ri)
                rect = {
                    "t": "rect",
                    "id": rid,
                    "x": _round(x),
                    "y": _round(cy),
                    "w": _round(w),
                    "h": _round(heights[ri] + 2 * rp),
                }
                if sc:
                    rect["sc"] = sc
                if spec.get("bg"):
                    rect["fill"] = spec["bg"]
                if spec.get("radius"):
                    rect["radius"] = spec["radius"]
                if spec.get("hover"):
                    rect["hover"] = spec["hover"]
                if spec.get("on_click"):
                    rect["click"] = True
                    _reg(ctx, rid, "click", spec["on_click"])
                if spec.get("on_dbl"):
                    rect["dbl"] = True
                    _reg(ctx, rid, "dbl", spec["on_dbl"])
                ctx.nodes.append(rect)
            cx = x + rp
            for ci, c in enumerate(row):
                tw = widths[ci]
                if c is not None:
                    mw, mh = measure(c)
                    if isinstance(c, Text) or c.flex > 0:
                        cw2 = tw
                    else:
                        cw2 = min(mw, tw)
                    ch2 = min(mh, heights[ri]) if heights[ri] else mh
                    a = el.cols[ci].get("align", "left")
                    if isinstance(c, Text) or a == "left":
                        ox = 0.0
                    elif a == "center":
                        ox = (tw - cw2) / 2
                    else:
                        ox = tw - cw2
                    oy = rp + (heights[ri] - ch2) / 2
                    _arrange(ctx, c, cx + ox, cy + oy, cw2, ch2, sc,
                             "%s.g%d_%d" % (path, ri, ci))
                cx += tw + el.gap
            cy += heights[ri] + 2 * rp + el.row_gap
        return

    if isinstance(el, Icon):
        from .vector import icon_ass

        node = _base(el, "icon", x, y, w, h, sc, path)
        node["path"] = icon_ass(el.name)
        node["c"] = el.color
        if el.hover_parent and el.hover_tint:
            node["hb"] = el.hover_parent
            node["hc"] = el.hover_tint
        if el.on_click:
            node["click"] = True
            _reg(ctx, node["id"], "click", el.on_click)
        if el.hover:
            node["hover"] = el.hover
        ctx.nodes.append(node)
        return

    if isinstance(el, (Dialog, Float)):
        # Out-of-flow top layer. Dialog centers itself and grabs input;
        # Float sits at its given position without grabbing. Fractional
        # min/max on the child resolve against the window here, so a
        # dialog can say "natural, but at most 60% of the screen".
        cw, ch = measure(el.child)
        cw, ch = _clamp_wh(el.child, cw, ch, ctx.root_w, ctx.root_h)
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
        if el.trigger_icon:
            from .vector import icon_ass

            node["ticon"] = icon_ass(el.trigger_icon)
            # icon triggers are narrower than their popup: size the
            # popup to the items (like Menu) and let the renderer
            # clamp it to the screen edges
            widest = max(
                (text_width(i, el.size) for i in el.items), default=40
            )
            pw = widest + 36
            if el.icons:
                pw += el.size * 1.5
            node["pw"] = _round(pw)
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
            # "rh", not "ih": this is a row height in LOGICAL px and must
            # scale, whereas an img node's "ih" is the physical bitmap
            # height and must not. One key meaning both is how the menu
            # ended up drawing 1x rows under 2x text.
            "rh": _round(el.size * 1.9),
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
        if el.follow:
            node["follow"] = True
        if getattr(el, "snaps", None):
            # Explicit unequal breakpoints (home sections). Logical; scale_scene
            # scales each element. Takes precedence over uniform snap.
            node["snaps"] = list(el.snaps)
        elif getattr(el, "snap", None):
            # Row-quantized display offset (see Scroll/renderer.lua snap_round).
            # Logical here; scale_scene converts both to physical, like cw/ch.
            node["snap"] = el.snap
            node["snap_off"] = el.snap_off
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
            if el.on_dbl:
                node["dbl"] = True
                _reg(ctx, node["id"], "dbl", el.on_dbl)
            if getattr(el, "on_context", None):
                node["ctx"] = True
                _reg(ctx, node["id"], "context", el.on_context)
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
    px, py = _pad2(box)
    pad_main = px if row else py
    pad_cross = py if row else px
    inner_main = (w if row else h) - 2 * pad_main - box.gap * (n - 1)
    inner_cross = (h if row else w) - 2 * pad_cross

    def clamp_main(c, size):
        lo = _res(c.min_w if row else c.min_h, inner_main)
        hi = _res(c.max_w if row else c.max_h, inner_main)
        if hi is not None:
            size = min(size, hi)
        if lo is not None:
            size = max(size, lo)
        return size

    sizes = []
    flex_total = 0
    for c in box.children:
        fixed = c.w if row else c.h
        if c.flex > 0:
            sizes.append(None)
            flex_total += c.flex
        elif fixed is not None:
            sizes.append(clamp_main(c, float(fixed)))
        elif isinstance(c, Text) and c.wrap and not row:
            # a wrapped Text's height depends on the width it will get
            wrap_w = c.w if c.w is not None else max(1.0, inner_cross)
            nl = len(_wrap_lines(c, wrap_w))
            sizes.append(float(nl * c.size * LINE_H))
        else:
            mw, mh = measure(c)
            sizes.append(clamp_main(c, float(mw if row else mh)))

    leftover = inner_main - sum(s for s in sizes if s is not None)
    for i, c in enumerate(box.children):
        if sizes[i] is None:
            sizes[i] = clamp_main(
                c, max(0.0, leftover * c.flex / flex_total))

    # flex-shrink, ROWS only: fixed/natural children used to overflow
    # silently and push content off-screen in narrow windows. Shrink
    # proportionally, floored at each child's min (bitmaps/icons floor
    # at natural — pixels never squeeze; Text just re-ellipsizes).
    # Columns keep overflowing on purpose: vertical overflow is normal
    # pre-scroll content, not a layout error.
    total = sum(sizes)
    if row and total > inner_main > 0:
        floors = []
        for c, s in zip(box.children, sizes):
            lo = _res(c.min_w if row else c.min_h, inner_main)
            if lo is None and isinstance(c, (Image, ImageMap, Icon,
                                             Busy)):
                lo = s
            if lo is None and isinstance(c, Box) and (c.on_click or
                                                      c.on_dbl):
                # buttons floor at natural: an "Edit" squeezed to "E…"
                # is garbage — long plain text absorbs the shrink
                # instead (it ellipsizes meaningfully)
                lo = s
            floors.append(min(lo if lo is not None else 0.0, s))
        shrinkable = sum(s - f for s, f in zip(sizes, floors))
        if shrinkable > 0:
            k = min(1.0, (total - inner_main) / shrinkable)
            sizes = [s - (s - f) * k for s, f in zip(sizes, floors)]

    # main-axis justification: distribute the slack that flex children
    # didn't absorb (with flex present the slack is already zero)
    main_pos = (x if row else y) + pad_main
    extra_gap = 0.0
    justify = getattr(box, "justify", "start")
    slack = inner_main - sum(sizes)
    if slack > 0 and justify != "start":
        if justify == "center":
            main_pos += slack / 2
        elif justify == "end":
            main_pos += slack
        elif justify == "between" and n > 1:
            extra_gap = slack / (n - 1)

    for idx, (c, size) in enumerate(zip(box.children, sizes)):
        fixed_cross = c.h if row else c.w
        if box.align == "stretch" and fixed_cross is None:
            cross = inner_cross
        elif fixed_cross is not None:
            cross = float(fixed_cross)
        elif row and isinstance(c, Text) and c.wrap:
            # a wrapped Text in a Row wraps to its main-axis width;
            # its cross size (height) follows the wrapped line count
            wrapped = c
            lines = len(_wrap_lines(wrapped, max(1.0, size)))
            if wrapped.max_lines is not None:
                lines = min(lines, wrapped.max_lines)
            cross = min(float(lines) * c.size * LINE_H, inner_cross)
        else:
            mw, mh = measure(c)
            cross = float(mh if row else mw)
            cross = min(cross, inner_cross)
        lo = _res(c.min_h if row else c.min_w, inner_cross)
        hi = _res(c.max_h if row else c.max_w, inner_cross)
        if hi is not None:
            cross = min(cross, hi)
        if lo is not None:
            cross = max(cross, lo)

        if box.align == "center":
            cross_pos = (y if row else x) + pad_cross + (
                inner_cross - cross) / 2
        elif box.align == "end":
            cross_pos = (y if row else x) + pad_cross + inner_cross - cross
        else:
            cross_pos = (y if row else x) + pad_cross

        cpath = "%s.%d" % (path, idx)
        if row:
            _arrange(ctx, c, main_pos, cross_pos, size, cross, sc, cpath)
        else:
            _arrange(ctx, c, cross_pos, main_pos, cross, size, sc, cpath)
        main_pos += size + box.gap + extra_gap
