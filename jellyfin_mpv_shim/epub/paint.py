"""Draw a laid-out page with Pillow.

**Why Pillow and not libass.** The toolkit's `Text` node is real ASS drawn
by libass, and it is the right tool for chrome — but not for a book, for
four reasons that compound:

1. **Images composite above all script ASS** (mpvtk GUIDE §6). A book is
   text with pictures *in* it; every illustration would draw over the
   paragraph it sits beside, and the escape hatch (an occluder rect) works
   per node, not per line of a reflowing page.
2. **One face per scene.** The toolkit measures and renders one UI font.
   Bold, italic, bold-italic and monospace in one paragraph is the ordinary
   case in a book and is not something that model has room for.
3. **Line breaking would have to agree with libass exactly.** Layout here
   decides where lines break; libass decides where glyphs land. Any drift
   between the two shows up as a justified line that overflows its column,
   and the metrics work needed to prevent it (§6.3 — per-char advances,
   kerning pairs, the ascender-vs-em correction) is the toolkit's, tuned
   for the UI font.
4. **A page is one bitmap either way.** Rasterizing the page costs one
   image; the alternative is several hundred text nodes per scene, pushed
   as JSON, on every page turn.

So the reader renders its page as a single premultiplied bitmap and hands
it to the scene as one `Image` — the same path the tile strips use, with
the same cache and the same LRU. The cost is that text on the page cannot
be selected or hit-tested, which for a reader is not a cost.
"""

import io
import logging

log = logging.getLogger("epub.paint")

#: Where an underline sits below the baseline, and how thick it is, as
#: fractions of the font size. Pillow gives us no underline position from
#: the face, and these are the values that look right across the serif
#: families in ``fonts.py``.
UNDERLINE_OFFSET = 0.12
UNDERLINE_WEIGHT = 0.055
STRIKE_HEIGHT = 0.28


class Palette:
    """Page colours. Three named sets, because a reader is looked at for
    hours and the right one is a matter of the room, not of the app."""

    def __init__(self, bg, fg, muted, rule):
        self.bg = bg
        self.fg = fg
        self.muted = muted
        self.rule = rule


PALETTES = {
    # Not pure black on white: a full-brightness page in a dark room is what
    # every reader added a sepia mode to get away from.
    "light": Palette((250, 248, 244), (26, 24, 22), (120, 116, 110),
                     (196, 190, 182)),
    "sepia": Palette((244, 232, 210), (60, 48, 34), (130, 112, 88),
                     (198, 178, 148)),
    "dark": Palette((22, 22, 24), (216, 213, 208), (140, 137, 132),
                    (70, 68, 66)),
}


def palette(name):
    return PALETTES.get(name) or PALETTES["dark"]


def render_page(page, size, style, measurer, colors, load_image=None,
                origin=None):
    """Draw one :class:`~.layout.Page` into a new RGB image of ``size``.

    ``origin`` is where the text column's top-left sits in the image; it
    defaults to the style's margins. ``load_image(src)`` returns a PIL
    image for an illustration, or None — a picture that will not decode
    leaves its space blank rather than failing the page, because one
    corrupt JPEG in a 400-page book should cost that page's picture and
    nothing else.
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", size, colors.bg)
    draw = ImageDraw.Draw(image)
    ox, oy = origin if origin is not None else (style.margin_x,
                                                style.margin_y)
    for item in page.items:
        kind = type(item).__name__
        if kind == "Line":
            _draw_line(draw, item, ox, oy, measurer, colors)
        elif kind == "ImageItem":
            _draw_image(image, item, ox, oy, load_image, colors, draw,
                        measurer)
        elif kind == "RuleItem":
            y = oy + item.y + 2
            draw.rectangle([ox + item.x, y, ox + item.x + item.w, y + 1],
                           fill=colors.rule)
    return image


def _draw_line(draw, line, ox, oy, measurer, colors):
    base = oy + line.y + line.ascent
    for piece in line.pieces:
        font = measurer.font(piece.style)
        x = ox + piece.x
        # "ls" is left/baseline: pieces of different sizes on one line share
        # the line's baseline, which is what keeps a run set larger or
        # smaller sitting on the same line rather than floating. ``dy`` is
        # the deliberate exception — a superscript, or a drop capital
        # reaching down past the line it is drawn on — and it is a number
        # layout worked out, not a decision taken here.
        baseline = base + piece.dy
        draw.text((x, baseline), piece.text, font=font, fill=colors.fg,
                  anchor="ls")
        if piece.style.underline or piece.style.strike:
            width = measurer.width(piece.text, piece.style)
            size = measurer.size_for(piece.style)
            weight = max(1, int(round(size * UNDERLINE_WEIGHT)))
            if piece.style.underline:
                y = baseline + max(1, int(round(size * UNDERLINE_OFFSET)))
                draw.rectangle([x, y, x + width, y + weight - 1],
                               fill=colors.fg)
            if piece.style.strike:
                y = baseline - int(round(size * STRIKE_HEIGHT))
                draw.rectangle([x, y, x + width, y + weight - 1],
                               fill=colors.fg)


def _draw_image(canvas, item, ox, oy, load_image, colors, draw, measurer):
    picture = load_image(item.src) if load_image else None
    if picture is None:
        _draw_missing(draw, item, ox, oy, colors, measurer)
        return
    from PIL import Image

    try:
        if picture.size != (item.w, item.h):
            picture = picture.resize((max(1, item.w), max(1, item.h)),
                                     Image.LANCZOS)
        box = (ox + item.x, oy + item.y)
        if picture.mode in ("RGBA", "LA", "P"):
            # Composite rather than paste: an illustration with a
            # transparent background is drawn for a white page, and pasting
            # it onto a dark one without compositing leaves the black
            # Pillow put behind it. Same trap as the channel logos in
            # ``imageutil``.
            picture = picture.convert("RGBA")
            plate = Image.new("RGB", picture.size, colors.bg)
            plate.paste(picture, (0, 0), picture)
            picture = plate
        else:
            picture = picture.convert("RGB")
        canvas.paste(picture, box)
    except Exception:
        log.debug("could not draw %s", item.src, exc_info=True)
        _draw_missing(draw, item, ox, oy, colors, measurer)


def _draw_missing(draw, item, ox, oy, colors, measurer):
    """A frame where a picture should have been, with its alt text.

    Deliberately visible. A silently missing illustration in a technical
    book removes the thing the surrounding paragraph is about, and the
    reader has no way to know it happened.
    """
    x0, y0 = ox + item.x, oy + item.y
    draw.rectangle([x0, y0, x0 + item.w, y0 + item.h], outline=colors.rule)
    label = item.alt or "[image]"
    from .content import Style

    style = Style(scale=0.85)
    font = measurer.font(style)
    width = measurer.width(label, style)
    if width < item.w - 8:
        draw.text((x0 + (item.w - width) / 2, y0 + item.h / 2), label,
                  font=font, fill=colors.muted, anchor="lm")


def decode_image(data, max_pixels=40_000_000):
    """Bytes -> a PIL image, or None. Never raises.

    ``max_pixels`` is a second bomb guard behind Pillow's own: the archive
    layer caps the *bytes* an entry may deliver, and this caps what those
    bytes are allowed to expand into.
    """
    from PIL import Image

    try:
        picture = Image.open(io.BytesIO(data))
        picture.load()
    except Exception:
        log.debug("undecodable image", exc_info=True)
        return None
    if picture.size[0] * picture.size[1] > max_pixels:
        log.info("image %s is too large to draw", picture.size)
        return None
    return picture


def image_size(data):
    """``(w, h)`` without decoding the pixels. None if unreadable."""
    from PIL import Image

    try:
        with Image.open(io.BytesIO(data)) as picture:
            return picture.size
    except Exception:
        log.debug("unreadable image header", exc_info=True)
        return None
