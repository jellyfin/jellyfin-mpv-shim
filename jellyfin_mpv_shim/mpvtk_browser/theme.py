"""Design tokens for the mpvtk browser.

Ported from the Tk browser's ``theme.py`` palette (the ttk styling is
dropped — mpvtk widgets take colours directly). Colours are stored as
bare ``"rrggbb"`` (what mpvtk widget ``bg``/``color`` fields want); use
:func:`rgb` when a PIL drawing needs an ``(r, g, b)`` tuple.

``theme.ACCENT`` and friends are **not module globals**. They are served from
:data:`_palette` through the module ``__getattr__`` below, and the difference
matters now that themes come from user-editable JSON:

* Applying a theme no longer *writes* to this module. It used to copy a
  theme's palette into ``globals()`` in a loop, so a theme could define — or
  redefine — anything in this namespace. A file with ``"rgb": "ff0000"``
  would have replaced the :func:`rgb` function; a typo like ``ACCNET`` would
  have quietly created a new global while ``ACCENT`` kept its old value, so
  the theme just looked subtly wrong with nothing to point at.
* An unknown name is now an ``AttributeError`` at the point of use rather
  than a silently stale colour. Real module attributes (the functions here)
  are found first and can never be shadowed by a palette key.
* A theme that omits a colour gets the default's, because
  :func:`themes.resolve` merges over ``themes.DEFAULT`` before it ever gets
  here. Switching themes cannot leave a colour behind from the previous one.

The ~190 ``theme.X`` reads across the browser are unchanged by all this: the
lookup still works exactly as it looks.
"""


from . import themes

# The theme in force, as a dict from themes.py. Set by apply(); active() is
# how the rest of the browser reads the non-colour parts (glow, rounded
# cards, cover/heading sizes, carousel arrow mode).
_active = None

#: The live colour values, keyed as the palette names. Seeded with the stock
#: palette so a consumer that reads a colour before apply() runs — which is
#: every import-time default argument — gets the documented value rather than
#: an error.
_palette = dict(themes.DEFAULT["palette"])


def __getattr__(name):
    """Serve palette colours as module attributes.

    Only reached when normal attribute lookup fails (PEP 562), so everything
    actually defined in this module wins — which is what stops a theme file
    from redefining the functions here.
    """
    try:
        return _palette[name]
    except KeyError:
        raise AttributeError(
            "module %r has no attribute %r. Palette colours are %s"
            % (__name__, name, ", ".join(sorted(_palette)))) from None


def __dir__():
    return sorted(list(globals()) + list(_palette))


def apply(name):
    """Make a theme active. Returns it.

    Called once at startup (MpvtkBrowser.__init__) before anything is built,
    so every consumer that reads ``theme.X`` gets the chosen theme's value
    without knowing a theme system exists. ``default`` reproduces the stock
    palette exactly, so an untouched install is unaffected."""
    global _active
    _active = themes.get(name)
    # Replaced wholesale rather than updated in place: an update would leave
    # any colour the new theme does not mention set to the OLD theme's value.
    # (themes.resolve already fills every key from the default, so this is
    # belt and braces — but it is the invariant that matters, not the layer
    # that happens to enforce it.)
    _palette.clear()
    _palette.update(themes.DEFAULT["palette"])
    _palette.update(_active.get("palette") or {})
    return _active


def active():
    """The theme dict in force (None before apply())."""
    return _active


def window_gradient():
    """Stops for the page background, or None for a flat fill."""
    return (_active or {}).get("window_gradient")


def topbar_gradient():
    """Stops for the top bar, or None for a flat fill."""
    return (_active or {}).get("topbar_gradient")


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
    return {"bg": _palette["BUTTON_BG"], "border": _palette["ACCENT"],
            "border_w": 1, "radius": 9,
            "hover": {"fill": _palette["BUTTON_ACTIVE"], "glow": True}}


def toolkit_tokens():
    """This palette expressed as mpvtk's design tokens.

    The two vocabularies are deliberately different. Ours is the *app's*
    (CARD_BG, SUBTLE_FG, WATCHED_GREEN) and mpvtk's is a widget toolkit's
    (CONTROL_SUNKEN, ON_SURFACE_MUTED); this is the one place that knows
    both. Before it existed, mpvtk was handed a single accent and hardcoded
    every other colour it drew, so a text field, a dropdown, its popup, a
    scrollbar and a tooltip were fixed shades of grey no theme could reach.

    Three mpvtk tokens have no equivalent here and are left at their stock
    values on purpose — SCRIM, CHIP_BG, CHIP_FG are drawn over *video*, and
    a light theme must not turn the playback HUD white.
    """
    return {
        "ON_SURFACE": _palette["TEXT_FG"],
        "ON_SURFACE_MUTED": _palette["SUBTLE_FG"],
        # Between muted and the background: placeholder and disabled text.
        "ON_SURFACE_FAINT": mix(_palette["SUBTLE_FG"],
                                 _palette["WINDOW_BG"], 0.45),
        "ON_SURFACE_STRONG": _lift(_palette["TEXT_FG"]),
        "CONTROL_BG": _palette["BUTTON_BG"],
        "CONTROL_HOVER": _palette["BUTTON_ACTIVE"],
        "CONTROL_SUNKEN": _palette["ENTRY_BG"],
        "OUTLINE": _palette["BORDER"],
        "OUTLINE_STRONG": _palette["BORDER"],
        "POPUP_BG": _palette["PANEL_BG"],
        "OVERLAY_BG": _palette["CARD_BG"],
        "SCROLLBAR_THUMB": mix(_palette["BORDER"], _palette["TEXT_FG"], 0.25),
        "SCROLLBAR_THUMB_ACTIVE": mix(_palette["BORDER"],
                                       _palette["TEXT_FG"], 0.6),
        "SELECTION": _palette["ACCENT_SOFT"],
        "ACCENT": _palette["ACCENT"],
        "ACCENT_HOVER": _palette["ACCENT_HOVER"],
        "ACCENT_SOFT": _palette["ACCENT_SOFT"],
        "ON_ACCENT": _palette["ACCENT_FG"],
        # Over video. None here means "follow the accent", which is mpvtk's
        # own default, so a theme with no opinion gets one accent everywhere.
        "ACCENT_ON_VIDEO": (_active or {}).get("hud_accent"),
    }


def heading_size():
    """The carousel section-heading size.

    The theme's ``heading_size`` if it pins one, else the scale's HEADING
    tier -- so a heading follows the user's text-size multiplier unless a
    theme has deliberately taken it over.
    """
    from ..mpvtk import theme as tk

    return (_active or {}).get("heading_size") or tk.size("HEADING")


def apply_to_toolkit(glow=False):
    """Hand this palette (and the theme's ``glow`` flag) to mpvtk, so every
    widget default and every control the renderer draws for itself follows
    the app's theme rather than a hardcoded dark palette."""
    from ..conf import settings
    from ..mpvtk import theme as tk

    tk.set_tokens(glow=glow, **toolkit_tokens())
    # Type scale as well as palette. The user's multiplier rides on the
    # theme's base rather than being applied per-size later, so every tier
    # keeps its proportion to every other -- which is the property that
    # makes a scale a scale.
    base = (_active or {}).get("base_size") or tk.DEFAULT_BASE_SIZE
    try:
        factor = float(getattr(settings, "ui_text_scale", 1.0) or 1.0)
    except (TypeError, ValueError):
        factor = 1.0
    tk.set_type_scale(base * (factor if factor > 0 else 1.0))


def mix(a, b, t):
    """``a`` moved ``t`` of the way toward ``b``.

    Public because views need it: a colour derived from the palette follows
    the theme, while the hardcoded hex it replaces does not. Prefer a real
    palette key where one fits — this is for the shades between them (a
    disabled label, a warning wash) that do not deserve their own token.
    """
    ca, cb = rgb(a), rgb(b)
    return "%02x%02x%02x" % tuple(
        int(round(x + (y - x) * t)) for x, y in zip(ca, cb))


def _lift(colour):
    """A touch more contrast than ``colour``, for hover emphasis: toward
    white on a dark theme, toward black on a light one."""
    r, g, b = rgb(colour)
    lum = (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0
    return mix(colour, "000000" if lum > 0.6 else "ffffff", 0.5)


def rgb(hexstr, alpha=None):
    """``"rrggbb"`` -> ``(r, g, b)``; with ``alpha`` -> ``(r, g, b, a)``."""
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha) if alpha is not None else (r, g, b)
