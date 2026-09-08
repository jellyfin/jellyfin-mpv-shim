"""Script-aware PIL font resolution for baked bitmaps.

**Pillow has no font fallback**: one TrueType face draws the whole string and
anything it lacks renders as tofu (□□□). libass, which draws the ASS half
of the UI, does its own fontconfig fallback — so only text *baked into
bitmaps* (tile captions in mpvtk_browser.strips, the display mirror's title
block) needs any of this. CJK library titles hit it immediately.

So we pick the face per string: scan for the first character outside the
Latin/Cyrillic/Greek range our default face covers, map it to a script, and
load a system font known to cover that script. Everything is cached, and a
miss degrades to the default face (tofu, but never a crash).

Two pseudo-scripts, neither of which is one anywhere else. **"symbol"**,
because a Latin face is not a symbol face (:data:`_SYMBOL_RANGES`). **"emoji"**,
the only one where the *face* is awkward rather than the choice of it: it draws
in its own colours (``embedded_color``) and is very often available at one
fixed pixel size and no other, which is what :data:`_STRIKES` and
:func:`_draw_scaled` are for. A symbol face is not an emoji face either.

The measured evidence behind every table here — face coverage, the RTL
whole-line rule, colour-emoji strikes, shaping and the lookup cost — is in
mpvtk/GUIDE.md section 12.
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
    # **Order here is preference, not correctness** -- `font()` rejects a
    # candidate that cannot draw the run in hand, so nothing below depends on
    # its position for a string to render. What it still decides is *regional
    # glyph form*, and that is why the Japanese entries are deliberately NOT
    # demoted: Han unification means one codepoint has different shapes in
    # Japanese and Chinese typography, coverage cannot tell them apart, and
    # the shim has no per-item language to ask. Promoting `msyh.ttc` would
    # fix Simplified Chinese by giving every Japanese title Chinese shapes.
    #
    # Measured 2026-09-08 on a stock Windows 10 -- and it is the reason no
    # ordering of this list was ever going to be right: `msgothic.ttc` has
    # zh-Hant and ja and misses four of #736's eight codepoints; `msyh.ttc`
    # and `simsun.ttc` have the Chinese and no Hangul at all; `malgun.ttf`
    # has the Hangul and misses most Han. Four languages, one bucket, no
    # single face. See mpvtk/GUIDE.md section 12.6.
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
        # Microsoft YaHei and JhengHei -- the Simplified and Traditional
        # Chinese UI faces, shipped with Windows since Vista/7. Appended
        # rather than promoted, for the reason above: with the coverage
        # check they are what a Chinese title reaches once the Japanese
        # faces decline it, which is a modern face instead of `simsun.ttc`.
        "msyh.ttc",
        "msjh.ttc",
        "simsun.ttc",
        "malgun.ttf",
    ],
    # Arabic is RTL, so the whole-line rule under "hebrew" applies -- but
    # here it has no clean answer. NotoSansArabic has the neutrals and no
    # A-Z, so a Latin *word* inside an Arabic line is boxes; the face that
    # would fix that gives up three quarters of the presentation forms,
    # which is most of the script. Left as it is, knowingly: coverage
    # figures in mpvtk/GUIDE.md section 12.1.
    "arabic": [
        "NotoSansArabic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansArabic-Regular.ttf",
        "/System/Library/Fonts/GeezaPro.ttc",
        "arial.ttf",
    ],
    # **The order here is load-bearing and is the opposite of every other
    # list in this file.** Hebrew is RTL, so `has_rtl` gives the whole line
    # to ONE face and there is no Latin run to fall to -- that face has to
    # carry the neutrals as well as the script, or the full stop and every
    # year in a title come out as boxes. So the script-specific face goes
    # LAST, not first. Per-face coverage: mpvtk/GUIDE.md section 12.1.
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
    # `mangal.ttf` alone was a Windows entry that is not on Windows:
    # **Mangal became an optional feature in Windows 10** ("Hindi
    # Supplemental Fonts"), so on a stock install this list resolved
    # nothing, fell through to the Latin backstop and drew Hindi as boxes
    # with Arial. `Nirmala.ttf` (Nirmala UI) ships by default and covers
    # the script completely -- measured 2026-09-08, 12/12 codepoints.
    # Mangal stays behind it for the hosts that do have it.
    #
    # Found by `TestTheHostsOwnInventory`, which asks the host what it has
    # rather than trusting this list -- every Devanagari assertion here was
    # about `script_of` and none could see it.
    "devanagari": [
        "NotoSansDevanagari-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansDevanagari-Regular.ttf",
        "Nirmala.ttf",
        "mangal.ttf",
    ],
    "thai": [
        "NotoSansThai-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
        # Leelawadee UI is Windows' own Thai face; tahoma also has it and
        # stays behind it. `FreeSerif` is the one the Flatpak runtime has.
        "LeelawUI.ttf",
        "tahoma.ttf",
        "FreeSerif.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    ],
    # Nine scripts that had **no bucket at all** until now: every codepoint
    # in them answered "latin", got the Latin face and drew as boxes -- on
    # Linux boxes with the whole Noto set installed and on a stock Windows
    # 10, both of which ship a face for every one of them. Georgian and
    # Armenian were in the same position and were invisible because DejaVu
    # happens to cover them, which is what made this look like nothing.
    #
    # Every name below was **measured on the host that has it**, never
    # guessed -- `mangal.ttf` above is what guessing costs. Windows 10
    # inventory, measured 2026-09-08: `Nirmala.ttf` covers all ten Indic
    # scripts by itself, `LeelawUI.ttf` covers Lao and Khmer (and Thai),
    # `mmrtext.ttf` Myanmar, `himalaya.ttf` Tibetan, `ebrima.ttf` Ethiopic,
    # `gadugi.ttf` Cherokee, `monbaiti.ttf` Mongolian, `sylfaen.ttf` and
    # Calibri Georgian. `FreeSerif` is a broad Indic backstop and is in the
    # Flatpak runtime, which matters on a host with no Indic font at all.
    #
    # **One bucket for the ten Indic scripts, and the coverage check is why
    # that works.** They share no codepoints, so unlike CJK there is no
    # regional-form problem; Noto has a file per script and Windows has one
    # face for all of them, and `font()` picks per run by what the run's
    # codepoints actually draw as. Order here is preference only.
    "indic": [
        "NotoSansBengali-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansBengali-Regular.ttf",
        "NotoSansGurmukhi-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansGurmukhi-Regular.ttf",
        "NotoSansGujarati-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansGujarati-Regular.ttf",
        "NotoSansOriya-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansOriya-Regular.ttf",
        "NotoSansTamil-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansTamil-Regular.ttf",
        "NotoSansTelugu-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansTelugu-Regular.ttf",
        "NotoSansKannada-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansKannada-Regular.ttf",
        "NotoSansMalayalam-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMalayalam-Regular.ttf",
        "NotoSansSinhala-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansSinhala-Regular.ttf",
        # One face for all ten on Windows.
        "Nirmala.ttf",
        "FreeSerif.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSerif.ttf",
    ],
    "lao": [
        "NotoSansLao-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansLao-Regular.ttf",
        "LeelawUI.ttf",
        "DejaVuSans.ttf",
    ],
    # **Serif, not Sans**: Noto ships no `NotoSansTibetan`, and the guessed
    # name was caught by `TestTheHostsOwnInventory` on the first run rather
    # than by a bug report -- the same way `mangal.ttf` was.
    "tibetan": [
        "NotoSerifTibetan-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSerifTibetan-Regular.ttf",
        "himalaya.ttf",
    ],
    "myanmar": [
        "NotoSansMyanmar-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMyanmar-Regular.ttf",
        "mmrtext.ttf",
    ],
    "georgian": [
        "NotoSansGeorgian-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansGeorgian-Regular.ttf",
        # DejaVu has Georgian, which is why this script never looked broken.
        "DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "sylfaen.ttf",
    ],
    "ethiopic": [
        "NotoSansEthiopic-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf",
        "ebrima.ttf",
        "FreeSerif.ttf",
    ],
    "cherokee": [
        "NotoSansCherokee-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansCherokee-Regular.ttf",
        "gadugi.ttf",
        "FreeSans.ttf",
    ],
    "khmer": [
        "NotoSansKhmer-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansKhmer-Regular.ttf",
        "LeelawUI.ttf",
    ],
    "mongolian": [
        "NotoSansMongolian-Regular.ttf",
        "/usr/share/fonts/truetype/noto/NotoSansMongolian-Regular.ttf",
        "monbaiti.ttf",
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
    # Colour emoji. Not a script, and the one list where the *pixel size* is
    # part of the problem: NotoColorEmoji is a CBDT bitmap face with a single
    # 109px strike and raises `OSError: invalid pixel size` at any other one,
    # so `_load` probes strikes for this script alone and `draw_text` shrinks
    # what the strike draws (mpvtk/GUIDE.md section 12.2). The two monochrome
    # outline faces at the end load at the asked size and need none of that.
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

#: Where a script's face falls back to *before* the Latin one, and both
#: entries are measured rather than offered.
#:
#: **emoji -> symbol**: with no emoji font at all, "⭐" drawn by the Latin
#: face is a box and drawn by the symbol face is the monochrome star it has
#: always been. Falling straight to Latin would make the emoji bucket a
#: regression on exactly the hosts it cannot help.
#:
#: **symbol -> emoji**: measured 2026-09-08 on Debian, of the 1108
#: codepoints :data:`_SYMBOL_RANGES` claims, **223 are drawn by nothing in
#: the symbol chain or the Latin one behind it, and the emoji chain draws
#: 219 of them** -- `Symbola` is on that list and is a symbol face in every
#: respect except the bucket it sits in. The colour faces are not reachable
#: this way and do not need to be: `_load` only probes strikes for the emoji
#: script, so a CBDT face that opens at one fixed size is simply skipped
#: here, and #713's star still comes from the symbol chain's own first
#: answer (a control test holds that).
#:
#: **No other edge is declared, because no other edge changed an answer.**
#: A `cjk` edge would rescue 22 symbol codepoints and all 22 are already in
#: the 219; the 33 Latin codepoints nothing draws are the C0/C1 controls,
#: which must not be rescued by anything. A speculative entry here
#: pre-authorises a face for a run nobody has seen.
_FALLBACK_SCRIPTS = {"emoji": ("symbol",), "symbol": ("emoji",)}

#: Pixel sizes a bitmap-strike face may be available at, tried in
#: `_strike_order` when the asked-for size is refused. The Apple entries are
#: a **probe set, not a verified inventory** -- being wrong either way is
#: cheap, and mpvtk/GUIDE.md section 12.2 says how to settle it on a Mac.
_STRIKES = (16, 20, 26, 32, 40, 48, 52, 64, 96, 109, 128, 136, 160)

# Bold variants, tried before the regular list for bold requests.
_BOLD = {
    "latin": ["DejaVuSans-Bold.ttf",
              "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
              "NotoSans-Bold.ttf", "arialbd.ttf"],
    "cjk": ["NotoSansCJK-Bold.ttc",
            "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc"],
}

_cache = {}          # (script, size, bold) -> the face asked for with no text
_chains = {}         # (script, size, bold) -> [(name, face), ...] opened so far
# (font name, char) -> whether that face has a glyph. Unbounded on purpose:
# the ceiling is the codepoints actually drawn times the faces asked, and a
# bounded clear would re-pay 45 us per entry to save a few hundred KB.
_coverage = {}
_notdef = {}         # (font name, open size) -> that face's no-glyph renders
_resolved = {}       # (script, bold) -> path/name that loaded, or None

#: Codepoints Unicode guarantees will never be assigned, so what a face
#: renders for one is that face's own "no glyph" mark. Three rather than
#: one because a face that maps one anyway would make everything look
#: covered; they are required to agree, which
#: `tests/test_mpvtk_pilfont.py:TestCjkCoverage` holds against every
#: candidate installed on the host.
_NOTDEF_PROBES = ("\U000FFFFF", "\U0010FFFF", "\U000FFFFE")


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
#: **The BMP half must stay small ranges, never whole blocks.** U+2605 and
#: U+2713 sit inside U+2600-27BF and no colour face draws them, so sweeping
#: the block in here puts #713's star straight back to tofu -- the
#: text-presentation neighbours belong on the symbol face
#: (mpvtk/GUIDE.md section 12.2).
#:
#: The astral half can be whole blocks: up there the current answer is the
#: ``cp >= 0x2E80`` CJK catch-all and it is already tofu, so the ~130
#: text-presentation pictographs Noto Color Emoji lacks are no worse off.
#: Carving them out would be a table nobody could check.
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
    against emoji-data.txt; this is the form the repaint path can afford,
    since `script_of_char` runs per character and `strips._ellipsize`
    re-measures a caption once per character while it trims. Measured at ~70x
    per character (mpvtk/GUIDE.md section 12.5); the three sets together are
    ~3,800 codepoints.
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
    # One comparison for the nine table-driven scripts. Deliberately after
    # thai and devanagari, which are inside this band and have their own
    # buckets, and deliberately falling through rather than returning when
    # nothing matches.
    if 0x0980 <= cp <= 0x18AF:
        for lo, hi, script in _BAND_SCRIPTS:
            if lo <= cp <= hi:
                return script
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

    **Except that RTL outranks everything, wherever it appears.** This
    answer becomes the *whole line's* face when `has_rtl` says so, and
    "first non-Latin wins" handed "進撃の巨人 مسلسل" to a CJK face -- which
    draws the Arabic as boxes, unjoined, in logical order. The trade is
    already decided at :data:`_RTL_RANGES`: *reordered text is a wrong line
    where tofu is only an ugly one*. So the CJK is what becomes tofu here,
    and a line with no RTL in it is untouched.

    **A symbol only wins when there is nothing else in the string.** It is a
    face for the odd glyph, not for words, and this answer is used for two
    things that would be wrong for: the line height a caller reserves
    (`components/banner.py`, `mpvtk_browser/cast.py`), and the face handed to
    ``draw_text`` for a *substring* the symbol need not have survived into.
    One star choosing it would re-typeset whole paragraphs, and for an RTL
    line draw every word as a box (mpvtk/GUIDE.md section 12.4).

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
    first = None
    for ch in text or "":
        script = script_of_char(ord(ch))
        if script in ("hebrew", "arabic"):
            # Returned rather than remembered: no later character can
            # outrank it, so there is nothing left to scan for.
            return script
        if script in ("symbol", "emoji"):
            saw_symbol = True
        elif script != "latin":
            if first is None:
                first = script       # cjk / thai / devanagari
        elif not ch.isspace():
            saw_word = True
    if first is not None:
        return first
    # No ``has_rtl`` guard needed and none added: every RTL codepoint maps
    # to hebrew or arabic and has returned above (an invariant a test
    # holds), so anything reaching here has no RTL in it at all. A
    # redundant condition would read as load-bearing.
    return "symbol" if saw_symbol and not saw_word else "latin"


#: ``(lo, hi, script)`` for the scripts that need nothing but a range and a
#: candidate list. Reached through **one** comparison in
#: :func:`script_of_char` -- every entry lives inside U+0980..U+18AF, so a
#: single band test in front of this loop keeps the cost off Latin, CJK,
#: emoji and symbols entirely.
#:
#: Thai (U+0E00..U+0E7F) and Devanagari (U+0900..U+097F) sit inside that
#: band and are deliberately **not** here: they have their own buckets and
#: are answered before the gate. A codepoint in the band that matches
#: nothing here falls through to the rest of the chain rather than being
#: claimed, so the two orderings cannot silently swap.
_BAND_SCRIPTS = ((0x0980, 0x0DFF, "indic"),      # Bengali..Sinhala
                 (0x0E80, 0x0EFF, "lao"),
                 (0x0F00, 0x0FFF, "tibetan"),
                 (0x1000, 0x109F, "myanmar"),
                 (0x10A0, 0x10FF, "georgian"),
                 (0x1200, 0x137F, "ethiopic"),
                 (0x13A0, 0x13FF, "cherokee"),
                 (0x1780, 0x17FF, "khmer"),
                 (0x1800, 0x18AF, "mongolian"))

#: Characters that join what is around them into one glyph and must never
#: start a run of their own, because **shaping does not cross a run
#: boundary** -- split "👩‍💻" at the joiner and Raqm draws two emoji where
#: the font has one glyph (mpvtk/GUIDE.md section 12.3). U+20E3 rides with
#: the digit *before* it, which is what makes "1️⃣" a keycap.
_JOINERS = frozenset((0x200D,            # zero width joiner
                      0xFE0E, 0xFE0F,    # text / emoji variation selectors
                      0x20E3))           # combining enclosing keycap


def runs(text):
    """``[(script, chunk), ...]`` in order, adjacent same-script chars merged.

    **This is the whole fix for mixed strings.** A face named for a script is
    very often a face for *only* that script, so "進撃の巨人 (2013)" came out
    with the year as four tofu boxes (mpvtk/GUIDE.md section 12.5). Splitting
    draws each run with its own face, which is what libass already does for
    the text we hand it.

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


#: Hebrew and Arabic. A string containing any of these is drawn with **one
#: face**: Pillow reorders bidi within a single draw call and cannot across
#: several, and reordered text is a wrong line where tofu is only an ugly
#: one (mpvtk/GUIDE.md section 12.1).
#:
#: **Kept in step with `script_of_char` by a test** — every codepoint here
#: must map to "hebrew" or "arabic", since this table decides a line gets ONE
#: face and `script_of_char` decides which. They disagreed once, and an
#: Arabic line in presentation forms was drawn end to end with a CJK face.
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


#: Where a Flatpak's *host* fonts are bind-mounted. **This is the whole of
#: why the sandbox needs special handling**: Pillow does not use fontconfig
#: -- it searches a fixed list of directories -- so inside a Flatpak it sees
#: only the runtime's own ``/usr/share/fonts``, and the user's fonts are
#: over here instead.
#:
#: Measured 2026-09-08 against the shipped 3.0.0 Flatpak: the
#: ``org.freedesktop.Platform`` 25.08 runtime carries **95 font files and no
#: CJK, Thai or Indic face at all** (DejaVu, Liberation, FreeSans, Adwaita,
#: Caladea, Carlito, Cantarell, plus NotoColorEmoji), while the host's 4243
#: fonts -- including ``NotoSansCJK-Regular.ttc``, verified to draw 莲 --
#: were sitting in here unreachable. Every Flatpak user with a Chinese,
#: Japanese, Korean, Thai or Hindi library saw a screen of boxes.
_HOST_FONT_DIRS = ("/run/host/fonts", "/run/host/local-fonts",
                   "/run/host/user-fonts")

#: ``basename.lower() -> path`` over :data:`_HOST_FONT_DIRS`, or ``{}``.
#: Built at most once, and only when a candidate has already failed to load
#: by its own name, so a host that is not a Flatpak never walks anything.
_host_index = None


def _host_fonts():
    global _host_index
    if _host_index is None:
        _host_index = {}
        for directory in _HOST_FONT_DIRS:
            if not os.path.isdir(directory):
                continue
            for root, _dirs, files in os.walk(directory):
                for filename in files:
                    if filename.lower().endswith((".ttf", ".otf", ".ttc",
                                                  ".otc")):
                        # First wins, so the earlier directory in the tuple
                        # takes precedence over a later duplicate.
                        _host_index.setdefault(filename.lower(),
                                               os.path.join(root, filename))
    return _host_index


def _resolutions(name):
    """The paths to try for one candidate: its own name, then the same
    **basename** under the host's font tree.

    A generator because the second half must not be reached on a normal
    host: `_load` only advances it when the name has already failed at
    every size, so the walk in :func:`_host_fonts` is paid by a Flatpak and
    by nobody else.

    The basename is what carries over, for absolute candidates as much as
    bare ones -- ``/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc``
    does not exist inside the sandbox, and the file it names does.
    """
    yield name
    alt = _host_fonts().get(os.path.basename(name).lower())
    if alt and alt != name:
        yield alt


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
        for candidate in _resolutions(name):
            for want in [size] + (_strike_order(size) if strikes else []):
                try:
                    return (ImageFont.truetype(candidate, want), candidate,
                            want)
                except (OSError, IOError):
                    continue
    return None, None, size


def _notdef_refs(fnt, name):
    """What ``fnt`` renders for a codepoint that cannot exist.

    Memoized per (name, open size) because the *bitmap* is size-dependent
    even though the coverage verdict below is not: three renders cost 105 us
    on Linux and 19 us on Windows (measured), which is worth paying once per
    face rather than once per batch of characters.
    """
    key = (name, getattr(fnt, "size", None))
    hit = _notdef.get(key)
    if hit is None:
        hit = []
        for probe in _NOTDEF_PROBES:
            mask = fnt.getmask(probe, mode="L")
            hit.append((mask.size, bytes(mask)))
        _notdef[key] = hit
    return hit


def _draws(fnt, name, ch):
    """Whether ``fnt`` has a glyph for ``ch``, by rendering it and comparing
    against that face's own no-glyph mark.

    **A render comparison because Pillow exposes no cmap.** A
    ``FreeTypeFont`` offers ``getmask``, ``getbbox``, ``getlength`` and
    ``getname`` and nothing that maps a character to a glyph id, so the
    alternative is a fontTools dependency for a check this cheap.

    Measured 2026-09-08: 45 us per codepoint on Linux, 7.5 us on Windows,
    paid once per (face, codepoint) for the session. **The verdict is the
    same at 8px and at 96px on every face tried**, which is why the memo is
    keyed by the face's name and shared across every size it is opened at.

    Two faces measured here answer in ways that look wrong and are not.
    ``simsun.ttc`` renders a *blank* no-glyph mark, and
    ``NotoColorEmoji.ttf`` renders one identical to its space -- both are
    harmless because the only glyphs that render blank are whitespace, and
    :func:`_covers` skips it. Do not "fix" this by requiring the mark to be
    non-blank; that assertion fails on a stock Debian box.
    """
    key = (name, ch)
    hit = _coverage.get(key)
    if hit is None:
        try:
            mask = fnt.getmask(ch, mode="L")
            hit = (mask.size, bytes(mask)) not in _notdef_refs(fnt, name)
        except (OSError, ValueError, AttributeError):
            # Pillow's bitmap default, or a face that will not render this
            # at all. "Covered" is the answer that keeps the behaviour
            # there was before this check existed, which for an
            # unanswerable face is the right one.
            hit = True
        _coverage[key] = hit
    return hit


def _covers(fnt, name, text, script):
    """Whether ``fnt`` can draw the part of ``text`` that chose ``script``.

    **Only that part, and that is the whole design of this check.** Asking
    a face to cover the whole *string* would quietly overturn two decisions
    this module made on measured evidence and wrote down:
    ``NotoSansArabic-Regular.ttf`` is kept first for Arabic although it has
    no ASCII at all (:data:`_CANDIDATES`, and the comment above it says
    why), and the Hebrew list is ordered the opposite way round for
    precisely the neutrals an RTL line cannot split off. Requiring only the
    selecting script's codepoints leaves both answering exactly as they did,
    while a Japanese face stops being handed a Simplified Chinese title
    (#736).

    Everything else in the string is somebody else's run: ``runs`` splits a
    mixed title and each piece resolves its own face.
    """
    for ch in text or "":
        if ch.isspace() or script_of_char(ord(ch)) != script:
            continue
        if not _draws(fnt, name, ch):
            return False
    return True


def _opened(script, size, bold, index):
    """``(name, face)`` at ``index`` in this script's candidate order, or
    None past the end. Candidates are opened lazily and kept, so the common
    case is still one ``ImageFont.truetype`` per (script, size, weight).

    Measured 2026-09-08 on the Windows VM, where the walk is longest: a
    Korean title reaches ``malgun.ttf`` on the fourth candidate for 1.81 ms
    all in, once per chain.
    """
    key = (script, size, bool(bold))
    state = _chains.get(key)
    if state is None:
        names = []
        if bold:
            names += _BOLD.get(script, [])
        names += _CANDIDATES.get(script, [])
        for other in _FALLBACK_SCRIPTS.get(script, ()):
            names += _CANDIDATES.get(other, [])
        if script != "latin":
            # Better a Latin face than Pillow's 11px bitmap default. It will
            # not satisfy `_covers` for a non-Latin run, which is correct:
            # it is the tofu backstop, not a candidate.
            if bold:
                names += _BOLD["latin"]
            names += _CANDIDATES["latin"]
        state = {"names": names, "next": 0, "faces": []}
        _chains[key] = state
    while len(state["faces"]) <= index and state["next"] < len(state["names"]):
        name = state["names"][state["next"]]
        state["next"] += 1
        fnt, got, native = _load([name], size, strikes=(script == "emoji"))
        if fnt is not None:
            _stamp(fnt, script, size, bold, native)
            state["faces"].append((got, fnt))
    if index < len(state["faces"]):
        return state["faces"][index]
    return None


def _stamp(fnt, script, size, bold, native):
    """What it was asked for, so ``draw_text`` can ask for the same in
    another script. ``size`` is on ``FreeTypeFont`` already but the weight is
    not, and the bitmap default has neither. The script rides along too, so
    a face can be asked whether it is the right one for the run in hand --
    see :func:`_run_face`."""
    try:
        fnt._jms_size, fnt._jms_bold = size, bool(bold)
        fnt._jms_script = script
        # The size it OPENED at, which is the size everything it draws and
        # measures comes back in. `_scale_of` is the only reader.
        fnt._jms_native = native
    except AttributeError:         # a face that will not be annotated
        pass


def font_for(text, size, bold=False):
    """A PIL font able to render ``text`` at ``size``. Falls back to the Latin
    face (and finally Pillow's bitmap default) when nothing better is
    installed."""
    return font(script_of(text), size, bold, text=text)


def font(script, size, bold=False, text=None):
    """A PIL face for ``script``, able to draw ``text`` if it is given.

    **"The first candidate that opens" is not the same question as "the
    first candidate that works", and the difference is #736.** A stock
    Windows 10 resolves ``msgothic.ttc`` for every CJK string, and MS Gothic
    is a *Japanese* face: measured, it is missing four of the eight
    codepoints in that issue's own title, so half a Simplified Chinese
    library drew as tofu. No ordering of :data:`_CANDIDATES` fixes that,
    because no face on that host covers all four CJK languages -- msyh and
    simsun have the Chinese and no Hangul, malgun has the Hangul and little
    Han. So ``text`` decides, and the list only expresses preference.

    Without ``text`` this answers exactly what it always did, from the same
    one-lookup cache: the first candidate that opens. That is the answer for
    a caller who wants a face's *metrics* rather than its glyphs
    (`components/banner.py` reserving a line), and it is the fallback when
    nothing in the chain covers the string -- tofu, but never a crash.
    """
    key = (script, size, bool(bold))
    primary = _cache.get(key)
    if primary is None:
        entry = _opened(script, size, bold, 0)
        if entry is None:
            from PIL import ImageFont

            fnt = ImageFont.load_default()
            _stamp(fnt, script, size, bold, size)
            entry = (None, fnt)
        name, primary = entry
        if _resolved.get((script, bool(bold))) != name:
            _resolved[(script, bool(bold))] = name
            if name is None and script != "latin":
                log.info("no font found for script %r; text may not render",
                         script)
        _cache[key] = primary
    if not text:
        return primary
    index = 0
    while True:
        entry = _opened(script, size, bold, index)
        if entry is None:
            return primary
        name, fnt = entry
        if name is None or _covers(fnt, name, text, script):
            return fnt
        index += 1


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


def _same_size(fnt, script, text=None):
    """The face for ``script`` at the size and weight ``fnt`` was made with.

    Callers hold a font, not a (size, bold) pair -- they need its metrics for
    line spacing -- so the pair rides along on the object, stamped by
    ``font()``. A font from anywhere else falls back to its own ``size`` and
    regular weight, which is right for every caller here and wrong for
    nobody: the worst case is a bold run drawn regular.
    """
    return font(script, getattr(fnt, "_jms_size", getattr(fnt, "size", 12)),
                getattr(fnt, "_jms_bold", False), text=text)


def _run_face(fnt, script, text=None):
    """The face to draw a run of ``script`` with, given the font a caller
    chose — possibly for a longer string this run was wrapped out of.

    ``fnt`` itself whenever it is already that script, or when it carries no
    stamp at all: a caller who built a face by hand gets exactly the face it
    passed, which is what keeps a single-run string drawn byte for byte as
    it was before any of this existed.

    **Single-run only.** The multi-run paths use :func:`_same_size`, which
    resolves a real face per script unconditionally. For an unstamped font the
    two must not agree: "leave the caller's face alone" is right for a string
    that face can draw and disastrous for the mixed string this whole path
    exists to handle, where it puts the tofu straight back.

    For a stamped font the two agree **as long as the caller resolved the face
    for the text it goes on to draw** -- every caller here does, and the ones
    that ellipsize or wrap draw a subset plus a Latin "…", which is its own
    run. That is the contract now, not an identity: since `font()` picks by
    coverage, the same (script, size, weight) can legitimately answer with two
    different faces, so a face resolved for one string and reused for another
    the first face cannot draw would be returned unchanged. No guard is added
    for it, because a guard here would be a second place deciding what
    `font()` already decides -- if a caller ever needs that, it should pass
    the text it draws.
    """
    stamped = getattr(fnt, "_jms_script", None)
    if stamped is None or stamped == script:
        return fnt
    return _same_size(fnt, script, text)


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
    return ((lambda script, text=None: _run_face(fnt, script, text)),
            (lambda script, text=None: _same_size(fnt, script, text)))


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
        # The whole line, so the face is chosen against every codepoint in
        # it that belongs to the line's script -- see `_covers`.
        return parts, single(script_of(text), text), per_run
    if len(parts) == 1 and parts[0][0] != "emoji":
        return parts, single(parts[0][0], parts[0][1]), per_run
    return parts, None, per_run


def _measure(text, fnt, faces, measure):
    if not text:
        return 0.0
    parts, whole, per_run = _split(text, fnt, faces)
    if whole is not None:
        return measure(text, whole) * _scale_of(whole)
    total = 0.0
    for script, chunk in parts:
        face = per_run(script, chunk)
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
    composited -- the only route for a CBDT face with one fixed strike
    (mpvtk/GUIDE.md section 12.2).

    Two things here look wrong and are not. **Not premultiplied** before the
    resize: the glyphs are already antialiased into transparent *black*, so
    there is no halo to remove and premultiplying without dividing back out
    darkens every edge. And **the alpha rides in the mask, not the source**,
    or a transparent plate squares it and the run comes out thin.
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

    fonts = [per_run(script, chunk) for script, chunk in parts]
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
            # `embedded_color` for emoji only, and only onto a target that
            # can hold colour. On a monochrome face it renders the same
            # picture but not the same *bytes*, and every other run here is
            # one this module promises to leave exactly as it found it;
            # on a non-RGB target Pillow raises. A greyscale emoji is the
            # right degradation for a greyscale plate, an exception is not.
            # (mpvtk/GUIDE.md section 12.2)
            draw.text((x, baseline), chunk, font=face, fill=fill,
                      anchor="ls",
                      embedded_color=(script == "emoji"
                                      and draw.mode in ("RGB", "RGBA")))
        x += draw.textlength(chunk, font=face) * scale


def clear_cache():
    global _host_index

    _host_index = None
    _cache.clear()
    _chains.clear()
    _coverage.clear()
    _notdef.clear()
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
