"""Pillow helpers shared by the display mirror and the browser's banners.

These used to live in ``display_mirror`` as privates, and the mpvtk browser
reached in for them — a hard dependency from a core view onto an *optional*,
Pillow-gated feature module that is itself slated for cleanup. They belong to
neither, so they live here.

**Importing this module requires Pillow.** Import it lazily, inside the
function that composites, the way ``display_mirror`` and
``TilesMixin._compose_banner`` do — everything past the four required
dependencies has to degrade gracefully when its package is missing (see
CONTRIBUTING.md).
"""

from PIL import Image


def scale_to_cover(image: "Image.Image", w: int, h: int) -> "Image.Image":
    """Scale `image` to fully cover (w, h), center-cropping the overflow."""
    iw, ih = image.size
    scale = max(w / iw, h / ih)
    new_w, new_h = max(1, int(iw * scale)), max(1, int(ih * scale))
    image = image.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - w) // 2
    top = (new_h - h) // 2
    return image.crop((left, top, left + w, top + h))


def apply_dark_gradient(
    image: "Image.Image", height_fraction: float = 0.55, max_alpha: int = 200
) -> "Image.Image":
    """Composite a vertical transparent->dark gradient over the image's bottom."""
    w, h = image.size
    grad_h = max(1, int(h * height_fraction))
    overlay = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    # Build the gradient as a single column then resize horizontally — much
    # faster than per-row paste for large images.
    column = Image.new("RGBA", (1, grad_h))
    for y in range(grad_h):
        alpha = int(max_alpha * (y / max(1, grad_h - 1)) ** 1.5)
        column.putpixel((0, y), (0, 0, 0, alpha))
    column = column.resize((w, grad_h), Image.NEAREST)
    overlay.paste(column, (0, h - grad_h))
    return Image.alpha_composite(image, overlay)


#: Key under which :func:`measure_transparency` parks its measurement in a
#: decoded image's ``info`` dict. Measuring happens once, on the thumbnail
#: worker thread; the compositors that need it run on the loop thread and on
#: the strip pool, and neither should be scanning pixels to find out whether a
#: logo has a transparent background.
ALPHA_INFO = "mpvshim_alpha"


def measure_transparency(image: "Image.Image"):
    """Measure ``image``'s transparency and how bright its *visible* pixels are.

    Returns (and records under :data:`ALPHA_INFO`) ``(clear_fraction,
    mean_luma)``: the share of pixels that are at least half transparent, and
    the 0-255 luma averaged over the pixels that are not. The mask matters —
    Pillow leaves black under a PNG's transparent pixels, so an unmasked mean
    says "dark" about every logo on a transparent background, which is the
    exact distinction this is here to make.

    ``None`` for an image without an alpha channel; there is nothing to decide.
    """
    if image.mode != "RGBA":
        return None
    alpha = image.getchannel("A")
    total = image.width * image.height
    if not total:
        return None
    clear = sum(alpha.histogram()[:128]) / total
    # Threshold rather than use alpha directly as the mask: a weighted mean
    # would pull the anti-aliased fringe of dark-on-transparent text towards
    # the black underneath it.
    opaque = alpha.point(lambda v: 255 if v >= 128 else 0)
    from PIL import ImageStat

    stat = ImageStat.Stat(image.convert("L"), mask=opaque)
    luma = stat.mean[0] if stat.count[0] else 0.0
    image.info[ALPHA_INFO] = (clear, luma)
    return clear, luma


def _luma(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


def plate_color(image: "Image.Image", behind, light=(240, 240, 240),
                dark=(16, 16, 18), min_contrast=48, min_clear=0.10):
    """Colour to flatten a transparent ``image`` onto, or ``None`` to let its
    transparency through to ``behind``.

    Broadcasters ship channel logos as artwork on a transparent background,
    and a good few of them are *black* artwork — drawn for a white page. Let
    through onto this UI's near-black surfaces they are invisible, which is no
    better than the solid black block they used to flatten to. So when a
    genuinely transparent image has too little contrast against what is behind
    it, it gets a contrasting neutral plate of its own instead.

    Deliberately narrow, because plating art that does not need it is its own
    kind of wrong: an image with no alpha channel, one whose transparency is
    just an anti-aliased edge (``min_clear``), or one that already reads
    against ``behind`` (``min_contrast``) is all left alone.
    """
    if image.mode != "RGBA":
        return None
    m = image.info.get(ALPHA_INFO) or measure_transparency(image)
    if m is None:
        return None
    clear, luma = m
    if clear < min_clear:
        return None
    if abs(luma - _luma(behind)) >= min_contrast:
        return None
    return light if luma < 128 else dark


def flatten_onto(image: "Image.Image", color, radius: int = 0) -> "Image.Image":
    """``image`` composited over an opaque ``color`` plate of the same size.

    ``radius`` rounds the plate's corners (the pixels outside stay
    transparent), so a light plate in a dark list reads as a chip rather than
    a hard white square.
    """
    back = Image.new("RGBA", image.size, tuple(color) + (255,))
    if radius > 0:
        from PIL import ImageDraw

        mask = Image.new("L", image.size, 0)
        ImageDraw.Draw(mask).rounded_rectangle(
            [0, 0, image.width - 1, image.height - 1], radius=radius, fill=255)
        back.putalpha(mask)
    return Image.alpha_composite(back, image)


def pil_font(size, bold=False, text=None):
    """Font for a baked text block. ``text`` picks a face that covers the
    string's script — Pillow has no fallback, so a CJK title drawn with the
    Latin face is tofu (see mpvtk.pilfont)."""
    from .mpvtk import pilfont

    if text is None:
        return pilfont.font("latin", size, bold)
    return pilfont.font_for(text, size, bold)
