"""UI scale factor: the logical/physical pixel boundary.

**The rule: every number in Python view code is logical.** The only code
that thinks in physical pixels is bitmap rasterization -- because the
renderer never resamples (see ``rawimage``: "images must be rasterized at
their display size") -- plus the renderer/Lua side itself.

So views author at 1x and stay readable, `layout()` runs in logical
space, and ``scale_scene()`` converts the finished scene to physical on
the way out. ``app`` hands views a *logical* ``size``, which is what
keeps derived math (``hud``'s responsive ``sz()``, anything computed off
the surface width) logical automatically instead of double-scaling.

Images are the leak in that abstraction, so they get an explicit
boundary: a producer converts with ``raster()`` and declares BOTH the
physical bitmap size and the logical footprint it was built for. ``Image``
checks the two agree, which turns "a producer forgot to scale" from a
silently cropped or sheared poster into a loud failure at construction.

``px()`` is deliberately the single rounding rule shared by layout and
every rasterizer. If the two ever rounded differently -- 150*1.5 to 225
on one side and 224 on the other -- the mismatch lands in overlay-add's
stride and shears the image, which is a genuinely miserable bug to read
backwards from a screenshot.
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
_PX_KEYS = ("x", "y", "w", "h", "size", "radius", "bw", "pw", "cw", "ch",
            "rh", "snap", "snap_off")

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
# These lists are keyed on name alone, which is only safe while a key means
# the same thing on every node type. Menu's row height used to ship as "ih"
# and silently inherited the img exclusion -- if you add a field, make sure
# its name isn't already spoken for.


def scale_scene(nodes):
    """Convert a laid-out scene from logical to physical, in place."""
    if _scale == 1.0:
        return nodes
    for node in nodes:
        for key in _PX_KEYS:
            v = node.get(key)
            if v is not None:
                node[key] = px(v)
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
