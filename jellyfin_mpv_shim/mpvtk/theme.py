"""The toolkit's accent colour.

mpvtk draws a handful of things in "the app's colour": a checkbox's fill, a
progress bar, a tile's hover ring, and — renderer-side — the focused
textbox border, the open dropdown's border and the slider thumb. Those used
to be a hardcoded ``7aa2f7`` scattered across widgets.py, layout.py and
renderer.lua, which meant an embedding app with its own palette ended up
with two unrelated blues on screen.

Call :func:`set_accent` once at startup; widgets and the layout engine read
these at build time, and :meth:`MpvtkApp.push_theme` forwards them to the
renderer.

Colours are bare ``"rrggbb"``, matching every other colour field in mpvtk.
"""

DEFAULT_ACCENT = "7aa2f7"

# Resolved palette. ACCENT is the colour itself; HOVER is it lightened, for
# the hovered state of accent-filled controls; SOFT is it darkened, for
# fills that sit *behind* text (selected table rows, banners) where the full
# accent would drown the label.
ACCENT = DEFAULT_ACCENT
HOVER = None      # set by set_accent
SOFT = None
# Colour drawn on top of an ACCENT fill.
ON_ACCENT = "ffffff"
# Whether the embedding app wants the themed glow (titles + card selection).
GLOW = False


def _rgb(hexstr):
    h = (hexstr or "").lstrip("#")
    if len(h) != 6:
        raise ValueError("expected rrggbb, got %r" % (hexstr,))
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _hex(rgb):
    return "%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def lighten(hexstr, amount=0.18):
    """Move a colour toward white by ``amount`` (0..1)."""
    r, g, b = _rgb(hexstr)
    return _hex((r + (255 - r) * amount,
                 g + (255 - g) * amount,
                 b + (255 - b) * amount))


def darken(hexstr, amount=0.72):
    """Move a colour toward black by ``amount`` (0..1)."""
    r, g, b = _rgb(hexstr)
    f = 1.0 - amount
    return _hex((r * f, g * f, b * f))


def readable_on(hexstr):
    """Black or white, whichever reads better on ``hexstr``. Uses relative
    luminance rather than a naive average — a saturated blue is much darker
    than its mean channel suggests."""
    r, g, b = (c / 255.0 for c in _rgb(hexstr))
    lum = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "101010" if lum > 0.6 else "ffffff"


def set_accent(accent, hover=None, soft=None, on_accent=None, glow=None):
    """Set the toolkit accent. ``hover``/``soft``/``on_accent`` default to
    sensible derivations, so most callers pass one colour. ``glow`` toggles the
    themed title/selection glow (forwarded to the renderer)."""
    global ACCENT, HOVER, SOFT, ON_ACCENT, GLOW
    ACCENT = (accent or DEFAULT_ACCENT).lstrip("#")
    HOVER = (hover or lighten(ACCENT)).lstrip("#")
    SOFT = (soft or darken(ACCENT)).lstrip("#")
    ON_ACCENT = (on_accent or readable_on(ACCENT)).lstrip("#")
    if glow is not None:
        GLOW = bool(glow)
    return palette()


def palette():
    """The resolved palette, as pushed to the renderer."""
    return {"accent": ACCENT, "hover": HOVER, "soft": SOFT,
            "on_accent": ON_ACCENT, "glow": GLOW}


set_accent(DEFAULT_ACCENT)
