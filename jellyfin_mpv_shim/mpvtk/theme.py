"""The toolkit's design tokens.

mpvtk used to know exactly one colour — the app's accent — and hardcode
everything else. That made it themeable in the narrow sense that a checkbox
fill and a hover ring could follow the app, and not at all in the sense that
matters: a button, a dropdown, a text field, a scrollbar and a tooltip were
fixed shades of grey chosen for a dark UI, so an app with a light palette got
near-white text on a near-white page.

So: a small set of **semantic tokens**, defaulting to exactly the greys that
were hardcoded before. An embedding app calls :func:`set_tokens` once at
startup (or again later — see below) and every widget and renderer-drawn
control follows.

Two rules make this work:

* **Semantic, not literal.** ``ON_SURFACE_MUTED``, not ``grey_aaaaaa``. There
  are deliberately fewer tokens than there were literals; four near-identical
  greys collapsing into one is the point, because a theme author cannot reason
  about seventeen shades and will not try.
* **Two surfaces.** Everything named ``*_SURFACE*`` is chrome drawn on the
  app's own background and follows the theme. The ``SCRIM_*`` / ``CHIP_*``
  tokens are for things drawn over **video** — the playback HUD, the Skip
  Intro chip, the cast backdrop. Those stay dark whatever the theme does,
  because a white HUD over a dark film is wrong no matter how light the rest
  of the app is. jellyfin-web keeps its player OSD dark for the same reason.

Values are bare ``"rrggbb"``, matching every other colour field in mpvtk.
They are read through the module ``__getattr__`` rather than being module
globals, so a theme can never redefine the functions here and an unknown name
raises instead of silently resolving.
"""

DEFAULT_ACCENT = "7aa2f7"

#: Semantic token -> stock value. The defaults reproduce the greys that were
#: hardcoded across widgets.py, layout.py and renderer.lua, so an app that
#: never calls set_tokens() renders as it always did.
#:
#: The wire/Lua name of each token is its lowercase form; see :func:`palette`.
TOKENS = {
    # --- text and icons on app chrome -------------------------------------
    "ON_SURFACE": "eeeeee",         # body text, button labels, list rows
    "ON_SURFACE_STRONG": "ffffff",  # hover emphasis, the spatial-nav ring
    # Secondary text, dropdown glyphs, table headers, tooltip text. Was four
    # shades (dddddd / cccccc / aaaaaa / 9a9a9a) that no one could have told
    # apart on purpose.
    "ON_SURFACE_MUTED": "aaaaaa",
    "ON_SURFACE_FAINT": "777777",   # placeholder text, disabled glyphs

    # --- chrome surfaces ---------------------------------------------------
    "CONTROL_BG": "333333",         # button fill, popup row hover
    "CONTROL_HOVER": "4a4a4a",      # button hover fill
    # Recessed: text fields, the closed dropdown, slider and progress tracks,
    # an unchecked checkbox.
    "CONTROL_SUNKEN": "2a2a2a",
    "OUTLINE": "444444",            # field and dropdown borders
    "OUTLINE_STRONG": "555555",     # checkbox border, popup border
    "POPUP_BG": "222222",           # open dropdown, context menu
    "OVERLAY_BG": "111111",         # tooltip, and anything floating above it
    "SCROLLBAR_THUMB": "666666",
    "SCROLLBAR_THUMB_ACTIVE": "bbbbbb",
    "SELECTION": "3d59a1",          # textbox selection wash

    # --- the app's colour --------------------------------------------------
    "ACCENT": DEFAULT_ACCENT,
    "ACCENT_HOVER": None,           # None -> derived by lighten()
    "ACCENT_SOFT": None,            # None -> derived by darken()
    "ON_ACCENT": None,              # None -> derived by readable_on()

    # --- drawn over VIDEO, not over the app ---------------------------------
    # Deliberately not themed by default: see the module docstring.
    # The accent as drawn ON VIDEO -- the seek bar's fill, its chapter
    # marks. Defaults to ACCENT, because one accent everywhere is what makes
    # a theme read as a theme. It is separable because the two jobs differ:
    # over app chrome the accent only has to beat a known background, over
    # video it has to stay visible against an unknown and moving one. (This
    # is where we diverge from jellyfin-web, which hardcodes its player
    # slider to Jellyfin blue and lets no theme near it.)
    "ACCENT_ON_VIDEO": None,        # None -> ACCENT
    "SCRIM": "000000",              # HUD gradients, cast backdrop wash
    "CHIP_BG": "202020",            # Skip Intro / Credits button
    "CHIP_BG_HOVER": "3a3a3a",
    "CHIP_FG": "ffffff",
}

#: Aliases kept so existing reads (``theme.HOVER``, ``theme.SOFT``) keep
#: working; they were the accent's derived pair before tokens existed.
_ALIASES = {"HOVER": "ACCENT_HOVER", "SOFT": "ACCENT_SOFT"}

_tokens = {}

# Whether the renderer draws the themed glow (a blurred accent halo behind
# bold titles and around the selected card). Not a colour, so not a token.
# Off unless an app asks for it, so the stock look is unchanged.
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


def set_tokens(glow=None, **tokens):
    """Replace the token set. Unnamed tokens fall back to their stock value.

    Replaced wholesale rather than updated in place, which is what makes a
    **runtime** theme swap safe: a token the new theme does not mention resets
    to the default instead of keeping the old theme's value. Callers pass
    whatever subset they have opinions about.

    Names are case-insensitive so an app can pass the lowercase wire names
    straight back in.
    """
    global GLOW
    resolved = {k: v for k, v in TOKENS.items() if v is not None}
    for key, value in tokens.items():
        name = key.upper()
        name = _ALIASES.get(name, name)
        if name not in TOKENS:
            raise KeyError("unknown mpvtk theme token %r" % (key,))
        if value is not None:
            resolved[name] = str(value).lstrip("#")
    # The accent's companions are derived when not given, so an app that has
    # only one colour still gets a coherent set.
    accent = resolved["ACCENT"]
    resolved.setdefault("ACCENT_HOVER", lighten(accent))
    resolved.setdefault("ACCENT_SOFT", darken(accent))
    resolved.setdefault("ON_ACCENT", readable_on(accent))
    resolved.setdefault("ACCENT_ON_VIDEO", resolved["ACCENT"])
    for name in ("ACCENT_HOVER", "ACCENT_SOFT", "ON_ACCENT",
                 "ACCENT_ON_VIDEO"):
        if name not in resolved:
            resolved[name] = None
    _tokens.clear()
    _tokens.update(resolved)
    if glow is not None:
        GLOW = bool(glow)
    return palette()


def set_accent(accent, hover=None, soft=None, on_accent=None, glow=None):
    """Set just the accent, leaving every other token alone.

    The original entry point, and still the right one for an app whose only
    opinion is its brand colour. ``hover``/``soft``/``on_accent`` default to
    sensible derivations.
    """
    keep = {k: v for k, v in _tokens.items()
            if k not in ("ACCENT", "ACCENT_HOVER", "ACCENT_SOFT", "ON_ACCENT",
                         "ACCENT_ON_VIDEO")}
    return set_tokens(glow=glow, ACCENT=(accent or DEFAULT_ACCENT),
                      ACCENT_HOVER=hover, ACCENT_SOFT=soft,
                      ON_ACCENT=on_accent, **keep)


def palette():
    """The resolved tokens as the renderer wants them: lowercase keys, plus
    the glow flag. This is the ``mpvtk-theme`` message payload."""
    out = {k.lower(): v for k, v in _tokens.items()}
    out["glow"] = GLOW
    return out


def __getattr__(name):
    """Serve tokens as module attributes (PEP 562).

    Only reached when normal lookup fails, so the functions above always win
    and no caller can shadow them by naming a token after one.
    """
    key = _ALIASES.get(name, name)
    if key in _tokens:
        return _tokens[key]
    raise AttributeError(
        "module %r has no attribute %r. Tokens are %s"
        % (__name__, name, ", ".join(sorted(_tokens)))) from None


def __dir__():
    return sorted(list(globals()) + list(_tokens) + list(_ALIASES))


set_tokens()
