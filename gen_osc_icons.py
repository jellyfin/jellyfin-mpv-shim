#!/usr/bin/env python3
"""Regenerate the vector icon table embedded in trickplay-jf-osc.lua.

The Jellyfin-styled OSC draws its buttons as ASS vector drawings ({\\p1}
paths) instead of relying on an icon font, because mpv only loads custom
fonts from its own config directory (which the shim does not control for
external mpv). The paths are converted from Google's Material Design icons
(Apache License 2.0, https://github.com/google/material-design-icons) --
the same icon set jellyfin-web uses via its ligature font.

Usage: ./gen_osc_icons.py [--svg-dir DIR]

By default the SVG sources are downloaded from the material-design-icons
repository. Pass --svg-dir to use a directory of pre-downloaded
<name>.svg files instead. The output is written into
jellyfin_mpv_shim/trickplay-jf-osc.lua between the
"-- BEGIN generated icons" / "-- END generated icons" markers (or to
stdout if the file or markers are missing).

Each icon is emitted on a 24x24 unit canvas. Two zero-length line
contours at (0,0) and (24,24) are prepended so libass computes the same
bounding box for every icon, which keeps \\an-based alignment stable
regardless of how much of the canvas the glyph ink covers.
"""

import math
import os
import re
import sys
import urllib.request

# icon name -> category in the material-design-icons repo layout
ICONS = {
    "play_arrow": "av",
    "pause": "av",
    "fast_rewind": "av",
    "fast_forward": "av",
    "skip_previous": "av",
    "skip_next": "av",
    "closed_caption": "av",
    "volume_up": "av",
    "volume_down": "av",
    "volume_off": "av",
    "undo": "content",
    "redo": "content",
    "audiotrack": "image",
    "settings": "action",
    "fullscreen": "navigation",
    "fullscreen_exit": "navigation",
    "check": "navigation",
    "close": "navigation",
    "arrow_back": "navigation",
    "favorite": "action",
    "groups": "social",
}

SVG_URL = (
    "https://raw.githubusercontent.com/google/material-design-icons/"
    "master/src/{category}/{name}/materialicons/24px.svg"
)

TARGET = os.path.join(
    os.path.dirname(os.path.abspath(__file__)),
    "jellyfin_mpv_shim",
    "trickplay-jf-osc.lua",
)
BEGIN_MARK = "-- BEGIN generated icons"
END_MARK = "-- END generated icons"

_NUM = re.compile(r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?")
_CMD = re.compile(r"[MmLlHhVvCcSsQqTtAaZz]")


def fmt(v):
    """Format a coordinate: <= 2 decimals, no trailing zeros."""
    s = "%.2f" % v
    s = s.rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


class AssPath:
    def __init__(self):
        self.parts = []

    def move(self, x, y):
        self.parts.append("m %s %s" % (fmt(x), fmt(y)))

    def line(self, x, y):
        self.parts.append("l %s %s" % (fmt(x), fmt(y)))

    def cubic(self, x1, y1, x2, y2, x, y):
        self.parts.append(
            "b %s %s %s %s %s %s"
            % (fmt(x1), fmt(y1), fmt(x2), fmt(y2), fmt(x), fmt(y))
        )


def arc_to_cubics(x0, y0, rx, ry, phi_deg, large, sweep, x, y):
    """SVG elliptical arc -> list of cubic bezier control points.

    Implements the endpoint-to-center conversion from the SVG spec
    (appendix B.2.4), then approximates the arc with one cubic per <= 90
    degree slice.
    """
    if rx == 0 or ry == 0:
        return [("l", x, y)]
    rx, ry = abs(rx), abs(ry)
    phi = math.radians(phi_deg % 360)
    cosp, sinp = math.cos(phi), math.sin(phi)

    dx2, dy2 = (x0 - x) / 2.0, (y0 - y) / 2.0
    x1p = cosp * dx2 + sinp * dy2
    y1p = -sinp * dx2 + cosp * dy2

    lam = (x1p / rx) ** 2 + (y1p / ry) ** 2
    if lam > 1:
        s = math.sqrt(lam)
        rx, ry = rx * s, ry * s

    num = rx**2 * ry**2 - rx**2 * y1p**2 - ry**2 * x1p**2
    den = rx**2 * y1p**2 + ry**2 * x1p**2
    co = math.sqrt(max(0.0, num / den)) if den else 0.0
    if large == sweep:
        co = -co
    cxp = co * rx * y1p / ry
    cyp = -co * ry * x1p / rx

    cx = cosp * cxp - sinp * cyp + (x0 + x) / 2.0
    cy = sinp * cxp + cosp * cyp + (y0 + y) / 2.0

    def angle(ux, uy, vx, vy):
        dot = ux * vx + uy * vy
        length = math.hypot(ux, uy) * math.hypot(vx, vy)
        ang = math.acos(max(-1.0, min(1.0, dot / length)))
        if ux * vy - uy * vx < 0:
            ang = -ang
        return ang

    theta1 = angle(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dtheta = angle(
        (x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry
    )
    if not sweep and dtheta > 0:
        dtheta -= 2 * math.pi
    elif sweep and dtheta < 0:
        dtheta += 2 * math.pi

    nseg = max(1, int(math.ceil(abs(dtheta) / (math.pi / 2))))
    delta = dtheta / nseg
    k = 4.0 / 3.0 * math.tan(delta / 4.0)

    out = []
    t = theta1
    for _ in range(nseg):
        cos1, sin1 = math.cos(t), math.sin(t)
        cos2, sin2 = math.cos(t + delta), math.sin(t + delta)

        def pt(c, s):
            return (
                cx + rx * c * cosp - ry * s * sinp,
                cy + rx * c * sinp + ry * s * cosp,
            )

        def dpt(c, s):
            return (
                -rx * s * cosp - ry * c * sinp,
                -rx * s * sinp + ry * c * cosp,
            )

        p1 = pt(cos1, sin1)
        p2 = pt(cos2, sin2)
        d1 = dpt(cos1, sin1)
        d2 = dpt(cos2, sin2)
        out.append(
            (
                "b",
                p1[0] + k * d1[0],
                p1[1] + k * d1[1],
                p2[0] - k * d2[0],
                p2[1] - k * d2[1],
                p2[0],
                p2[1],
            )
        )
        t += delta
    return out


def svg_path_to_ass(d):
    """Convert one SVG path `d` attribute to ASS drawing commands."""
    tokens = []
    pos = 0
    while pos < len(d):
        ch = d[pos]
        if _CMD.match(ch):
            tokens.append(ch)
            pos += 1
        else:
            m = _NUM.match(d, pos)
            if m:
                tokens.append(float(m.group()))
                pos = m.end()
            else:
                pos += 1  # whitespace / comma

    ass = AssPath()
    i = 0
    cmd = None
    cx = cy = sx = sy = 0.0  # current point, subpath start
    pcx = pcy = None  # previous cubic control (for S/s)
    pqx = pqy = None  # previous quadratic control (for T/t)

    def take(n):
        nonlocal i
        vals = tokens[i : i + n]
        i += n
        return vals

    while i < len(tokens):
        if isinstance(tokens[i], str):
            cmd = tokens[i]
            i += 1
            if cmd in "Zz":
                cx, cy = sx, sy
                pcx = pcy = pqx = pqy = None
                continue
        elif cmd is None:
            raise ValueError("path starts with a number")
        elif cmd in "Mm":
            # implicit lineto after moveto
            cmd = "L" if cmd == "M" else "l"

        rel = cmd.islower()
        c = cmd.upper()
        new_pc = new_pq = None

        if c == "M":
            x, y = take(2)
            if rel:
                x, y = cx + x, cy + y
            ass.move(x, y)
            cx, cy, sx, sy = x, y, x, y
        elif c == "L":
            x, y = take(2)
            if rel:
                x, y = cx + x, cy + y
            ass.line(x, y)
            cx, cy = x, y
        elif c == "H":
            (x,) = take(1)
            if rel:
                x = cx + x
            ass.line(x, cy)
            cx = x
        elif c == "V":
            (y,) = take(1)
            if rel:
                y = cy + y
            ass.line(cx, y)
            cy = y
        elif c in "CS":
            if c == "C":
                x1, y1, x2, y2, x, y = take(6)
                if rel:
                    x1, y1, x2, y2, x, y = (
                        cx + x1, cy + y1, cx + x2, cy + y2, cx + x, cy + y,
                    )
            else:
                x2, y2, x, y = take(4)
                if rel:
                    x2, y2, x, y = cx + x2, cy + y2, cx + x, cy + y
                if pcx is not None:
                    x1, y1 = 2 * cx - pcx, 2 * cy - pcy
                else:
                    x1, y1 = cx, cy
            ass.cubic(x1, y1, x2, y2, x, y)
            new_pc = (x2, y2)
            cx, cy = x, y
        elif c in "QT":
            if c == "Q":
                qx, qy, x, y = take(4)
                if rel:
                    qx, qy, x, y = cx + qx, cy + qy, cx + x, cy + y
            else:
                x, y = take(2)
                if rel:
                    x, y = cx + x, cy + y
                if pqx is not None:
                    qx, qy = 2 * cx - pqx, 2 * cy - pqy
                else:
                    qx, qy = cx, cy
            # quadratic -> cubic
            x1, y1 = cx + 2.0 / 3.0 * (qx - cx), cy + 2.0 / 3.0 * (qy - cy)
            x2, y2 = x + 2.0 / 3.0 * (qx - x), y + 2.0 / 3.0 * (qy - y)
            ass.cubic(x1, y1, x2, y2, x, y)
            new_pq = (qx, qy)
            cx, cy = x, y
        elif c == "A":
            rx, ry, rot, large, sweep, x, y = take(7)
            if rel:
                x, y = cx + x, cy + y
            for seg in arc_to_cubics(
                cx, cy, rx, ry, rot, bool(large), bool(sweep), x, y
            ):
                if seg[0] == "l":
                    ass.line(seg[1], seg[2])
                else:
                    ass.cubic(*seg[1:])
            cx, cy = x, y
        else:
            raise ValueError("unsupported command %r" % cmd)

        pcx, pcy = new_pc if new_pc else (None, None)
        pqx, pqy = new_pq if new_pq else (None, None)

    return " ".join(ass.parts)


def extract_paths(svg):
    """All filled path `d` attributes from an SVG document."""
    out = []
    for m in re.finditer(r"<path\b[^>]*>", svg):
        tag = m.group(0)
        if 'fill="none"' in tag:
            continue
        d = re.search(r'\bd="([^"]*)"', tag)
        if d:
            out.append(d.group(1))
    return out


def get_svg(name, svg_dir):
    if svg_dir:
        with open(os.path.join(svg_dir, name + ".svg")) as f:
            return f.read()
    url = SVG_URL.format(category=ICONS[name], name=name)
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8")


def main():
    svg_dir = None
    args = sys.argv[1:]
    if args and args[0] == "--svg-dir":
        svg_dir = args[1]

    # zero-length contours pinning the bbox to the full 24x24 canvas
    anchor = "m 0 0 l 0 0 m 24 24 l 24 24"

    lines = []
    lines.append(BEGIN_MARK)
    lines.append("-- Generated by gen_osc_icons.py -- do not edit by hand.")
    lines.append("-- Path data derived from Google Material Design icons,")
    lines.append("-- Copyright Google LLC, Apache License 2.0:")
    lines.append("--   https://github.com/google/material-design-icons")
    lines.append("-- All icons are ASS drawing commands on a 24x24 canvas.")
    lines.append("local icons = {")
    for name in sorted(ICONS):
        svg = get_svg(name, svg_dir)
        paths = extract_paths(svg)
        if not paths:
            raise SystemExit("no filled paths found for %s" % name)
        ass = " ".join(svg_path_to_ass(d) for d in paths)
        lines.append('    %s = "%s %s",' % (name, anchor, ass))
    lines.append("}")
    lines.append(END_MARK)
    block = "\n".join(lines)

    try:
        with open(TARGET) as f:
            content = f.read()
        begin = content.index(BEGIN_MARK)
        end = content.index(END_MARK) + len(END_MARK)
    except (OSError, ValueError):
        print(block)
        return
    with open(TARGET, "w") as f:
        f.write(content[:begin] + block + content[end:])
    print("updated %s (%d icons)" % (TARGET, len(ICONS)))


if __name__ == "__main__":
    main()
