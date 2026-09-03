"""Script-aware PIL font resolution for baked bitmaps.

Text drawn as ASS (Text/Icon nodes) goes through libass, which does its own
fontconfig fallback — CJK, Arabic, emoji all just work. Text *baked into
bitmaps* (tile captions in mpvtk_browser.strips, the display mirror's title
block) goes through Pillow, and Pillow has no fallback at all: one TrueType
face is used for the whole string and anything it lacks renders as tofu
(□□□). Japanese/Chinese/Korean library titles hit this immediately.

So we pick the face per string: scan for the first character outside the
Latin/Cyrillic/Greek range our default face covers, map it to a script, and
load a system font known to cover that script. Everything is cached, and a
miss degrades to the default face (tofu, but never a crash).

"symbol" is a script here for the same reason and is otherwise not one: a
Latin face is not a symbol face, and the one the app lands on under Windows
(Arial) has no U+2605 — see :data:`_SYMBOL_RANGES`. **The ASS half of #713
needed no fix**: measured on Windows, libass falls back through DirectWrite
and draws U+2605, U+2713 and U+25B6 fine, so only text baked here was broken.
"""

import logging
import os

log = logging.getLogger("mpvtk.pilfont")

# Per-script candidates, most-preferred first. Bare names are resolved by
# Pillow through the platform font path; absolute paths are tried as-is so a
# Linux box with fontconfig-only layout still finds Noto.
_CANDIDATES = {
    "latin": [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "NotoSans-Regular.ttf",
        "Arial.ttf",
        "arial.ttf",
    ],
    "cjk": [
        "NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJKjp-Regular.otf",
        "/usr/share/fonts/truetype/droid/DroidSansFallbackFull.ttf",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/System/Library/Fonts/Hiragino Sans GB.ttc",
        "/System/Library/Fonts/PingFang.ttc",
        "msgothic.ttc",
        "meiryo.ttc",
        "YuGothM.ttc",
        "simsun.ttc",
        "malgun.ttf",
    ],
    "arabic": [
        "NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/System/Library/Fonts/GeezaPro.ttc",
        "arial.ttf",
    ],
    "devanagari": [
        "NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "mangal.ttf",
    ],
    "thai": [
        "NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        "tahoma.ttf",
    ],
    # Stars, ticks, arrows, media glyphs. Not a script anybody writes in, and
    # on Linux it resolves to the same DejaVu the Latin text does -- it earns
    # its place on **Windows**, where the Latin face is Arial and Arial has no
    # U+2605 (measured, along with segoeui/tahoma/verdana/calibri, none of
    # which has it either). That is #713: the community rating in a baked
    # detail banner drew as a tofu box.
    "symbol": [
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "NotoSansSymbols2-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansSymbols2-Regular.ttf",
        # Segoe UI Symbol, shipped with Windows since 7. Measured to carry
        # U+2605, U+2713, U+25B6 and U+266A.
        "seguisym.ttf",
        "/System/Library/Fonts/Apple Symbols.ttf",
        "/System/Library/Fonts/Supplemental/Arial Unicode.ttf",
    ],
}

# Bold variants, tried before the regular list for bold requests.
_BOLD = {
    "latin": ["DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "NotoSans-Bold.ttf", "arialbd.ttf"],
    "cjk": ["NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
}

_cache = {}          # (script, size, bold) -> ImageFont
_resolved = {}       # (script, bold) -> path/name that loaded, or None


#: Blocks a text face is not expected to cover: arrows, media and geometric
#: glyphs, stars, ticks. Deliberately **not** General Punctuation, currency,
#: letterlike or maths — an ordinary title is full of "…", "—" and "™", every
#: Latin face has them, and sending those to a second face would fragment
#: almost every string we draw for nothing.
_SYMBOL_RANGES = ((0x2190, 0x21FF),    # arrows
                  (0x2300, 0x23FF),    # misc technical, incl. media controls
                  (0x25A0, 0x25FF),    # geometric shapes
                  (0x2600, 0x26FF),    # misc symbols: stars, notes
                  (0x2700, 0x27BF),    # dingbats: ticks, crosses
                  (0x2B00, 0x2BFF))    # misc symbols and arrows


def script_of_char(cp):
    """The script one codepoint needs a face for.

    Latin/Cyrillic/Greek/Hebrew and the punctuation blocks all map to
    "latin", which is the face that covers them. The symbol blocks do not:
    see :data:`_SYMBOL_RANGES`.
    """
    if cp < 0x0590:                # ASCII, Latin ext, Greek, Cyrillic
        return "latin"
    if cp <= 0x05FF:               # Hebrew — DejaVu has it
        return "latin"
    if 0x0600 <= cp <= 0x06FF or 0xFB50 <= cp <= 0xFDFF:
        return "arabic"
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    if cp >= 0x2E80:               # CJK, kana, hangul, fullwidth forms
        return "cjk"
    if any(lo <= cp <= hi for lo, hi in _SYMBOL_RANGES):
        return "symbol"
    return "latin"                 # punctuation, currency, maths


def script_of(text):
    """The single script a string is drawn with when it cannot be split:
    the first character outside the Latin face's coverage wins, so
    "進撃の巨人 (2013)" resolves to cjk.

    **A lone symbol wins too, and that is deliberate**, even though it is
    one character of a line that is otherwise Latin. Callers read line
    height off this face (`components/banner.py`, `mpvtk_browser/cast.py`
    both take `getmetrics()` from it and reserve a band that tall), while
    ``draw_text`` puts the runs on a baseline derived from the *tallest*
    face present. Those two agree only if the face answered here is the
    tallest one — and it is: measured on Windows, Segoe UI Symbol is
    (22, 6) against Arial's (19, 5) at 20px. Answering "latin" for
    "★ 8.1" instead would reserve Arial's band and then draw into Segoe's,
    overflowing it by 4px. The cost is that a rated banner's meta line is
    a few px taller than an unrated one on Windows, which nothing on
    screen is there to compare against.
    """
    for ch in text or "":
        script = script_of_char(ord(ch))
        if script != "latin":
            return script
    return "latin"


def runs(text):
    """``[(script, chunk), ...]`` in order, adjacent same-script chars merged.

    **This is the whole fix for mixed strings.** A face named for a script is
    very often a face for *only* that script: measured here, DroidSansFallback
    (the CJK face a Debian box without Noto CJK lands on), NotoSansThai and
    NotoSansArabic all draw the letter A as .notdef -- so "進撃の巨人 (2013)"
    came out with the year as four tofu boxes. Splitting means the Latin run
    is drawn with the Latin face and the CJK run with the CJK one, which is
    what libass does for the text we hand to it.

    Whitespace is neutral and stays with the run in progress rather than
    starting a Latin one: a space is blank in every face, and splitting on it
    would double the run count of an ordinary sentence for nothing.
    """
    out = []
    for ch in text or "":
        script = None if ch.isspace() else script_of_char(ord(ch))
        if out and (script is None or script == out[-1][0]):
            out[-1][1].append(ch)
        else:
            out.append([script or "latin", [ch]])
    return [(script, "".join(chunk)) for script, chunk in out]


#: Hebrew and Arabic. A string containing any of these is drawn with one
#: face, because drawing runs left to right in logical order would put a
#: right-to-left run in the wrong place -- Pillow (through Raqm) reorders
#: within a single draw call and cannot across several. Tofu in one run is
#: a worse-looking line; reordered text is a wrong one.
_RTL_RANGES = ((0x0590, 0x05FF), (0x0600, 0x06FF), (0xFB50, 0xFDFF),
               (0xFE70, 0xFEFF))


def has_rtl(text):
    return any(lo <= ord(ch) <= hi
               for ch in text or "" for lo, hi in _RTL_RANGES)


def _load(names, size):
    # Before the import, not after: Pillow resolves FriBiDi once at
    # extension init, so this is the last moment it can matter. Idempotent
    # and a no-op off Windows; `mpv_shim.main` has normally done it already
    # and this covers every other way a face gets loaded. See win_fribidi.
    from ..win_fribidi import preload

    preload()
    from PIL import ImageFont

    for name in names:
        try:
            return ImageFont.truetype(name, size), name
        except (OSError, IOError):
            continue
    return None, None


def font_for(text, size, bold=False):
    """A PIL font able to render ``text`` at ``size``. Falls back to the Latin
    face (and finally Pillow's bitmap default) when nothing better is
    installed."""
    return font(script_of(text), size, bold)


def font(script, size, bold=False):
    key = (script, size, bool(bold))
    hit = _cache.get(key)
    if hit is not None:
        return hit
    names = []
    if bold:
        names += _BOLD.get(script, [])
    names += _CANDIDATES.get(script, [])
    if script != "latin":
        # Better a Latin face than Pillow's 11px bitmap default.
        if bold:
            names += _BOLD["latin"]
        names += _CANDIDATES["latin"]
    fnt, name = _load(names, size)
    if fnt is None:
        from PIL import ImageFont

        fnt = ImageFont.load_default()
        name = None
    if _resolved.get((script, bool(bold))) != name:
        _resolved[(script, bool(bold))] = name
        if name is None and script != "latin":
            log.info("no font found for script %r; text may not render", script)
    # What it was asked for, so draw_text can ask for the same in another
    # script. `size` is on FreeTypeFont already but the weight is not, and
    # the bitmap default has neither.
    try:
        fnt._jms_size, fnt._jms_bold = size, bool(bold)
    except AttributeError:         # a face that will not be annotated
        pass
    _cache[key] = fnt
    return fnt


def _same_size(fnt, script):
    """The face for ``script`` at the size and weight ``fnt`` was made with.

    Callers hold a font, not a (size, bold) pair -- they need its metrics for
    line spacing -- so the pair rides along on the object, stamped by
    ``font()``. A font from anywhere else falls back to its own ``size`` and
    regular weight, which is right for every caller here and wrong for
    nobody: the worst case is a bold run drawn regular.
    """
    return font(script, getattr(fnt, "_jms_size", getattr(fnt, "size", 12)),
                getattr(fnt, "_jms_bold", False))


def text_length(draw, text, fnt):
    """``draw.textlength`` for a string that may need more than one face.

    Measuring has to agree with drawing or a caption is ellipsized against a
    width it is not drawn at, so this and ``draw_text`` split identically.
    """
    if not text:
        return 0.0
    parts = runs(text)
    if len(parts) == 1 or has_rtl(text):
        return draw.textlength(text, font=fnt)
    return sum(draw.textlength(chunk, font=_same_size(fnt, script))
               for script, chunk in parts)


def draw_text(draw, xy, text, fnt, fill=None, anchor=None):
    """``draw.text`` with a face per script run.

    **A single-run string takes the original path**, byte for byte: one
    ``draw.text`` with the font it was given. That is almost every string
    this app draws, and it means the change can only alter the strings that
    were broken.

    Multi-run strings are drawn run by run along a shared baseline. The
    baseline is the point: PIL's default vertical anchor is the *ascender*,
    and two faces do not share one, so anchoring each run that way would
    stagger them. The tallest ascent in the line decides where the baseline
    sits, and every run is drawn from it.
    """
    if not text:
        return
    parts = runs(text)
    if len(parts) == 1 or has_rtl(text):
        draw.text(xy, text, font=fnt, fill=fill, anchor=anchor)
        return

    fonts = [_same_size(fnt, script) for script, _chunk in parts]
    ascent = max(f.getmetrics()[0] for f in fonts)
    descent = max(f.getmetrics()[1] for f in fonts)
    x, y = xy
    horizontal, vertical = (anchor or "la")[0], (anchor or "la")[1]
    if horizontal == "m":
        x -= text_length(draw, text, fnt) / 2.0
    elif horizontal == "r":
        x -= text_length(draw, text, fnt)
    if vertical == "a":
        baseline = y + ascent
    elif vertical == "m":
        baseline = y + (ascent - descent) / 2.0
    elif vertical == "d":
        baseline = y - descent
    else:                          # "s" -- already a baseline
        baseline = y
    for (script, chunk), face in zip(parts, fonts):
        draw.text((x, baseline), chunk, font=face, fill=fill, anchor="ls")
        x += draw.textlength(chunk, font=face)


def clear_cache():
    _cache.clear()
    _resolved.clear()


def _env_extra():
    """Allow an explicit override for exotic setups (a single font path)."""
    path = os.environ.get("JELLYFIN_MPV_SHIM_UI_FONT")
    if path:
        for names in list(_CANDIDATES.values()) + list(_BOLD.values()):
            names.insert(0, path)


_env_extra()
