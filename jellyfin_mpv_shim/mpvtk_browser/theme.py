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


def apply_to_toolkit(glow=False):
    """Hand this palette (and the theme's ``glow`` flag) to mpvtk, so the
    toolkit's own accented bits — a checkbox fill, a hover ring, a focused
    textbox border, the slider — match the app's accent, and the renderer
    knows whether to draw the themed title/selection glow."""
    from ..mpvtk import theme as tk

    tk.set_accent(_palette["ACCENT"], hover=_palette["ACCENT_HOVER"],
                  soft=_palette["ACCENT_SOFT"],
                  on_accent=_palette["ACCENT_FG"], glow=glow)


def rgb(hexstr, alpha=None):
    """``"rrggbb"`` -> ``(r, g, b)``; with ``alpha`` -> ``(r, g, b, a)``."""
    h = hexstr.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (r, g, b, alpha) if alpha is not None else (r, g, b)
