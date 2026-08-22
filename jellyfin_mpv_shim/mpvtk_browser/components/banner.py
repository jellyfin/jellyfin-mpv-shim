"""The detail-screen banner compositor.

Moved verbatim from ``TilesMixin``. ``compose_banner`` was a ``classmethod``
purely so it could reach ``cls._wrap_pil``; as module-level functions that
indirection disappears.
"""

from ...mpvtk import pilfont
from ...mpvtk.scaling import px
from . import chrome
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
#: **Of the SLOT, not of the banner.** POSTER_H_FRAC is of the whole banner
#: and defines the margins it leaves; this is applied afterwards, inside
#: those, so measuring against the whole height counts them twice.
#:
#: Deriving the band from the horizontal margin instead is the paradox that
#: made a **wider window shrink the thumbnail** -- the inset grew with the
#: banner's width while its height stayed fixed. Pinned by
#: test_widening_the_header_never_shrinks_the_still and
#: test_a_thumbnail_never_takes_more_than_its_share.
#:
#: The two fractions differ because a poster is height-limited at any banner
#: shape and a still is width-limited, so a banner widened without being
#: heightened grows the still until it crowds the heading.
THUMB_H_FRAC = 0.60

#: Aspect at or above which inset artwork is a "thumbnail" rather than a
#: poster. jellyfin-web's own landscape threshold (see
#: TileRenderer.LANDSCAPE_RATIO); asked of the PICTURE rather than of the
#: item type, because that is what decides which limit binds -- and a
#: 4:3 still from an older show is as much a thumbnail as a 16:9 one.
THUMB_RATIO = 1.33

#: Widest shape anything inset into a header can be drawn at: a 16:9
#: still. Used to bound the slot's WIDTH by its height -- see poster_box.
MAX_INSET_ASPECT = 16 / 9


def poster_box(box):
    """``(x, y, max_w, max_h)`` the inset artwork is fitted inside.

    A **bounding** box, not the drawn size: what lands here is a 2:3 poster
    for a film or series and a 16:9 still for an episode, and both are
    drawn at their own shape within these limits. Sized off the banner so
    it scales with the header, and refused when there is no sensible room.
    """
    w, h = box
    max_h = int(h * POSTER_H_FRAC)
    # Capped against the slot's own HEIGHT as well as the banner's width.
    # POSTER_W_FRAC grows without limit while `h` is fixed (widening a
    # banner buys backdrop, not page), so on a very wide window the slot
    # was 1597px wide for a picture that can never be drawn wider than
    # ~570 -- and `_banner_poster` sizes its REQUEST from this, so a
    # detail page pulled a 1000x1500 poster whole (185 KB) to draw it at
    # 214px (19 KB). Nothing inset here is wider than a 16:9 still.
    max_w = min(int(w * POSTER_W_FRAC), int(max_h * MAX_INSET_ASPECT))
    if max_w <= 0 or max_h <= 0 or max_w > w // 3:
        return None
    return _content_x(w), (h - max_h) // 2, max_w, max_h


def _content_x(w):
    """The banner's left edge, in physical px.

    :data:`chrome.CONTENT_PAD`, not a fraction of the banner: a backdrop
    bleeds to the window edges, but everything baked ON it -- the inset
    poster, the heading, the meta line -- is page content, and the Play
    button lands directly under it. ``w // 40`` put the two in different
    columns (32 against 16 at a 1280 window), which is exactly the size of
    offset the eye picks up on a left edge it can compare against.

    ``w`` is taken and ignored so the call sites read as a measurement of
    the box rather than a constant that happens to be in scope.
    """
    return px(chrome.CONTENT_PAD)


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
    margin = _content_x(w)
    # The heading's inset from the BOTTOM of the banner is a different
    # measurement and keeps the old rule: it is breathing room over a
    # gradient, with nothing above or below it to line up with.
    bottom = max(px(18), w // 40)
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
    y = h - bottom
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
        # Of the SLOT, which `max_h` already is -- see THUMB_H_FRAC for why
        # measuring against the banner, or against the banner less a margin
        # taken from its WIDTH, is the bug that made a wider window shrink
        # the thumbnail.
        max_h = int(max_h * THUMB_H_FRAC)
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
