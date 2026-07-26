"""The detail-screen banner compositor.

Moved verbatim from ``TilesMixin``. ``compose_banner`` was a ``classmethod``
purely so it could reach ``cls._wrap_pil``; as module-level functions that
indirection disappears.
"""

from ...mpvtk.scaling import px


def wrap_pil(draw, text, font, max_w, max_lines=2):
    """Word-wrap ``text`` to ``max_lines``, ellipsizing the last line.
    Falls back to breaking mid-word for a single word too long to fit.

    Was ``TilesMixin._wrap_pil``.
    """
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if not cur or draw.textlength(trial, font=font) <= max_w:
            cur = trial
            continue
        lines.append(cur)
        cur = word
        if len(lines) == max_lines:
            break
    if cur and len(lines) < max_lines:
        lines.append(cur)
    if not lines:
        return [text]
    # The last line absorbs whatever didn't fit, ellipsized.
    consumed = len(" ".join(lines).split())
    if consumed < len(words) or draw.textlength(
            lines[-1], font=font) > max_w:
        last = lines[-1]
        if consumed < len(words):
            last = " ".join([last] + words[consumed:])
        while last and draw.textlength(last + "…", font=font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


def compose_banner(image, box, title=None, meta=None, context=None):
    """Crop ``image`` to the banner box and bake the heading over a
    bottom-up dark gradient.

    Stacked bottom-up: meta, title (wrapped to two lines), then the
    context line above it. Text is sized off the banner height and
    stays small enough that a long episode title reads in full.

    Was ``TilesMixin._compose_banner``.
    """
    from PIL import ImageDraw

    from ...imageutil import (apply_dark_gradient, pil_font,
                              scale_to_cover)

    # box is PHYSICAL here; the bare sizes below are logical constants,
    # so they convert. The h//6 and w//40 terms are already physical
    # because they derive from box.
    w, h = box
    canvas = scale_to_cover(image.convert("RGBA"), w, h)
    if not title:
        return canvas
    canvas = apply_dark_gradient(canvas, height_fraction=0.7,
                                 max_alpha=215)
    draw = ImageDraw.Draw(canvas)
    margin = max(px(18), w // 40)
    avail = w - 2 * margin
    # Smaller than it was: the heading has up to three stacked lines to
    # fit inside the gradient now, not one.
    size = max(px(20), min(px(34), h // 6))
    y = h - margin
    if meta:
        f = pil_font(int(size * 0.6), text=meta)
        asc, desc = f.getmetrics()
        draw.text((margin, y - asc - desc), meta, font=f,
                  fill=(200, 200, 200, 255))
        y -= asc + desc + px(6)
    f = pil_font(size, bold=True, text=title)
    asc, desc = f.getmetrics()
    for line in reversed(wrap_pil(draw, title, f, avail)):
        draw.text((margin, y - asc - desc), line, font=f,
                  fill=(255, 255, 255, 255))
        y -= asc + desc + px(2)
    if context:
        f = pil_font(int(size * 0.62), text=context)
        asc, desc = f.getmetrics()
        line = wrap_pil(draw, context, f, avail, max_lines=1)[0]
        draw.text((margin, y - asc - desc + px(2)), line, font=f,
                  fill=(215, 215, 215, 255))
    return canvas
