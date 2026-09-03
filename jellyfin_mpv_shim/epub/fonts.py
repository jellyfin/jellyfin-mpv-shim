"""Faces for the reader, and the one place text is measured.

Pillow has no font fallback and no synthetic styles — one `FreeTypeFont`
draws one face — so bold, italic, bold-italic and monospace mean four to
eight real font files, resolved up front by *trying to load them*,
most-preferred first. Every failure degrades and nothing here raises: a
missing font must not be the difference between a book opening and an
error screen.

Why the body face is a serif, and what the :mod:`~jellyfin_mpv_shim.mpvtk.pilfont`
fallback costs a non-Latin book — see ``docs/readers.md`` §4.6.
"""


import logging
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("epub.fonts")

#: Serif families to try for the body text, most-preferred first. Each
#: entry is (regular, bold, italic, bold-italic); a missing member of a
#: family falls back to that family's regular.
_SERIF_FAMILIES = [
    ("DejaVuSerif.ttf", "DejaVuSerif-Bold.ttf", "DejaVuSerif-Italic.ttf",
     "DejaVuSerif-BoldItalic.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Italic.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSerif-BoldItalic.ttf"),
    ("LiberationSerif-Regular.ttf", "LiberationSerif-Bold.ttf",
     "LiberationSerif-Italic.ttf", "LiberationSerif-BoldItalic.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-Italic.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSerif-BoldItalic.ttf"),
    ("NotoSerif-Regular.ttf", "NotoSerif-Bold.ttf", "NotoSerif-Italic.ttf",
     "NotoSerif-BoldItalic.ttf"),
    ("Georgia.ttf", "Georgiab.ttf", "Georgiai.ttf", "Georgiaz.ttf"),
    ("georgia.ttf", "georgiab.ttf", "georgiai.ttf", "georgiaz.ttf"),
    ("times.ttf", "timesbd.ttf", "timesi.ttf", "timesbi.ttf"),
    ("/System/Library/Fonts/Supplemental/Georgia.ttf",) * 4,
]

#: Sans families, for the reader that asks for one. Nothing selects
#: this yet — the reader's bar has type size and page colour, not face.
_SANS_FAMILIES = [
    ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf", "DejaVuSans-Oblique.ttf",
     "DejaVuSans-BoldOblique.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-Oblique.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSans-BoldOblique.ttf"),
    ("LiberationSans-Regular.ttf", "LiberationSans-Bold.ttf",
     "LiberationSans-Italic.ttf", "LiberationSans-BoldItalic.ttf"),
    ("NotoSans-Regular.ttf", "NotoSans-Bold.ttf", "NotoSans-Italic.ttf",
     "NotoSans-BoldItalic.ttf"),
    ("arial.ttf", "arialbd.ttf", "ariali.ttf", "arialbi.ttf"),
]

_MONO_FAMILIES = [
    ("DejaVuSansMono.ttf", "DejaVuSansMono-Bold.ttf",
     "DejaVuSansMono-Oblique.ttf", "DejaVuSansMono-BoldOblique.ttf"),
    ("/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Oblique.ttf",
     "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-BoldOblique.ttf"),
    ("LiberationMono-Regular.ttf", "LiberationMono-Bold.ttf",
     "LiberationMono-Italic.ttf", "LiberationMono-BoldItalic.ttf"),
    ("consola.ttf", "consolab.ttf", "consolai.ttf", "consolaz.ttf"),
    ("cour.ttf", "courbd.ttf", "couri.ttf", "courbi.ttf"),
]

FAMILIES = {"serif": _SERIF_FAMILIES, "sans": _SANS_FAMILIES,
            "mono": _MONO_FAMILIES}

_family_cache: Dict[str, Optional[Tuple[str, ...]]] = {}
_font_cache: Dict[Tuple[Any, ...], Any] = {}


def _load(name, size):
    # See mpvtk.pilfont._load. The reader paints with Pillow too, so a book
    # in a right-to-left script needs this as much as the library does.
    from ..win_fribidi import preload

    preload()
    from PIL import ImageFont

    try:
        return ImageFont.truetype(name, size)
    except (OSError, IOError, ValueError):
        return None


def _resolve_family(kind):
    """The first family whose *regular* face loads. Cached per kind."""
    hit = _family_cache.get(kind)
    if hit is not None:
        return hit
    for family in FAMILIES.get(kind, _SERIF_FAMILIES):
        if _load(family[0], 16) is not None:
            _family_cache[kind] = family
            return family
    log.info("no %s family found; falling back to Pillow's default face",
             kind)
    _family_cache[kind] = None
    return None


def face(kind, size, bold=False, italic=False, script="latin"):
    """A PIL font. Never None, never raises.

    ``kind`` is "serif", "sans" or "mono"; ``script`` is a
    :mod:`~jellyfin_mpv_shim.mpvtk.pilfont` script name, and anything other
    than latin is served by that module (which has the CJK/Arabic/Indic
    candidate lists) rather than duplicating them here.
    """
    size = max(6, int(size))
    key = (kind, size, bool(bold), bool(italic), script)
    hit = _font_cache.get(key)
    if hit is not None:
        return hit
    font = None
    # "symbol" is treated as latin. `script_of` no longer answers it, so the
    # reader cannot reach this today -- but `script` is a parameter and
    # "symbol" is a legal pilfont script, and it is the one value that would
    # be wrong here: it names a face for the odd glyph, and this call is
    # choosing the face a whole book is set in.
    if script not in ("latin", "symbol", "", None):
        from ..mpvtk import pilfont

        # No italic: pilfont's per-script lists are regular and bold, and a
        # synthesised slant is not something Pillow offers. Drawing the
        # upright face is the same choice every CJK-capable reader makes.
        font = pilfont.font(script, size, bold)
    else:
        family = _resolve_family(kind)
        if family:
            index = (2 if italic else 0) + (1 if bold else 0)
            # Order in the tuple is (regular, bold, italic, bold-italic),
            # and each missing face falls back along the axis that matters
            # least: bold-italic -> italic -> regular, so emphasis survives
            # even when the weight cannot.
            for candidate in (family[index], family[index & ~1],
                              family[index & 1], family[0]):
                font = _load(candidate, size)
                if font is not None:
                    break
    if font is None:
        from PIL import ImageFont

        font = ImageFont.load_default()
    _font_cache[key] = font
    return font


def metrics(font):
    """``(ascent, descent)`` in pixels, with a usable answer for the
    bitmap default (which has no ``getmetrics``)."""
    try:
        ascent, descent = font.getmetrics()
        return ascent, descent
    except AttributeError:
        size = getattr(font, "size", 11)
        return int(size * 0.8), int(size * 0.2)


def clear_cache():
    _font_cache.clear()
    _family_cache.clear()
