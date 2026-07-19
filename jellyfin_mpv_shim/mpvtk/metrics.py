"""Measured font metrics shared by Python layout and the Lua renderer.

The heuristic char-width table in layout.py/renderer.lua is only a
fallback: at startup the app measures real glyph advances for printable
ASCII with Pillow against an actual font file, applies them to the
layout engine (layout.set_metrics) and pushes them to the renderer
(mpvtk-metrics script-message) together with the font family name for
libass (\\fn). Both sides then agree on accurate text widths — box
sizing, ellipsis and textbox cursor positioning all line up.

Non-ASCII glyphs still use the fallback width; a fuller table (or
shipping a UI font) is the production path.
"""

import json
import logging
import os
import sys
import tempfile

log = logging.getLogger("mpvtk")

# Bump when the measurement logic changes (invalidates disk caches).
_METRICS_VERSION = 1

# Candidates per platform; Pillow searches the system font paths.
_CANDIDATES = [
    "DejaVuSans.ttf",  # Linux (and the demo's poster font)
    "segoeui.ttf",  # Windows
    "arial.ttf",
    "Helvetica.ttc",  # macOS
]

_MEASURE_SIZE = 128


def _load_font():
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for name in _CANDIDATES:
        try:
            return ImageFont.truetype(name, _MEASURE_SIZE)
        except OSError:
            continue
    return None


def measure_kerning():
    """Pair-kerning adjustments as {2-char string: em fraction}, only
    non-zero pairs, with the libass fs factor folded in. ~9k getlength
    calls — call from a background thread and hot-swap the table in
    (layout.set_metrics + a second mpvtk-metrics push)."""
    font = _load_font()
    if font is None:
        return None
    try:
        ascent, descent = font.getmetrics()
        factor = _MEASURE_SIZE / float(ascent + descent)
        chars = [chr(i) for i in range(32, 127)]
        single = {c: font.getlength(c) for c in chars}
        kern = {}
        for a in chars:
            la = single[a]
            for b in chars:
                d = font.getlength(a + b) - la - single[b]
                if abs(d) > 0.6:  # font units of noise at 128px
                    kern[a + b] = round(d / _MEASURE_SIZE * factor, 4)
        log.info("mpvtk metrics: %d kerning pairs", len(kern))
        return kern
    except AttributeError:
        return None


def _cache_path():
    if sys.platform.startswith("win"):
        base = os.environ.get("LOCALAPPDATA") or tempfile.gettempdir()
    else:
        base = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser(
            "~/.cache"
        )
    return os.path.join(base, "mpvtk-metrics.json")


def _cache_key(font):
    path = getattr(font, "path", None) or "?"
    try:
        mtime = int(os.stat(path).st_mtime)
    except OSError:
        mtime = 0
    try:
        from PIL import __version__ as pilver
    except ImportError:
        pilver = "?"
    return "%s|%s|%s|%s" % (path, mtime, pilver, _METRICS_VERSION)


def measure_font():
    """Returns {"font": family_name, "widths": {char: fraction}, ...}
    or None when no measurable font / recent-enough Pillow is
    available.

    The full measurement (advances + ~9k kerning pairs) is ~40ms on a
    fast machine but could reach ~1s on weak hardware, so results are
    cached to disk keyed on the font file + Pillow version — every
    launch after the first reads ~6KB of JSON instead."""
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    font = _load_font()
    if font is not None:
        key = _cache_key(font)
        try:
            with open(_cache_path(), "r", encoding="utf-8") as f:
                obj = json.load(f)
            if obj.get("key") == key:
                log.info("mpvtk metrics: disk cache hit")
                return obj["data"]
        except (OSError, ValueError, KeyError):
            pass
        try:
            # libass (VSFilter compat) scales \fs to the font's
            # ascender+descender height, NOT the em size — so a glyph's
            # rendered advance is (advance/em) * fs * em/(asc+desc).
            # Fold that factor in here so every width consumer (layout
            # sizing, ellipsis, cursor/selection math) agrees with what
            # libass actually paints. Verified pixel-wise by
            # calibrate.py (DejaVu Sans: factor 0.859, ratios ~1.00).
            ascent, descent = font.getmetrics()
            factor = _MEASURE_SIZE / float(ascent + descent)
            # printable ASCII + Latin-1 supplement (é, ü, ñ, …); other
            # scripts use the fallback widths (fullwidth heuristic for
            # CJK)
            chars = [chr(i) for i in range(32, 127)]
            chars += [chr(i) for i in range(0xA1, 0x100)]
            widths = {
                c: round(font.getlength(c) / _MEASURE_SIZE * factor, 4)
                for c in chars
            }
            mask_w = font.getlength("•") / _MEASURE_SIZE * factor
            if not 0.1 < mask_w < 1.5:  # glyph missing/degenerate
                mask_w = 0.55
            family = font.getname()[0]
        except AttributeError:  # Pillow < 8: no getlength
            return None
        log.info(
            "mpvtk metrics: measured %s (libass factor %.3f)",
            family,
            factor,
        )
        data = {
            "font": family,
            "widths": widths,
            "mask_w": round(mask_w, 4),
            "kern": measure_kerning() or {},
        }
        try:
            tmp = _cache_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"key": key, "data": data}, f)
            os.replace(tmp, _cache_path())
        except OSError:
            log.debug("mpvtk metrics: cache not written", exc_info=True)
        return data
    return None
