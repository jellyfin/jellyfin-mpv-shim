"""Design tokens for the mpvtk browser.

Ported from the Tk browser's ``theme.py`` palette (the ttk styling is
dropped — mpvtk widgets take colours directly). Colours are stored as
bare ``"rrggbb"`` (what mpvtk widget ``bg``/``color`` fields want); use
:func:`rgb` when a PIL drawing needs an ``(r, g, b)`` tuple.
"""


from . import themes

# The theme in force, as a dict from themes.py. Set by apply(); active() is
# how the rest of the browser reads the non-colour parts (glow, rounded
# cards, cover/heading sizes).
_active = None


def apply(name):
    """Copy a theme's palette onto this module's globals. Returns the theme.

    Called once at startup (MpvtkBrowser.__init__) before anything is built,
    so every consumer that reads ``theme.X`` gets the chosen theme's value
    without knowing a theme system exists. ``default`` reproduces the stock
    palette exactly, so an untouched install is unaffected."""
    global _active
    _active = themes.get(name)
    g = globals()
    for key, value in _active["palette"].items():
        g[key] = value
    return _active


def active():
    """The theme dict in force (None before apply())."""
    return _active


def chrome_button_style():
    """Styling for the app's chrome buttons — the top bar and the Settings
    tabs — when the active theme asks for accented ones: a themed fill, an
    accent border, rounder corners, and a glow on hover.

    Empty for themes that do not ask, so a caller can splat it
    unconditionally and the stock button styling is left alone. The hover
    glow is itself only drawn when the theme turned the glow on.
    """
    if not (_active or {}).get("accent_buttons"):
        return {}
    return {"bg": BUTTON_BG, "border": ACCENT, "border_w": 1, "radius": 9,
            "hover": {"fill": BUTTON_ACTIVE, "glow": True}}


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


# Core palette (Jellyfin-ish dark), matching the Tk browser 1:1.
WINDOW_BG = "15171a"
CARD_BG = "1e2024"
PANEL_BG = "26292f"
PLACEHOLDER_BG = "2a2d33"
BUTTON_BG = "2e3138"
BUTTON_ACTIVE = "3a3e46"
ENTRY_BG = "2a2d33"
BORDER = "3a3d42"
TEXT_FG = "e8e8e8"
SUBTLE_FG = "9aa0a6"
# There is exactly ONE blue. Anything that reads as "the app's colour" —
# primary buttons, selection, hover rings, progress, active tabs — uses this
# family and nothing else; a second unrelated blue makes the UI look
# assembled from parts. ACCENT_HOVER is the same hue lightened, ACCENT_SOFT
# the same hue darkened for fills that sit behind text.
ACCENT = "00a4dc"
ACCENT_HOVER = "1cb6e8"
ACCENT_SOFT = "0a3a4d"
# Accent fills always carry white text — dark-on-blue reads as disabled.
ACCENT_FG = "ffffff"
FAV_RED = "e0264b"
OK_GREEN = "7bd88f"      # "Connected" / "active" badges
WARN_AMBER = "e5c07b"

# Semantic extras used by baked strip decorations.
PROGRESS_TRACK = "000000"   # drawn at ~78% alpha behind the resume bar
WATCHED_GREEN = "28a046"
