"""Vector icons for mpvtk — the same Material icon set and SVG->ASS
pipeline the Tk browser (rasterized) and the jellyfin OSC (ASS) use.

Icons are converted at first use from `ui_icon_paths.py`
(dep-free generated data) via the shared `svgpath` converter, on the
24x24 unit canvas with the OSC's corner-anchor convention: two
zero-length contours pin the bounding box so libass scales and aligns
the drawing exactly like a 24x24 box regardless of the glyph's ink.
The renderer scales with \\fscx/\\fscy — crisp at any size.

`icon_image` is the second consumer of the same data: a PIL rasterizer,
for the decorations that are *baked into a bitmap* rather than drawn as
widgets (the strip compositor's corner badges). One source of truth, so
a symbol drawn on a tile is the same symbol drawn in the widget beside
it.
"""

import logging
import math

from ..ui_icon_paths import ICON_PATHS
from ..svgpath import svg_path_to_ass

log = logging.getLogger("mpvtk.vector")

_ANCHOR = "m 0 0 l 0 0 m 24 24 l 24 24"
_cache = {}
_warned = set()


def icon_ass(name):
    """Unit-canvas (24x24, corner-anchored) ASS drawing for a Material
    icon.

    An unknown name yields an empty (but correctly sized) drawing rather
    than raising. This used to be a KeyError out of the middle of layout,
    which takes down the *entire* scene — one button naming an icon that
    isn't in the generated set and the whole browser goes blank. A missing
    glyph is a cosmetic bug; it should not be a fatal one. It is logged
    once per name so it still gets noticed.
    """
    path = _cache.get(name)
    if path is None:
        try:
            drawing = svg_path_to_ass(ICON_PATHS[name])
        except KeyError:
            if name not in _warned:
                _warned.add(name)
                log.warning("unknown icon %r — rendering nothing", name)
            drawing = ""
        path = ("%s %s" % (_ANCHOR, drawing)) if drawing else _ANCHOR
        _cache[name] = path
    return path


def icon_names():
    return sorted(ICON_PATHS)


# -- rasterizing ------------------------------------------------------------
#
# The renderer draws icons as ASS, which is how every icon in a *widget*
# reaches the screen. Decorations baked into a strip bitmap cannot use that
# path — a strip is composited by PIL, and mpv draws overlay bitmaps ABOVE
# all script ASS (GUIDE §6), so an ASS glyph over a tile would disappear
# under the artwork it is meant to annotate.
#
# So the same path data is flattened to polygons here. The point is that
# both routes start from ``ICON_PATHS``: the recording symbol on a tile is
# the *same glyph* as the one in the guide cell beside it, rather than a
# hand-drawn approximation that drifts from it.

#: Segments per cubic when flattening. 12 is indistinguishable from a curve
#: at the sizes these are drawn (a ~34px badge, supersampled ×3).
_CURVE_STEPS = 12

#: Supersample factor. The flattened polygons are aliased; PIL has no
#: antialiased fill, so draw big and downscale — the same trick the
#: carousel's round arrows use.
_SS = 3

_raster_cache: dict = {}


def _contours(name):
    """The icon's filled contours as lists of (x, y) on the 24x24 canvas.

    Parsed back out of the ASS drawing rather than from the raw ``d``: that
    converter already handles every SVG command, including the elliptical
    arcs a few of these use, and re-implementing it here is how the two
    representations would come to disagree.
    """
    cached = _raster_cache.get(("contours", name))
    if cached is not None:
        return cached
    drawing = icon_ass(name)
    contours, current, cmd = [], [], None
    tokens = drawing.split()
    i = 0
    pending: list = []

    def flush():
        if len(current) > 2:
            contours.append(list(current))

    while i < len(tokens):
        token = tokens[i]
        if token in ("m", "l", "b", "n", "s", "p", "c"):
            if token == "m":
                flush()
                del current[:]
            cmd, pending = token, []
            i += 1
            continue
        # A coordinate pair.
        try:
            x, y = float(token), float(tokens[i + 1])
        except (ValueError, IndexError):
            i += 1
            continue
        i += 2
        if cmd == "b":
            pending.append((x, y))
            if len(pending) == 3 and current:
                x0, y0 = current[-1]
                (x1, y1), (x2, y2), (x3, y3) = pending
                for step in range(1, _CURVE_STEPS + 1):
                    t = step / _CURVE_STEPS
                    u = 1 - t
                    current.append((
                        u * u * u * x0 + 3 * u * u * t * x1
                        + 3 * u * t * t * x2 + t * t * t * x3,
                        u * u * u * y0 + 3 * u * u * t * y1
                        + 3 * u * t * t * y2 + t * t * t * y3))
                pending = []
        else:
            current.append((x, y))
    flush()
    # Drop the two zero-length anchor contours — they carry no ink, and a
    # degenerate polygon makes PIL draw a stray pixel at the corner.
    contours = [c for c in contours if len(c) > 2]
    _raster_cache[("contours", name)] = contours
    return contours


def _nonzero_mask(contours, big, scale):
    """An ``L`` mask of the icon's ink, filled by the **nonzero winding
    rule** — SVG's default, and the one this path data is authored for.

    Contours used to be filled independently, on the reasoning that no icon
    in the set relied on an inner contour punching a hole. Material's filled
    ``photo`` and ``photo_album`` do exactly that: one subpath traces the
    frame and a second, wound the other way, cuts the mountain out of it.
    Filled independently they came out as solid squares — which is the
    graceful-degradation the old comment promised, and also completely
    unreadable as a type indicator.

    A scanline fill rather than "fill the outer contours, erase the inner
    ones": the winding number is what the rule is defined on, so counting it
    is both simpler than classifying contours and correct for the nesting
    depths that classification gets wrong. Cost is one pass over the edge
    list per raster row of a ≤24px glyph, and the result is cached.
    """
    from PIL import Image, ImageDraw

    mask = Image.new("L", (big, big), 0)
    edges = []
    for contour in contours:
        pts = [(x * scale, y * scale) for x, y in contour]
        for i in range(len(pts)):
            (x0, y0), (x1, y1) = pts[i], pts[(i + 1) % len(pts)]
            if y0 != y1:
                # Horizontal edges contribute no crossings; skipping them
                # also keeps the division below safe.
                edges.append((x0, y0, x1, y1))
    if not edges:
        return mask
    draw = ImageDraw.Draw(mask)
    for row in range(big):
        y = row + 0.5                      # sample at the pixel centre
        crossings = []
        for x0, y0, x1, y1 in edges:
            # Half-open in y so a vertex shared by two edges is counted
            # once, not twice or zero times.
            if (y0 <= y < y1) or (y1 <= y < y0):
                t = (y - y0) / (y1 - y0)
                crossings.append((x0 + t * (x1 - x0), 1 if y1 > y0 else -1))
        if not crossings:
            continue
        crossings.sort()
        winding = 0
        for i in range(len(crossings) - 1):
            winding += crossings[i][1]
            if winding == 0:
                continue
            # Pixel centres inside [a, b) are ink.
            start = int(math.ceil(crossings[i][0] - 0.5))
            end = int(math.ceil(crossings[i + 1][0] - 0.5)) - 1
            if end >= start:
                draw.line((max(start, 0), row, min(end, big - 1), row),
                          fill=255)
    return mask


def icon_image(name, size, color):
    """A Material icon as an RGBA PIL image, ``size`` px square.

    ``color`` is an ``(r, g, b)`` tuple. Cached per (name, size, colour):
    these are drawn per tile, i.e. potentially dozens per composited row.
    """
    key = ("image", name, int(size), tuple(color))
    hit = _raster_cache.get(key)
    if hit is not None:
        return hit
    from PIL import Image

    contours = _contours(name)
    big = int(size) * _SS
    canvas = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    if contours:
        canvas.paste(tuple(color) + (255,), (0, 0),
                     _nonzero_mask(contours, big, big / 24.0))
    lanczos = Image.LANCZOS  # type: ignore[attr-defined]
    out = canvas.resize((int(size), int(size)), lanczos)
    _raster_cache[key] = out
    return out
