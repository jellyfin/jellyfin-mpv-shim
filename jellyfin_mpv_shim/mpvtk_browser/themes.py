"""Selectable UI themes for the mpvtk browser.

A theme is a plain data bundle: the colour palette (see ``theme.py``), the mpv
browse ``background-color``, a glow toggle, and a couple of size multipliers.
``theme.apply(name)`` copies a theme's palette onto the ``theme`` module at
startup; ``player`` / ``strips`` / the renderer read the rest.

Adding a theme is adding one entry here — nothing else hardcodes a colour. The
built-in ``default`` reproduces the stock look exactly, so the feature is
strictly opt-in: an untouched install renders identically to before.
"""

# --- the stock Jellyfin-ish dark look (unchanged upstream default) ----------
DEFAULT = {
    "name": "Default",
    "palette": {
        "WINDOW_BG": "15171a", "CARD_BG": "1e2024", "PANEL_BG": "26292f",
        "PLACEHOLDER_BG": "2a2d33", "BUTTON_BG": "2e3138",
        "BUTTON_ACTIVE": "3a3e46", "ENTRY_BG": "2a2d33", "BORDER": "3a3d42",
        "TEXT_FG": "e8e8e8", "SUBTLE_FG": "9aa0a6", "ACCENT": "00a4dc",
        "ACCENT_HOVER": "1cb6e8", "ACCENT_SOFT": "0a3a4d", "ACCENT_FG": "ffffff",
        "FAV_RED": "e0264b", "OK_GREEN": "7bd88f", "WARN_AMBER": "e5c07b",
        "PROGRESS_TRACK": "000000", "WATCHED_GREEN": "28a046",
    },
    "browse_bg": "#141414",
    "glow": False,       # blurred accent halo behind titles + on card selection
    "rounded": False,    # rounded cover cards + cover-crop (False = stock square/letterbox)
    "poster_scale": 1.0,  # tile-geometry multiplier
    "heading_size": 24,   # carousel section-title font size
    "tile_landscape": (240, 135),  # (w, h) of the library/landscape tile
    "tile_title_size": None,  # tile caption font; None = stock (scales w/ cover)
    "tile_sub_size": None,
}

# --- "Nebula": deep-violet, glowing, bigger covers --------------------------
NEBULA = {
    "name": "Nebula",
    "palette": {
        "WINDOW_BG": "0c0620", "CARD_BG": "160a2e", "PANEL_BG": "1e0f3d",
        "PLACEHOLDER_BG": "17102b", "BUTTON_BG": "2a1656",
        "BUTTON_ACTIVE": "3d2170", "ENTRY_BG": "1e0f3d", "BORDER": "2e2550",
        "TEXT_FG": "ece4ff", "SUBTLE_FG": "a99cc8", "ACCENT": "a855f7",
        "ACCENT_HOVER": "c084fc", "ACCENT_SOFT": "3a1a6e", "ACCENT_FG": "ffffff",
        "FAV_RED": "e0264b", "OK_GREEN": "7bd88f", "WARN_AMBER": "e5c07b",
        "PROGRESS_TRACK": "000000", "WATCHED_GREEN": "28a046",
    },
    "browse_bg": "#0c0620",
    "glow": True,
    "rounded": True,
    "poster_scale": 1.4,
    "heading_size": 30,
    "tile_landscape": (380, 248),
    # Caption font is fixed (does NOT grow with the bigger covers) so long
    # titles fit before they clip — jellyfin-web-style big art, modest labels.
    "tile_title_size": 13,
    "tile_sub_size": 11,
}

THEMES = {"default": DEFAULT, "nebula": NEBULA}


def get(name):
    """A theme by id, falling back to ``default`` for unknown/None."""
    return THEMES.get((name or "default").lower(), DEFAULT)


def choices():
    """(id, label) pairs for the settings dropdown."""
    return [(k, v["name"]) for k, v in THEMES.items()]
