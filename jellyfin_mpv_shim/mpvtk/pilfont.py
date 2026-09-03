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

"emoji" is a third such pseudo-script, and the only one where the *face* is
awkward rather than just the choice of it: a colour-emoji face is drawn in
its own colours (``embedded_color``) and is very often available at one
fixed pixel size and no other. :data:`_STRIKES` and :func:`_draw_scaled` are
the two halves of that. A symbol face is not an emoji face either — neither
Segoe UI Symbol nor NotoSansSymbols2 carries one (measured).
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
    # Arabic is RTL too, so the "one face for the line" note under "hebrew"
    # applies -- but the answer is the opposite one. Measured:
    # NotoSansArabic carries the full stop, comma and digits, so an ordinary
    # Arabic sentence is whole; what it lacks is A-Z, so an Arabic line with
    # a Latin *word* in it draws that word as boxes. DejaVu would fix that
    # -- it has full Latin and real Arabic shaping (`arab` in both GSUB and
    # GPOS) -- but it covers 165 of 256 Arabic-block codepoints against
    # Noto's 255, and 249 of the 772 presentation forms against Noto's 751.
    # Arabic is a script of presentation forms, so that is the wrong three
    # quarters to give up for the occasional Latin word. Left as it is,
    # knowingly, and not for want of a candidate.
    "arabic": [
        "NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/System/Library/Fonts/GeezaPro.ttc",
        "arial.ttf",
    ],
    # Hebrew. Folded into "latin" until F33, on the strength of DejaVu
    # having it -- and Arial does too, so both the developer's box and
    # Windows looked fine. `NotoSans-Regular.ttf` is third in that list and
    # has no Hebrew at all (measured), so a box with Noto Sans and no DejaVu
    # drew every Hebrew title as boxes. A script is not covered because the
    # face you happen to have covers it.
    #
    # **The order is the load-bearing part, and it is the opposite of every
    # other list here.** Hebrew is RTL, so `has_rtl` gives the whole line to
    # ONE face -- there is no Latin run to fall to -- and that face has to
    # carry the neutrals as well as the script. `NotoSansHebrew-Regular.ttf`
    # is 145 codepoints: no full stop, no comma, no digit, no ASCII at all
    # (measured), so putting it first drew "שלום עולם." with the stop as a
    # box and every year in a title as four of them.
    #
    # Liberation Sans leads because it needs no trade at all: measured, 87
    # of the 88 assigned Hebrew codepoints (all but U+05EF), all 46
    # presentation forms, all 95 printable ASCII, and `hebr` in both GSUB
    # and GPOS so the points still stack. That is everything Noto Hebrew
    # has plus the Latin it does not. DejaVu is next and is the same idea
    # with a cost -- it misses 34: the cantillation marks U+0591-05AF plus
    # U+05C4, U+05C5 and U+05EF, i.e. biblical Hebrew. Noto stays last, for
    # the host with none of the above, which is what F33 was about.
    "hebrew": [
        "LiberationSans-Regular.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/System/Library/Fonts/Supplemental/Arial Hebrew.ttc",
        "arial.ttf",
        "NotoSansHebrew-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansHebrew-Regular.ttf",
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
    # Colour emoji. Not a script either, and the one candidate list here
    # where the *pixel size* is part of the problem: Windows' seguiemj is
    # COLR-outlined and loads at any size, but NotoColorEmoji.ttf -- what
    # every Linux box has -- is a CBDT bitmap face with a single 109px
    # strike and raises `OSError: invalid pixel size` at every other size
    # (measured). `_load` probes strikes for this script alone, and
    # `draw_text` shrinks what the strike draws. The two monochrome outline
    # faces at the end are the graceful degradation: they load at the asked
    # size and need none of that.
    "emoji": [
        "seguiemj.ttf",
        "NotoColorEmoji.ttf",
        "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
        "NotoColorEmoji-Regular.ttf",
        "/System/Library/Fonts/Apple Color Emoji.ttc",
        "NotoEmoji-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
        "Symbola.ttf",
        "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
    ],
}

#: Where a script's face falls back to *before* the Latin one. Only emoji
#: has an entry, and it matters: with no emoji font at all, "⭐" drawn by
#: the Latin face is a box, and drawn by the symbol face is the monochrome
#: star it has always been. Falling straight to Latin would make this
#: change a regression on exactly the hosts it cannot help.
_FALLBACK_SCRIPTS = {"emoji": ("symbol",)}

#: Pixel sizes a bitmap-strike face may be available at, tried in
#: `_strike_order` when the asked-for size is refused. NotoColorEmoji's 109
#: is measured here; 128 and 136 are what the older Noto and the Twemoji
#: builds ship. The Apple Color Emoji entries (20/26/32/40/48/52/64/96/160)
#: are a **probe set, not a verified inventory** -- nobody on this project
#: has a Mac, and 26 and 52 in particular are unconfirmed on current
#: releases. Being wrong either way is cheap: a size that is not a strike
#: costs one failed load, and a strike not listed means the face is simply
#: not used and the run falls to the symbol face, which is what it does
#: today. `TTCollection(path).fonts[0]["sbix"].strikes` settles it on a Mac.
_STRIKES = (16, 20, 26, 32, 40, 48, 52, 64, 96, 109, 128, 136, 160)

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

#: Codepoints that want a **colour emoji** face.
#:
#: The BMP half and the picked astral singletons are exactly Unicode's
#: ``Emoji_Presentation=Yes`` (UTS #51, emoji-data.txt) -- the codepoints
#: drawn in colour by default, as opposed to the ones that only become
#: emoji when followed by U+FE0F -- and a test checks that against the
#: local copy of that file. **The whole astral blocks deliberately are
#: not**: they are coarser than the property, for the reason below.
#:
#: The BMP half is a list of small ranges rather than whole blocks, and that
#: is the load-bearing part: U+2605 and U+2713 sit inside U+2600-27BF, and
#: **neither colour face draws them** -- measured, both NotoColorEmoji and
#: seguiemj answer .notdef -- so sweeping the block into this table would put
#: #713's star straight back to tofu. The text-presentation neighbours stay
#: on the symbol face, which is the face that has them.
#:
#: The astral half is whole blocks, and can be, because up there the current
#: answer is the ``cp >= 0x2E80`` CJK catch-all and it is **already tofu**:
#: NotoSansCJK covers 0 of U+1F300-1F5FF and msgothic 0 of every block here
#: (measured). The ~130 text-presentation pictographs inside U+1F300-1F5FF
#: that Noto Color Emoji does not carry stay tofu, exactly as they are now;
#: carving them out would be a table nobody could check.
_EMOJI_RANGES = ((0x231A, 0x231B), (0x23E9, 0x23EC), (0x23F0, 0x23F0),
                 (0x23F3, 0x23F3), (0x25FD, 0x25FE), (0x2614, 0x2615),
                 (0x2648, 0x2653), (0x267F, 0x267F), (0x2693, 0x2693),
                 (0x26A1, 0x26A1), (0x26AA, 0x26AB), (0x26BD, 0x26BE),
                 (0x26C4, 0x26C5), (0x26CE, 0x26CE), (0x26D4, 0x26D4),
                 (0x26EA, 0x26EA), (0x26F2, 0x26F3), (0x26F5, 0x26F5),
                 (0x26FA, 0x26FA), (0x26FD, 0x26FD), (0x2705, 0x2705),
                 (0x270A, 0x270B), (0x2728, 0x2728), (0x274C, 0x274C),
                 (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
                 (0x2795, 0x2797), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
                 (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
                 # Astral. The enclosed-alphanumeric singletons are picked
                 # out of blocks that are otherwise CJK's: U+1F110 (circled
                 # A) is tofu in both colour faces and mono in NotoSansCJK,
                 # so the block cannot move wholesale.
                 (0x1F004, 0x1F004), (0x1F0CF, 0x1F0CF), (0x1F18E, 0x1F18E),
                 (0x1F191, 0x1F19A), (0x1F1E6, 0x1F1FF), (0x1F201, 0x1F202),
                 (0x1F21A, 0x1F21A), (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A),
                 (0x1F250, 0x1F251), (0x1F300, 0x1F64F), (0x1F680, 0x1F6FF),
                 (0x1F7E0, 0x1F7FF), (0x1F900, 0x1F9FF), (0x1FA70, 0x1FAFF))

#: Astral blocks that are ordinary monochrome symbols, not emoji, and that
#: the same CJK catch-all was swallowing. Measured: NotoSansSymbols2 draws
#: all of these and NotoSansCJK draws none of them, so the catch-all was the
#: reason a domino or a chess piece in a title was a box.
_ASTRAL_SYMBOL_RANGES = ((0x1F000, 0x1F0FF),   # mahjong, dominoes, cards
                         (0x1F650, 0x1F67F),   # ornamental dingbats
                         (0x1F700, 0x1F7DF),   # alchemical, geometric ext
                         (0x1F800, 0x1F8FF),   # supplemental arrows-C
                         (0x1FA00, 0x1FA6F),   # chess, symbols ext-A
                         (0x1FB00, 0x1FBFF))   # legacy computing


def _codepoints(ranges):
    """A range table, flattened for lookup.

    The tables above stay ranges because that is the form a human can check
    them against emoji-data.txt in; this is the form the repaint path can
    afford. `script_of_char` runs per character, and `strips._ellipsize`
    re-measures a caption once per character while it trims, so walking 48
    emoji ranges to reject one CJK character showed up: measured, 1.2us a
    character against 0.017us for a set membership, which is 25us against
    2us for one Japanese caption and milliseconds across a grid. The three
    sets together are ~3,800 codepoints.
    """
    return frozenset(cp for lo, hi in ranges for cp in range(lo, hi + 1))


_SYMBOL_CPS = _codepoints(_SYMBOL_RANGES)
_EMOJI_CPS = _codepoints(_EMOJI_RANGES)
_ASTRAL_SYMBOL_CPS = _codepoints(_ASTRAL_SYMBOL_RANGES)


def script_of_char(cp):
    """The script one codepoint needs a face for.

    Latin/Cyrillic/Greek and the punctuation blocks all map to "latin",
    which is the face that covers them. The symbol blocks do not (see
    :data:`_SYMBOL_RANGES`), and neither do the emoji ones (see
    :data:`_EMOJI_RANGES`) -- a symbol face is not an emoji face either.
    """
    if cp < 0x0590:                # ASCII, Latin ext, Greek, Cyrillic
        return "latin"
    if cp <= 0x05FF or 0xFB1D <= cp <= 0xFB4F:
        return "hebrew"            # ...and its presentation forms
    if (0x0600 <= cp <= 0x06FF or 0xFB50 <= cp <= 0xFDFF
            or 0xFE70 <= cp <= 0xFEFF):
        return "arabic"            # ...and both presentation-form blocks
    if 0x0900 <= cp <= 0x097F:
        return "devanagari"
    if 0x0E00 <= cp <= 0x0E7F:
        return "thai"
    # Before the CJK catch-all, because most of this table is above it.
    if cp in _EMOJI_CPS:
        return "emoji"
    if cp >= 0x2E80:               # CJK, kana, hangul, fullwidth forms
        return "symbol" if cp in _ASTRAL_SYMBOL_CPS else "cjk"
    if cp in _SYMBOL_CPS:
        return "symbol"
    return "latin"                 # punctuation, currency, maths


def script_of(text):
    """The single script a string is drawn with when it cannot be split:
    the first character outside the Latin face's coverage wins, so
    "進撃の巨人 (2013)" resolves to cjk.

    **A symbol only wins when there is nothing else in the string.** It is a
    face for the odd glyph, not for words, and this answer is used for two
    things that would be wrong for: the line height a caller reserves
    (`components/banner.py`, `mpvtk_browser/cast.py`), and the face handed
    to ``draw_text`` for a *substring* — a wrapped or ellipsized line the
    symbol need not have survived into. Segoe UI Symbol's Latin is not
    Arial's and it carries no Arabic or Hebrew at all (measured), so one
    rating choosing it re-typesets whole paragraphs, and for an RTL line —
    which cannot be split at all — draws every word as a box. A real script
    returns above before the question arises.

    But a string of *nothing but* symbols has no words to protect and still
    has to be drawn by something. `components.placeholder_glyph` answers
    with the first character of a title, so an album named "★" is #713 all
    over again if this says "latin".

    **"emoji" is never the answer, not even for a string of nothing but
    emoji**, and that is deliberate rather than an omission. A colour-emoji
    face is very often bitmap-only -- ``NotoColorEmoji.ttf`` loads at 109px
    and at no other size (measured) -- so the object `font("emoji", 20)`
    returns has a 109px face's metrics, and this answer is used to reserve a
    line's *height* (`components/banner.py`, `mpvtk_browser/cast.py`) and to
    pick the face for a whole book (`epub/fonts.py`). Both would be wrong by
    a factor of five. The emoji face is reached per *run*, inside
    ``draw_text``, which knows to scale what it draws; it is not something a
    caller should be handed.
    """
    saw_symbol = saw_word = False
    for ch in text or "":
        script = script_of_char(ord(ch))
        if script in ("symbol", "emoji"):
            saw_symbol = True
        elif script != "latin":
            return script            # cjk / arabic / thai / devanagari
        elif not ch.isspace():
            saw_word = True
    # No ``has_rtl`` guard needed and none added: every RTL codepoint maps
    # to hebrew or arabic and has returned above (an invariant a test
    # holds), so anything reaching here has no RTL in it at all. A
    # redundant condition would read as load-bearing.
    return "symbol" if saw_symbol and not saw_word else "latin"


#: Characters that join what is around them into one glyph and must never
#: start a run of their own. A run boundary is a separate ``draw.text``
#: call, and shaping does not cross one: split "👩‍💻" at the joiner
#: and Raqm sees three strings instead of one, so it draws two emoji where
#: the font has a single glyph. Measured with Raqm: the ZWJ sequence, the
#: flag pair and the keycap all shape to one glyph of the same advance as
#: one emoji, and the variation selectors are consumed rather than drawn.
#:
#: U+20E3 rides with the digit *before* it, which is what makes "1️⃣"
#: come out as a keycap from an ordinary Latin face rather than as a digit
#: and a box.
_JOINERS = frozenset((0x200D,            # zero width joiner
                      0xFE0E, 0xFE0F,    # text / emoji variation selectors
                      0x20E3))           # combining enclosing keycap


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

    **Except after emoji**, where "blank in every face" stops being true: a
    space is blank in a colour-emoji face but as *wide* as an emoji --
    measured, NotoColorEmoji advances 135.7 of its 109px em for U+0020,
    against DejaVu's 6.4 at 20px -- so a caption with an emoji in the middle
    came out with a four-space hole after it.

    :data:`_JOINERS` is neutral for a harder reason, and unconditionally --
    see there.
    """
    out = []
    for ch in text or "":
        if ord(ch) in _JOINERS:
            script = None
        elif ch.isspace():
            script = "latin" if out and out[-1][0] == "emoji" else None
        else:
            script = script_of_char(ord(ch))
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
#:
#: **Kept in step with `script_of_char` by a test** — every codepoint here
#: must map to "hebrew" or "arabic", because this table decides that a line
#: gets ONE face and that one decides which. U+FE70-FEFF was in this table
#: and mapped to *cjk*, so an Arabic line written in presentation forms was
#: drawn end to end with a face that cannot draw a word of it.
_RTL_RANGES = ((0x0590, 0x05FF), (0x0600, 0x06FF), (0xFB1D, 0xFB4F),
               (0xFB50, 0xFDFF), (0xFE70, 0xFEFF))


def has_rtl(text):
    return any(lo <= ord(ch) <= hi
               for ch in text or "" for lo, hi in _RTL_RANGES)


def _strike_order(size):
    """Fixed sizes to try for a face that refused ``size``.

    Bigger first, so what gets drawn is shrunk rather than blown up, and
    the smallest of those, so the shrink is the gentlest available.
    """
    return ([s for s in _STRIKES if s > size]
            + [s for s in reversed(_STRIKES) if s < size])


def _load(names, size, strikes=False):
    """``(font, name, native)``. ``native`` is the size it actually opened
    at, which is ``size`` for every scalable face and the strike for a
    bitmap one."""
    # Before the import, not after: Pillow resolves FriBiDi once at
    # extension init, so this is the last moment it can matter. Idempotent
    # and a no-op off Windows; `mpv_shim.main` has normally done it already
    # and this covers every other way a face gets loaded. See win_fribidi.
    from ..win_fribidi import preload

    preload()
    from PIL import ImageFont

    for name in names:
        for want in [size] + (_strike_order(size) if strikes else []):
            try:
                return ImageFont.truetype(name, want), name, want
            except (OSError, IOError):
                continue
    return None, None, size


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
    for other in _FALLBACK_SCRIPTS.get(script, ()):
        names += _CANDIDATES.get(other, [])
    if script != "latin":
        # Better a Latin face than Pillow's 11px bitmap default.
        if bold:
            names += _BOLD["latin"]
        names += _CANDIDATES["latin"]
    fnt, name, native = _load(names, size, strikes=(script == "emoji"))
    if fnt is None:
        from PIL import ImageFont

        fnt = ImageFont.load_default()
        name, native = None, size
    if _resolved.get((script, bool(bold))) != name:
        _resolved[(script, bool(bold))] = name
        if name is None and script != "latin":
            log.info("no font found for script %r; text may not render", script)
    # What it was asked for, so draw_text can ask for the same in another
    # script. `size` is on FreeTypeFont already but the weight is not, and
    # the bitmap default has neither. The script rides along too, so a face
    # can be asked whether it is the right one for the run in hand -- see
    # _run_face.
    try:
        fnt._jms_size, fnt._jms_bold = size, bool(bold)
        fnt._jms_script = script
        # The size it OPENED at, which is the size everything it draws and
        # measures comes back in. `_scale_of` is the only reader.
        fnt._jms_native = native
    except AttributeError:         # a face that will not be annotated
        pass
    _cache[key] = fnt
    return fnt


def _scale_of(fnt):
    """How much of its natural size a face's output has to be shrunk to.

    1.0 for every scalable face, so every path below is a no-op for all of
    them. It is not 1.0 for a bitmap-strike colour-emoji face, which draws
    and measures at its strike and nothing else: see :data:`_STRIKES`.
    """
    native = getattr(fnt, "_jms_native", None)
    want = getattr(fnt, "_jms_size", None)
    if not native or not want or native == want:
        return 1.0
    return float(want) / float(native)


def metrics(fnt):
    """``(ascent, descent)`` in the pixels a face actually draws in.

    **The one place a face's metrics may be read from.** A bitmap-strike
    emoji face reports its strike's metrics -- 101 and 27, for a face being
    used at 20px -- so a caller reserving a line from ``getmetrics()``
    directly reserves five lines. Tolerant of Pillow's bitmap default,
    which has no ``getmetrics`` at all.
    """
    try:
        ascent, descent = fnt.getmetrics()
    except AttributeError:
        size = getattr(fnt, "size", 11)
        return int(size * 0.8), int(size * 0.2)
    scale = _scale_of(fnt)
    if scale == 1.0:
        return ascent, descent
    # Ints, like `getmetrics()` and like the untouched branch above. These
    # end up in `layout.Line.ascent` and in the baselines below, and a
    # float there is a gratuitous type change across the reader's whole
    # geometry for a number that is pixels either way.
    return int(round(ascent * scale)), int(round(descent * scale))


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


def _run_face(fnt, script):
    """The face to draw a run of ``script`` with, given the font a caller
    chose — possibly for a longer string this run was wrapped out of.

    ``fnt`` itself whenever it is already that script, or when it carries no
    stamp at all: a caller who built a face by hand gets exactly the face it
    passed, which is what keeps a single-run string drawn byte for byte as
    it was before any of this existed.

    **Single-run only.** The multi-run paths use :func:`_same_size`, which
    resolves a real face per script unconditionally. For a stamped font the
    two agree exactly (the stamp came from the same `font()` call the
    lookup would make), and for an unstamped one they must not: "leave the
    caller's face alone" is right for a string that face can draw and
    disastrous for the mixed string this whole path exists to handle,
    where it puts the tofu straight back.
    """
    stamped = getattr(fnt, "_jms_script", None)
    if stamped is None or stamped == script:
        return fnt
    return _same_size(fnt, script)


def _face_pickers(fnt, faces):
    """``(single, per_run)`` — how to turn a script name into a face.

    ``faces`` is the seam the epub reader hangs off: its faces are serif
    families of its own, resolved per (kind, size, weight, slant), so it
    supplies a resolver rather than borrowing this module's. **A supplied
    resolver is authoritative for both**, which is the difference that
    matters: `_run_face`'s "leave an unstamped caller's face exactly as it
    was passed" rule is a promise to the *browser*, whose fonts all come
    from `font()` and so carry a stamp. The reader's Latin faces do not,
    and honouring the rule there would hand a CJK run a serif face --
    which is the bug the reader was routed through here to fix.
    """
    if faces is not None:
        return faces, faces
    return ((lambda script: _run_face(fnt, script)),
            (lambda script: _same_size(fnt, script)))


def _split(text, fnt, faces):
    """``(runs, one_face_or_None, per_run_resolver)`` — the one place the
    two bypasses live, so measuring and drawing cannot disagree about them.
    A non-None second element means the whole string is drawn with it.

    A single **emoji** run does not take the bypass. It has to reach the
    per-run resolver (`_run_face` would hand back the caller's own face,
    which is the face that cannot draw it) and, on a bitmap-strike face,
    the scaling that only the run loop does.
    """
    parts = runs(text)
    single, per_run = _face_pickers(fnt, faces)
    if has_rtl(text):
        return parts, single(script_of(text)), per_run
    if len(parts) == 1 and parts[0][0] != "emoji":
        return parts, single(parts[0][0]), per_run
    return parts, None, per_run


def _measure(text, fnt, faces, measure):
    if not text:
        return 0.0
    parts, whole, per_run = _split(text, fnt, faces)
    if whole is not None:
        return measure(text, whole) * _scale_of(whole)
    total = 0.0
    for script, chunk in parts:
        face = per_run(script)
        total += measure(chunk, face) * _scale_of(face)
    return total


def text_length(draw, text, fnt, faces=None):
    """``draw.textlength`` for a string that may need more than one face.

    Measuring has to agree with drawing or a caption is ellipsized against a
    width it is not drawn at, so this and ``draw_text`` split identically.
    """
    return _measure(text, fnt, faces,
                    lambda chunk, face: draw.textlength(chunk, font=face))


def length(text, fnt, faces=None):
    """:func:`text_length` for a caller that has no ``ImageDraw``.

    The epub reader measures a chapter to paginate it long before there is
    anything to draw on, and building a 1x1 image per measurement is the
    thing its width cache exists to avoid. ``draw.textlength`` is
    ``font.getlength`` with the draw's ``fontmode`` passed through, and
    every surface here is antialiased, which is that default.
    """
    return _measure(text, fnt, faces,
                    lambda chunk, face: face.getlength(chunk))


def _draw_scaled(draw, x, baseline, chunk, face, scale, fill):
    """Draw one run with a face that only exists at another pixel size.

    Rendered at the face's own size into a scratch bitmap, shrunk, and
    composited. There is no other way round it: ``NotoColorEmoji.ttf`` is a
    CBDT bitmap face with one 109px strike, so a 20px caption either draws
    its emoji at 109 and shrinks, or does not draw them.

    Not premultiplied before the resize, deliberately. The glyphs are
    already antialiased into transparent *black*, so the naive resize has no
    halo to remove -- measured against both a white and a dark plate -- and
    premultiplying without dividing back out darkens every edge instead.

    Composited through ``draw.im`` rather than ``Image.paste`` because that
    is all a caller gives us, and it is the same core call Pillow's own
    ``ImageDraw.text`` makes for colour glyphs. The alpha rides in the mask
    and not in the source, or a transparent plate squares it and the run
    comes out thin.
    """
    from PIL import Image, ImageDraw

    target = getattr(draw, "im", None)
    if target is None:             # a draw with no image behind it
        return
    ascent, descent = face.getmetrics()
    # Emoji bitmaps overshoot the ascent/descent box by a pixel or two, and
    # the scratch is the only clip they have.
    pad = max(1, int(round(ascent * 0.25)))
    advance = draw.textlength(chunk, font=face)
    w = max(1, int(advance) + 2 * pad)
    h = max(1, ascent + descent + 2 * pad)
    scratch = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(scratch).text(
        (pad, pad + ascent), chunk, font=face,
        fill=fill if fill is not None else (255, 255, 255, 255),
        anchor="ls", embedded_color=True)
    small = scratch.resize((max(1, int(round(w * scale))),
                            max(1, int(round(h * scale)))), Image.LANCZOS)
    body = small.convert("RGB")
    if draw.mode != "RGB":
        body = body.convert(draw.mode)
    ox = int(round(x - pad * scale))
    oy = int(round(baseline - (pad + ascent) * scale))
    target.paste(body.im, (ox, oy, ox + small.width, oy + small.height),
                 small.getchannel("A").im)


def draw_text(draw, xy, text, fnt, fill=None, anchor=None, faces=None):
    """``draw.text`` with a face per script run.

    **A single-run string takes the original path**, byte for byte: one
    ``draw.text`` with the font it was given. That is almost every string
    this app draws, and it means the change can only alter the strings that
    were broken. The one exception is a run of emoji — see :func:`_split`.

    ``faces`` overrides where a run's face comes from; the epub reader
    passes its own resolver. See :func:`_face_pickers`.

    Multi-run strings are drawn run by run along a shared baseline. The
    baseline is the point: PIL's default vertical anchor is the *ascender*,
    and two faces do not share one, so anchoring each run that way would
    stagger them. The tallest ascent in the line decides where the baseline
    sits, and every run is drawn from it.

    So a mixed line can sit a little lower than the band its caller
    reserved, which reserves from ``script_of``'s face and not from the
    tallest run: on Windows the symbol face is (22, 6) against Arial's
    (19, 5) at 20px, so one star pushes the line down about 3px. That is
    the accepted cost of *not* letting a symbol choose the whole string's
    face — the alternative re-typesets every wrapped line of a paragraph in
    Segoe UI Symbol. Every caller here draws into a margin that absorbs it
    (`banner.py` has 18px below the meta line, `cast.py` a per-line gap),
    and no tile caption carries a symbol.
    """
    if not text:
        return
    parts, whole, per_run = _split(text, fnt, faces)
    if whole is not None:
        # One draw call covers the line. For an RTL line that is forced --
        # Pillow reorders bidi within a call and cannot across several --
        # and it has to be the face for THIS line, not for the longer one it
        # may have been wrapped out of. For a single-run line it is the
        # original path, byte for byte.
        draw.text(xy, text, font=whole, fill=fill, anchor=anchor)
        return

    fonts = [per_run(script) for script, _chunk in parts]
    scales = [_scale_of(f) for f in fonts]
    # Through `metrics`, or a 109px emoji strike would decide the baseline
    # for a 20px line and push the whole thing five lines down.
    ascent = max(metrics(f)[0] for f in fonts)
    descent = max(metrics(f)[1] for f in fonts)
    x, y = xy
    horizontal, vertical = (anchor or "la")[0], (anchor or "la")[1]
    if horizontal == "m":
        x -= text_length(draw, text, fnt, faces) / 2.0
    elif horizontal == "r":
        x -= text_length(draw, text, fnt, faces)
    if vertical == "a":
        baseline = y + ascent
    elif vertical == "m":
        baseline = y + (ascent - descent) / 2.0
    elif vertical == "d":
        baseline = y - descent
    else:                          # "s" -- already a baseline
        baseline = y
    for (script, chunk), face, scale in zip(parts, fonts, scales):
        if scale != 1.0:
            _draw_scaled(draw, x, baseline, chunk, face, scale, fill)
        else:
            # `embedded_color` for emoji only. It is what makes a colour
            # face draw in its own colours instead of the fill's, and on a
            # face that has no colour in it the two render the same picture
            # -- but not the same *bytes*, and every other run here is one
            # this module promises to leave exactly as it found it.
            #
            # **And only onto a target that can hold colour**: Pillow
            # raises `ValueError: Embedded color supported only in RGB and
            # RGBA modes` otherwise. Reachable only where the emoji face
            # loads at the asked size, so it fires on Windows (seguiemj is
            # COLR-outlined) and never on Linux, where a bitmap strike
            # sends the same run through `_draw_scaled` and its own RGBA
            # scratch instead. A greyscale emoji is the right degradation
            # for a greyscale plate; an exception is not.
            draw.text((x, baseline), chunk, font=face, fill=fill,
                      anchor="ls",
                      embedded_color=(script == "emoji"
                                      and draw.mode in ("RGB", "RGBA")))
        x += draw.textlength(chunk, font=face) * scale


def clear_cache():
    _cache.clear()
    _resolved.clear()


def _env_extra():
    """Allow an explicit override for exotic setups (a single font path).

    It goes in front of **every** list, emoji included, so setting it turns
    emoji back into whatever that one face draws for them. That is the
    contract of "use this font" and is no worse than the answer before
    there was an emoji bucket -- but it is why a host with this set will
    not show the colour ones.
    """
    path = os.environ.get("JELLYFIN_MPV_SHIM_UI_FONT")
    if path:
        for names in list(_CANDIDATES.values()) + list(_BOLD.values()):
            names.insert(0, path)


_env_extra()
