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

#: The type scale, as multiples of the base size.
#:
#: **Surveyed, not invented.** Every explicit ``size=`` in the app was
#: counted (237 call sites) and grouped by what the text actually is, and
#: these ratios are the result: with a base of 17 they reproduce 236 of
#: those 237 to within a pixel. ``HEADING`` landing on 24 -- which is what
#: ``heading_size`` has always been -- is the check that the ratios are
#: real rather than fitted.
#:
#: The tiers are named for the JOB, like the colour tokens above and for
#: the same reason: an author can pick "this is a caption" and cannot pick
#: between 13 and 14. That was the actual problem -- 161 of the 237 sizes
#: sat in the 3px band 14-18 with no rule saying which.
TYPE_SCALE = {
    "MICRO": 0.70,     # guide badges ("HD"), the densest chrome
    "CAPTION": 0.80,   # help text under a settings row
    "SMALL": 0.88,     # dense body: guide, music, comic, reader
    "NORMAL": 1.00,    # body text, and every control label
    "LARGE": 1.12,     # meta and secondary lines on a detail page
    "TITLE": 1.30,     # dialog titles
    "HEADING": 1.42,   # carousel section headings
    "PAGE": 1.53,      # page titles
    "HERO": 1.70,      # onboarding: "Connect to Jellyfin"
}

#: The base every tier is a multiple of, in logical px.
#:
#: 17 because that is what the app was already written at: the survey's
#: best fit, and the size six of the shim's own buttons pass explicitly
#: when their author bothered to pick one. The widget defaults were 20 --
#: a value **no call site ever chose**, arrived at 194 times by not
#: passing one -- which is why unstyled controls read a whole tier larger
#: than the text beside them. **[iw]**: "I'd probably default to the size
#: we use for buttons as Normal."
DEFAULT_BASE_SIZE = 17

_base_size = DEFAULT_BASE_SIZE

#: Nothing renders below this, whatever a tier or a call site asked for.
#: 0 = no floor, which is stock.
#:
#: For low vision: a scale multiplies everything, so the smallest text
#: stays the smallest text and a guide badge at 0.70x base is still the
#: hardest thing on screen to read. A floor is the other control -- it
#: compresses the bottom of the scale instead of moving all of it, which
#: is what somebody who cannot read 12px actually wants.
_min_size = 0

#: The user's text multiplier, applied to EVERY size the toolkit resolves
#: -- tiers and explicit sizes alike.
#:
#: Not folded into the base, which is what it did first: that scaled the
#: tiers and left the several hundred call sites that still pass a literal
#: exactly where they were, so turning text up enlarged the controls and
#: not the body text -- recreating the mismatch the scale exists to fix,
#: in the other direction. The base is the theme's design; this is the
#: user's preference about all of it.
_text_factor = 1.0


class Px(int):
    """A size that has already been resolved by :func:`text_size`.

    The user's multiplier has to be applied **exactly once**. A composite
    widget resolves its own size and then hands the number to a child --
    `Button` builds a `Text` and an `Icon`, `Checkbox` builds a `Text`,
    `Form` builds a `Grid` which builds a `Text` per cell -- and each of
    those resolved again. At "150%" a button label came out 39px against
    body copy at 20 (a Form cell, 76px), which is the size mismatch this
    scale exists to remove, arriving from inside it.

    An int subclass rather than a flag, so it stores, compares and
    arithmetics exactly like the number it replaces. **Arithmetic returns
    a plain int**, which is the right default: `int(size * 0.95)` is a
    NEW size derived from this one, and deriving is not resolving.
    """

    __slots__ = ()


def set_type_scale(base=None, minimum=None, factor=None):
    """Set the base size every tier derives from. ``None`` restores stock.

    Wholesale like :func:`set_tokens`, and for the same reason: a theme
    that says nothing about type must get the default back rather than
    keep the last theme's.

    ``minimum`` floors every size the toolkit resolves and ``factor``
    multiplies it; see ``_min_size`` and ``_text_factor``.
    """
    global _base_size, _min_size, _text_factor
    try:
        value = float(DEFAULT_BASE_SIZE if base is None else base)
    except (TypeError, ValueError):
        value = float(DEFAULT_BASE_SIZE)
    # A base of 0 would render the whole UI invisible and a negative one is
    # meaningless; both are reachable from a hand-edited theme file.
    _base_size = value if value > 0 else float(DEFAULT_BASE_SIZE)
    try:
        floor = float(0 if minimum is None else minimum)
    except (TypeError, ValueError):
        floor = 0.0
    # A floor above the largest tier would flatten the whole scale into one
    # size, which is not a readability aid, it is a broken UI. Capped at the
    # base so the tiers above it keep their spread.
    try:
        mult = float(1.0 if factor is None else factor)
    except (TypeError, ValueError):
        mult = 1.0
    _text_factor = mult if mult > 0 else 1.0
    # Capped at the LARGEST tier, not at the base. Capping at the base was
    # the first version and it quietly swallowed every useful value: with a
    # base of 17 a floor of 18 came out as 17, so the setting appeared to do
    # nothing at all to buttons or body text -- which is exactly what it is
    # for. A floor is allowed to raise everything up to the top of the
    # scale; beyond that it is a hand-edited value with no meaning.
    _biggest = _base_size * _text_factor * max(TYPE_SCALE.values())
    _min_size = min(max(floor, 0.0), _biggest)
    return _base_size



def size_of_tier(tier):
    """The px size for a named tier. Unknown tiers are an error, not a
    silent default -- a typo'd tier would otherwise render as body text
    and look almost right."""
    try:
        ratio = TYPE_SCALE[str(tier).upper()]
    except KeyError:
        raise KeyError("unknown type tier %r. Tiers are %s"
                       % (tier, ", ".join(TYPE_SCALE))) from None
    # Rounded, because a font size that is not a whole number of logical px
    # gives layout a fractional line height to divide the page by. The
    # logical->physical conversion deliberately does NOT round (see
    # scaling._EXACT_KEYS); this is the other end of that.
    # Never below 1: a zero font size divides by zero in layout's line
    # fitting, and `ui_text_scale` is a documented float, so 0.05 in a
    # hand-edited config reaches here even though the drop-down stops at
    # 0.75. The base is guarded the same way, for the same reason.
    return Px(max(int(round(_base_size * _text_factor * ratio)),
                  int(_min_size), 1))


#: The tier lookup under its short name. `theme.size("caption")` reads
#: better at a call site than `size_of_tier`, and `size` is what every
#: widget already calls its parameter.
size = size_of_tier


def text_size(size):
    """A size as the toolkit will actually render it.

    Accepts a **tier name** (``"caption"``) or a number. The name is the
    preferred spelling at a call site: an author can tell whether a line
    is a caption and cannot tell whether it is 13 or 14, and picking
    between those was the whole of what made the type "feel a little
    random" [iw]. A number still works, for the genuine one-off that no
    tier describes.

    Numbers are multiplied and floored;

    applied where a widget resolves its size, **before layout measures
    it**. Flooring later -- at the logical-to-physical boundary, where
    `scaling` already touches every size -- would draw bigger text inside
    boxes measured for the smaller kind, which is overflow everywhere
    rather than a readability setting.
    """
    if isinstance(size, Px):
        # Already resolved, by whichever widget owns this subtree.
        return size
    if isinstance(size, str):
        # A tier is already scaled and floored by size().
        return size_of_tier(size)
    try:
        return Px(max(int(round(float(size) * _text_factor)),
                      int(_min_size), 1))
    except (TypeError, ValueError):
        return Px(max(int(round(_base_size * _text_factor)), 1))


def text_factor():
    """The user's text multiplier, for code that resolves its own sizes --
    which is the renderer, and nothing else."""
    return _text_factor


def min_size():
    return _min_size


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
