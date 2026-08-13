"""The detail-screen banner compositor.

Moved verbatim from ``TilesMixin``. ``compose_banner`` was a ``classmethod``
purely so it could reach ``cls._wrap_pil``; as module-level functions that
indirection disappears.
"""

from ...mpvtk import pilfont
from ...mpvtk.scaling import px
from .. import theme


def wrap_pil(draw, text, font, max_w, max_lines=2):
    """Word-wrap ``text`` to ``max_lines``, ellipsizing the last line.
    Falls back to breaking mid-word for a single word too long to fit.

    Measured through ``pilfont.text_length`` so a heading that mixes scripts
    is wrapped at the width it will be drawn at -- the two differ whenever a
    run has to change face (see ``pilfont.runs``).

    Was ``TilesMixin._wrap_pil``.
    """
    words, lines, cur = text.split(), [], ""
    for word in words:
        trial = (cur + " " + word).strip()
        if not cur or pilfont.text_length(draw, trial, font) <= max_w:
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
    if consumed < len(words) or pilfont.text_length(
            draw, lines[-1], font) > max_w:
        last = lines[-1]
        if consumed < len(words):
            last = " ".join([last] + words[consumed:])
        while last and pilfont.text_length(draw, last + "…", font) > max_w:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return lines


#: Fraction of the banner's height the inset artwork may take. Not the
#: whole height: it sits *on* the backdrop rather than replacing it, and
#: art running edge to edge reads as a second panel.
POSTER_H_FRAC = 0.78

#: ...and at most this fraction of the width, which is what keeps a 16:9
#: still from crowding the heading. A 2:3 poster never reaches it; an
#: episode still is limited by this rather than by the height.
POSTER_W_FRAC = 0.25

#: Fraction of the banner's AVAILABLE height a LANDSCAPE inset may take --
#: an episode still, a home video's frame -- against POSTER_H_FRAC's 0.78
#: for a poster.
#:
#: Available, and the two fractions are deliberately of different things.
#: POSTER_H_FRAC is of the whole banner and *defines* the margin it leaves
#: (0.11 of the height, top and bottom); this one is applied afterwards, to
#: a picture already sitting inside that, so measuring it against the whole
#: height too counted the margins twice over -- 60% of the banner is nearer
#: 70% of the space there is [iw: "we're doing 60% including the margin,
#: should be 60% of available space"]. The band it is a fraction of is the
#: one the horizontal margin implies: `h - 2 * margin`.
#:
#: The two need different numbers because the width limit above is doing
#: the work for one of them and not the other. A poster is height-limited
#: at any banner shape; a still is width-limited, so its height is
#: whatever `POSTER_W_FRAC * banner width / 1.78` comes to -- which tracks
#: the banner's WIDTH while the cap it is measured against tracks its
#: height. At the padded banner's own 2.67:1 that lands around 37% and
#: nothing is wrong. Widen the banner without heightening it
#: (`backdrop_full_width`, or a height capped by BANNER_MAX_H) and the
#: still grows until it is most of the header, with the heading squeezed
#: into what is left.
THUMB_H_FRAC = 0.60

#: Aspect at or above which inset artwork is a "thumbnail" rather than a
#: poster. jellyfin-web's own landscape threshold (see
#: TileRenderer.LANDSCAPE_RATIO); asked of the PICTURE rather than of the
#: item type, because that is what decides which limit binds -- and a
#: 4:3 still from an older show is as much a thumbnail as a 16:9 one.
THUMB_RATIO = 1.33


def poster_box(box):
    """``(x, y, max_w, max_h)`` the inset artwork is fitted inside.

    A **bounding** box, not the drawn size: what lands here is a 2:3 poster
    for a film or series and a 16:9 still for an episode, and both are
    drawn at their own shape within these limits. Sized off the banner so
    it scales with the header, and refused when there is no sensible room.
    """
    w, h = box
    max_h = int(h * POSTER_H_FRAC)
    max_w = int(w * POSTER_W_FRAC)
    margin = max(px(18), w // 40)
    if max_w <= 0 or max_h <= 0 or max_w > w // 3:
        return None
    return margin, (h - max_h) // 2, max_w, max_h


def compose_banner(image, box, title=None, meta=None, context=None,
                   poster=None):
    """Crop ``image`` to the banner box and bake the heading over a
    bottom-up dark gradient.

    Stacked bottom-up: meta, title (wrapped to two lines), then the
    context line above it. Text is sized off the banner height and
    stays small enough that a long episode title reads in full.

    Was ``TilesMixin._compose_banner``.
    """
    from PIL import ImageDraw

    from ...imageutil import (TOP_HEAVY, apply_dark_gradient, pil_font,
                              scale_to_cover)

    # box is PHYSICAL here; the bare sizes below are logical constants,
    # so they convert. The h//6 and w//40 terms are already physical
    # because they derive from box.
    w, h = box
    # TOP_HEAVY rather than a centre crop: the banner is a wide slot cut out
    # of a 16:9 backdrop, so the middle band of one is chins and shoulders.
    # See imageutil.TOP_HEAVY.
    canvas = scale_to_cover(image.convert("RGBA"), w, h,
                            gravity_y=TOP_HEAVY)
    if not title:
        return canvas
    canvas = apply_dark_gradient(canvas, height_fraction=0.7,
                                 max_alpha=215)
    draw = ImageDraw.Draw(canvas)
    margin = max(px(18), w // 40)
    # The poster is baked in with the heading, not drawn as a second node:
    # overlay bitmaps composite above all script ASS, so a node here would
    # be a separate overlay fighting this one for z-order -- and the whole
    # reason the heading is baked is that it must sit *over* the artwork.
    slot = poster_box(box) if poster is not None else None
    text_left = margin
    if slot is not None:
        # From what was actually drawn, not from the slot: the artwork is
        # fitted at its own aspect inside the bounds, so a 16:9 still and a
        # 2:3 poster end at different x and the heading follows each.
        right = _paste_poster(canvas, poster, slot)
        text_left = right + max(px(14), w // 60)
    avail = w - text_left - margin
    # Smaller than it was: the heading has up to three stacked lines to
    # fit inside the gradient now, not one.
    size = theme.baked_text(max(px(20), min(px(34), h // 6)))
    y = h - margin
    if meta:
        f = pil_font(int(size * 0.6), text=meta)
        asc, desc = f.getmetrics()
        # Ellipsized to the width, like the context line below and unlike
        # what this used to do: the meta line ends in the genres, and a film
        # carrying five of them drew straight off the right edge of the
        # backdrop. Nothing clips a baked bitmap back -- the text IS the
        # picture by the time the compositor sees it.
        line = wrap_pil(draw, meta, f, avail, max_lines=1)[0]
        pilfont.draw_text(draw, (text_left, y - asc - desc), line, f,
                          fill=(200, 200, 200, 255))
        y -= asc + desc + px(6)
    f = pil_font(size, bold=True, text=title)
    asc, desc = f.getmetrics()
    for line in reversed(wrap_pil(draw, title, f, avail)):
        pilfont.draw_text(draw, (text_left, y - asc - desc), line, f,
                          fill=(255, 255, 255, 255))
        y -= asc + desc + px(2)
    if context:
        f = pil_font(int(size * 0.62), text=context)
        asc, desc = f.getmetrics()
        line = wrap_pil(draw, context, f, avail, max_lines=1)[0]
        pilfont.draw_text(draw, (text_left, y - asc - desc + px(2)), line, f,
                          fill=(215, 215, 215, 255))
    return canvas


def _paste_poster(canvas, poster, slot):
    """Draw the artwork into ``slot`` at its own shape. Returns its right edge.

    **No letterbox plate and no rounded corners.** The slot has to hold two
    aspect ratios -- a 2:3 poster and a 16:9 episode still -- and the first
    attempt made that one fixed 2:3 box, so a still arrived boxed in black
    with rounded corners, which reads as a poster of a photograph rather
    than as the frame it is [iw]. Fitted inside the bounds instead and
    composed straight onto the backdrop.

    A drop shadow does the separating that the plate was doing. It is drawn
    onto the *canvas* rather than through ``imageutil.with_shadow``, which
    keeps the shadow inside the image's own bounds -- right for a logo with
    margins, and invisible on a full-bleed rectangle, where every edge is
    ink.
    """
    from PIL import Image, ImageFilter

    x, y, max_w, max_h = slot
    # The line every inset sits on, taken BEFORE the cap below. The heading
    # sits at the bottom of the banner, and art whose baseline agrees with
    # it reads as one block -- so what the cap may move is the artwork's
    # top edge, never this. Reducing max_h and then bottom-aligning inside
    # the reduced box moves the box, which left a capped thumbnail floating
    # in the middle of the header with nothing lined up with it.
    baseline = y + max_h
    art = poster.convert("RGBA")
    if art.height and art.width / art.height >= THUMB_RATIO:
        # A thumbnail, not a poster: hold it to a smaller share of the
        # banner. See THUMB_H_FRAC -- the slot's height limit is a poster's,
        # and a landscape picture reaches it only when the banner is wider
        # than the shape that limit was chosen for.
        #
        # Of the height INSIDE the margins, which `x` is (poster_box puts
        # the artwork's left edge exactly one margin in), not of the whole
        # banner.
        max_h = min(max_h, int((canvas.height - 2 * x) * THUMB_H_FRAC))
    art.thumbnail((max_w, max_h), Image.LANCZOS)
    aw, ah = art.size
    ay = baseline - ah

    span = max(aw, ah)
    blur = max(2.0, span / 25.0)
    drop = max(2, round(span / 40.0))
    shadow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    shadow.paste((0, 0, 0, 190), (x, ay + drop, x + aw, ay + ah + drop))
    shadow = shadow.filter(ImageFilter.GaussianBlur(blur))
    canvas.alpha_composite(shadow)
    canvas.paste(art, (x, ay), art)
    return x + aw
