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
    "accent_buttons": False,  # accent-bordered top-bar buttons
    # Where a carousel's page buttons live. "header" is jellyfin-web's design
    # and the default: a flat pair in the section heading, clear of the
    # artwork, with a disabled state. "overlay" floats round translucent
    # bitmaps over the strip's edges — see tile_renderer.hscroll_row.
    "arrow_mode": "header",
    # Fill of the round carousel page arrow (see tile_renderer._arrow_bitmap).
    # Neutral dark grey rather than the palette's BUTTON_BG: the arrow floats
    # ON artwork, not in chrome, so it wants the same "player overlay
    # furniture" treatment as the HUD's Skip Intro chip (hud._SKIP_BG /
    # _SKIP_ALPHA) — a themed fill reads as a coloured chip stuck to a poster.
    "arrow_bg": "202020",
    "arrow_alpha": 180,
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
    "accent_buttons": True,
    # Big covers with the controls ON them, jellyfin-web's TV layout rather
    # than its web one — the reason the composited-bitmap path exists.
    "arrow_mode": "overlay",
    # Nebula does tint its arrows — the violet wash is part of the look, and
    # over its own deep-violet artwork frames it still reads as furniture.
    "arrow_bg": "2a1656",
    "arrow_alpha": 165,
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
