"""UI scale factor: the logical/physical pixel boundary.

Every number in Python view code is logical; bitmap rasterization and the
Lua side are the only physical things. See `mpvtk/GUIDE.md` section 8 for
why images are the leak in that abstraction, and for the exact-vs-rounded
split below.
"""

import math

# Resolved once at startup (see app._dispatch on "ready"). Not reactive:
# changing it needs a restart, because rescaling live would mean dropping
# every cached bitmap, and StripStore.clear() is only safe once mpv is
# dead on the libmpv path.
_scale = 1.0


def set_scale(value):
    """Set the global factor. Values <= 0 or non-finite fall back to 1.0."""
    global _scale
    try:
        v = float(value)
    except (TypeError, ValueError):
        v = 1.0
    if not math.isfinite(v) or v <= 0:
        v = 1.0
    _scale = v
    return _scale


def scale():
    return _scale


def px(dip_value):
    """Logical -> physical. THE rounding rule; never inline this."""
    if _scale == 1.0:
        return int(dip_value) if isinstance(dip_value, int) else _round(dip_value)
    return _round(dip_value * _scale)


def dip(px_value):
    """Physical -> logical. For anything coming back from mpv in real
    pixels (mouse positions, surface size)."""
    return px_value / _scale


def raster(w, h):
    """Physical (w, h) for a producer about to rasterize a logical box."""
    return px(w), px(h)


def logical_size(size):
    """Physical surface size -> the logical size views lay out against.

    Deliberately float: truncating here would lose up to a pixel of usable
    width per axis, and layout rounds at the end anyway.
    """
    if _scale == 1.0:
        return size
    return (size[0] / _scale, size[1] / _scale)


def _round(v):
    return int(math.floor(v + 0.5))


# --------------------------------------------------------------------------
# scene conversion
# --------------------------------------------------------------------------

# Pixel geometry, uniform across every node type (audited against
# layout.py's emission and renderer.lua's reads).
_PX_KEYS = ("x", "y", "w", "h", "radius", "bw", "pw", "cw", "ch",
            "rh", "snap", "snap_off", "off0")

# Scaled but NOT rounded: a font size is not a box, and rounding one makes
# the text render wider than the width layout fitted it to. The LINE BOX
# (h/rh) is the thing that must round like every other rasterizer, and does.
# Pinned by tests/test_ui_scale.py.
_EXACT_KEYS = ("size",)

# Pixel geometry that arrives as a LIST of numbers (scaled elementwise).
_PX_LIST_KEYS = ("snaps",)

# Pixel values that live INSIDE nested style dicts. These are the reason
# this is an explicit table rather than a recursive walk over anything
# numeric: `hover` also carries colours, and a slider's min/max/value/
# marks/ranges are domain values that must never be touched.
_NESTED = ("hover",)
_NESTED_PX_KEYS = ("bw", "radius")

# Never scaled: iw/ih are the physical bitmap dims (the boundary itself),
# min/max/value/marks/ranges are slider domain values, a/a1/a2 are alphas,
# v is a content version.
#
# The tables are keyed on NAME ALONE, so a name must mean the same thing on
# every node type before you add it here -- a menu's row height is "rh" and
# not "ih" for exactly that reason (full story in layout.py).


def scale_scene(nodes):
    """Convert a laid-out scene from logical to physical, in place."""
    if _scale == 1.0:
        return nodes
    for node in nodes:
        for key in _PX_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = px(v)
        for key in _EXACT_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = v * _scale
        for key in _PX_LIST_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = [px(x) for x in v]
        for nest in _NESTED:
            d = node.get(nest)
            if not isinstance(d, dict):
                continue
            if not any(d.get(k) is not None for k in _NESTED_PX_KEYS):
                continue
            # Copied, not mutated: hover dicts are frequently shared module
            # constants (layout.py's region default, theme-ish literals in
            # settings.py), and scaling one in place would compound on it
            # every single frame.
            d = dict(d)
            for key in _NESTED_PX_KEYS:
                v = d.get(key)
                if v is not None:
                    d[key] = px(v)
            node[nest] = d
    return nodes
