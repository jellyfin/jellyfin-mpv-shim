"""Measured font metrics shared by Python layout and the Lua renderer.

Advances and pair kerning are measured with Pillow against a real font
file, applied to layout (layout.set_metrics) and pushed to the renderer
(mpvtk-metrics) with the family name for libass's ``\\fn``, so both sides
agree on text widths. The heuristic tables in layout.py/renderer.lua are
the fallback for what is not measured. See `mpvtk/GUIDE.md` section 6.3.
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
    # See pilfont._load -- same reason, and this one matters twice over:
    # what is measured here becomes layout.text_width's model of libass.
    from ..win_fribidi import preload

    preload()
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


# session state for on-demand measurement (extend_metrics)
_session_font = None
_session_factor = None
_seen_pairs = set()


def _dyn_font():
    global _session_font, _session_factor
    if _session_font is None:
        f = _load_font()
        if f is None:
            return None, None
        try:
            ascent, descent = f.getmetrics()
        except AttributeError:
            return None, None
        _session_factor = _MEASURE_SIZE / float(ascent + descent)
        _session_font = f
    return _session_font, _session_factor


def extend_metrics(m, texts):
    """Measure codepoints/pairs from ``texts`` that aren't in ``m`` yet
    (mutates m['widths']/m['kern']); returns True if anything was added.
    The unicode pair space can't be pre-enumerated, so this measures what
    real UI text actually uses. Limited to codepoints below U+2E80, which
    is what the base font covers; CJK keeps the ~1em heuristic because
    libass draws it with a fallback font Pillow is not measuring."""
    font, factor = _dyn_font()
    if font is None or not m:
        return False
    widths = m["widths"]
    kern = m.setdefault("kern", {})
    added = False
    for s in texts:
        prev = None
        for c in s:
            o = ord(c)
            if o < 0x20 or o >= 0x2E80:
                prev = None
                continue
            if c not in widths:
                widths[c] = round(
                    font.getlength(c) / _MEASURE_SIZE * factor, 4
                )
                added = True
            if prev is not None and (ord(prev) > 0x7E or o > 0x7E):
                p = prev + c
                if p not in _seen_pairs:
                    _seen_pairs.add(p)
                    d = (font.getlength(p) - font.getlength(prev)
                         - font.getlength(c))
                    if abs(d) > 0.6:
                        kern[p] = round(
                            d / _MEASURE_SIZE * factor, 4
                        )
                        added = True
            prev = c
    return added


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
    # The layout engine is part of the key because it changes the numbers
    # and nothing else here moves when it does: Raqm applies the font's GPOS
    # kerning and Basic does not (measured, DejaVuSans at 20px: "AVATAR" is
    # 81.94px unkerned against 75.20px kerned), and Pillow picks between them
    # on whether FriBiDi loaded -- a property of the MACHINE. Without it, a
    # Windows user updating into a build that ships FriBiDi (#689) keeps the
    # old unkerned numbers forever off a same-font, same-mtime cache hit.
    engine = _layout_engine()
    return "%s|%s|%s|%s|%s" % (path, mtime, pilver, engine, _METRICS_VERSION)


def _layout_engine():
    """``"raqm"`` / ``"basic"`` / ``"?"`` -- which one Pillow will use.

    Asked of Pillow rather than of ``win_fribidi.preload``'s result: a system
    FriBiDi on the search path counts, and so does a distro Pillow built
    against raqm outright.
    """
    try:
        from PIL import features

        return "raqm" if features.check("raqm") else "basic"
    except Exception:
        return "?"


def measure_font():
    """Returns {"font": family_name, "widths": {char: fraction}, ...} or
    None when no measurable font / recent-enough Pillow is available.

    ~40ms on a fast machine and up to ~1s on weak hardware, so it is disk
    cached (see ``_cache_key``) and warm launches read ~6KB of JSON."""
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
            # libass scales \fs to ascender+descender, not the em, so the
            # correction factor is folded in HERE and every width consumer
            # inherits it (GUIDE.md section 6.3; calibrate.py verifies).
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
