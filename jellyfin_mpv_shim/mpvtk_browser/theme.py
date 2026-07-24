"""Design tokens for the mpvtk browser — theme-driven (see themes.py).

Colours are bare ``"rrggbb"`` (what mpvtk widget ``bg``/``color`` fields want);
use :func:`rgb` for a PIL ``(r, g, b)`` tuple. The palette constants below are
NOT literals: :func:`apply` copies the chosen theme's palette onto this module
at startup, and every consumer reads ``theme.<NAME>`` dynamically, so one call
repoints the whole UI.
"""

from . import themes

_active = None


def apply(name):
    """Copy a theme's palette onto this module's globals. Returns the theme."""
    global _active
    _active = themes.get(name)
    g = globals()
    for key, value in _active["palette"].items():
        g[key] = value
    return _active


def active():
    """The currently applied theme dict (palette + glow/size knobs)."""
    return _active


def apply_to_toolkit(glow=False):
    """Hand this palette (and the theme's ``glow`` flag) to mpvtk, so the
    toolkit's own accented bits — a checkbox fill, a hover ring, a focused
    textbox border, the slider — match the app's accent, and the renderer
    knows whether to draw the themed title/selection glow."""
    from ..mpvtk import theme as tk

    tk.set_accent(ACCENT, hover=ACCENT_HOVER, soft=ACCENT_SOFT,
                  on_accent=ACCENT_FG, glow=glow)


def rgb(hexstr, alpha=None):
    """``"rrggbb"`` -> ``(r, g, b)``; with ``alpha`` -> ``(r, g, b, a)``."""
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha) if alpha is not None else (r, g, b)


# Initialise the palette globals to the default theme so this module is fully
# populated at import time; startup re-applies the user's chosen theme (see the
# browser's UserInterface) before the first render.
apply("default")
