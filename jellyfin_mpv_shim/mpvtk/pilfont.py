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


def script_of(text):
    """The script the string needs a font for. Latin/Cyrillic/Greek/Hebrew all
    map to "latin" (DejaVu covers them); the first character outside that wins,
    so a mixed "進撃の巨人 (2013)" resolves to cjk."""
    for ch in text or "":
        cp = ord(ch)
        if cp < 0x0590:            # ASCII, Latin ext, Greek, Cyrillic
            continue
        if 0x0590 <= cp <= 0x05FF:  # Hebrew — DejaVu has it
            continue
        if 0x0600 <= cp <= 0x06FF or 0xFB50 <= cp <= 0xFDFF:
            return "arabic"
        if 0x0900 <= cp <= 0x097F:
            return "devanagari"
        if 0x0E00 <= cp <= 0x0E7F:
            return "thai"
        if cp >= 0x2E80:           # CJK, kana, hangul, fullwidth forms
            return "cjk"
    return "latin"


def _load(names, size):
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
    _cache[key] = fnt
    return fnt


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
