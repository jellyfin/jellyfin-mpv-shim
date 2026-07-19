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

import logging

log = logging.getLogger("mpvtk")

# Candidates per platform; Pillow searches the system font paths.
_CANDIDATES = [
    "DejaVuSans.ttf",  # Linux (and the demo's poster font)
    "segoeui.ttf",  # Windows
    "arial.ttf",
    "Helvetica.ttc",  # macOS
]

_MEASURE_SIZE = 128


def measure_font():
    """Returns {"font": family_name, "widths": {char: fraction}} or None
    when no measurable font / recent-enough Pillow is available."""
    try:
        from PIL import ImageFont
    except ImportError:
        return None
    for name in _CANDIDATES:
        try:
            font = ImageFont.truetype(name, _MEASURE_SIZE)
        except OSError:
            continue
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
            widths = {
                c: round(font.getlength(c) / _MEASURE_SIZE * factor, 4)
                for c in (chr(i) for i in range(32, 127))
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
        return {
            "font": family,
            "widths": widths,
            "mask_w": round(mask_w, 4),
        }
    return None
