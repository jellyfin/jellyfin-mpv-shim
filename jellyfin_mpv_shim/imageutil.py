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

import math

from typing import NamedTuple

from PIL import Image


#: Where a banner's crop is centred in its source, as a fraction of the
#: source's height. The middle of the TOP TWO THIRDS -- which is where the
#: subject of a piece of key art is, because that is where heads are [iw].
#:
#: Only the vertical axis is biased. Horizontally the interesting half of a
#: backdrop could be either side and centre is the only defensible guess;
#: vertically it is the same answer for essentially every photograph, and a
#: banner is a wide slot cut out of a 16:9 picture, so the centre crop was
#: routinely taking the band from the chin down.
TOP_HEAVY = 1 / 3


def scale_to_cover(image: "Image.Image", w: int, h: int,
                   gravity_y: float = 0.5) -> "Image.Image":
    """Scale `image` to fully cover (w, h), cropping the overflow.

    ``gravity_y`` is the point of the SCALED IMAGE that the crop tries to
    put at its own centre, as a fraction of that image's height -- 0.5 is
    the plain centre crop, :data:`TOP_HEAVY` is the middle of the top two
    thirds. Stated as a point in the source rather than as an alignment of
    the crop box (Pillow's ``centering``) because that keeps its meaning
    when the box is too tall for the bias to be honoured: the result is
    clamped to the picture, so a crop that cannot sit where it was asked to
    goes as far that way as it can instead of hanging off the edge.
    """
    iw, ih = image.size
    scale = max(w / iw, h / ih)
    # Rounded UP and floored at the box. `int()` truncates, and the scale is
    # exactly `w / iw` on the binding axis, so the product lands a hair under
    # the target for about one width in eighty: 1920x1080 into a 999px box
    # gave new_w 998, `left` -1, and a crop reaching outside the picture --
    # which Pillow pads with transparent black rather than refusing, so it
    # came out as a 1px hairline down each side. A full-bleed banner's width
    # tracks the window pixel for pixel, so it flickered in and out of
    # existence during a drag-resize.
    new_w = max(w, math.ceil(iw * scale))
    new_h = max(h, math.ceil(ih * scale))
    image = image.resize((new_w, new_h), Image.LANCZOS)
    left = max(0, min((new_w - w) // 2, new_w - w))
    top = int(round(new_h * gravity_y - h / 2))
    top = max(0, min(top, new_h - h))
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


class AlphaStats(NamedTuple):
    """What :func:`measure_transparency` records about a transparent image.

    ``clear`` is the share of pixels that are at least half transparent.
    ``luma`` is the 0-255 luma averaged over the pixels that are not — a
    summary, nothing decides on it. ``hist`` is the 256-bin luma histogram
    that mean was taken over. ``edge`` is the same histogram over the ink that
    *meets* the transparency — the outermost ring of the artwork — and it is
    what :func:`plate_for` asks about.
    """

    clear: float
    luma: float
    hist: tuple
    edge: tuple


def measure_transparency(image: "Image.Image"):
    """Measure ``image``'s transparency and the luma of its *visible* pixels.

    Returns (and records under :data:`ALPHA_INFO`) an :class:`AlphaStats`. The
    mask matters — Pillow leaves black under a PNG's transparent pixels, so an
    unmasked measurement says "dark" about every logo on a transparent
    background, which is the exact distinction this is here to make.

    The whole histogram is kept, not just its mean, because a logo is routinely
    *bimodal*: the NBC peacock is a bright mark next to a black wordmark, and
    its mean luma (71) describes no pixel in the image. Deciding anything from
    that average is deciding about ink that is not there.

    ``edge`` is the one *spatial* thing measured here, and the question it
    answers is "which ink touches the background": erode the opaque mask by a
    pixel and the ring that falls away is the artwork's outline. A white
    wordmark with a dark keyline round it and a bare white wordmark have the
    same luma histogram and want opposite treatment on a white plate; they
    differ in the ring.

    ``None`` for an image without an alpha channel; there is nothing to decide.
    """
    if image.mode != "RGBA":
        return None
    from PIL import ImageChops, ImageFilter

    alpha = image.getchannel("A")
    total = image.width * image.height
    if not total:
        return None
    clear = sum(alpha.histogram()[:128]) / total
    # Threshold rather than use alpha directly as the mask: a weighted mean
    # would pull the anti-aliased fringe of dark-on-transparent text towards
    # the black underneath it.
    opaque = alpha.point(lambda v: 255 if v >= 128 else 0)
    gray = image.convert("L")
    hist = gray.histogram(mask=opaque)
    ink = sum(hist)
    luma = sum(v * n for v, n in enumerate(hist)) / ink if ink else 0.0
    # MinFilter is erosion on a 0/255 mask; the difference is the outer ring.
    # Ink thinner than the filter erodes away entirely and is *all* ring,
    # which is the right answer — a hairline stroke is nothing but edge.
    band = ImageChops.subtract(opaque, opaque.filter(ImageFilter.MinFilter(3)))
    edge = gray.histogram(mask=band)
    stats = AlphaStats(clear, luma, tuple(hist),
                       tuple(edge) if sum(edge) else tuple(hist))
    image.info[ALPHA_INFO] = stats
    return stats


def _luma(rgb) -> float:
    return 0.299 * rgb[0] + 0.587 * rgb[1] + 0.114 * rgb[2]


#: sRGB -> linear, per 0-255 level. Built once; :func:`_lost_fraction` reads it
#: up to 256 times per decision and the exponent is not cheap.
_LINEAR = tuple(
    (i / 255.0) / 12.92 if i / 255.0 <= 0.04045
    else ((i / 255.0 + 0.055) / 1.055) ** 2.4
    for i in range(256)
)


def _lost_fraction(hist, surface, min_ratio: float = 3.0) -> float:
    """Share of the visible ink that ``surface`` swallows, 0-1: each pixel
    counts by how far its contrast against ``surface`` falls short of
    ``min_ratio``, WCAG's floor for graphics.

    Two things this is not, both of which were tried and both of which put the
    decision on a knife edge — the same edge the mean did, one level down:

    * Not a count of the ink within some distance. Logo ink comes in tight
      clusters and a hard edge through one flips the whole cluster at once: the
      PBS logo keeps 68% of its ink inside two luma steps, so a counting rule
      scored a 6-step change of plate as having rescued the logo. Hence a ramp.
    * Not a distance in luma. That axis is not perceptually uniform at the dark
      end, which is the end this UI lives at: black ink is 23 steps from
      ``WINDOW_BG`` and completely invisible on it, so a linear ramp wide
      enough to be useful scores "invisible" as about half lost. As a contrast
      ratio it is 1.2:1, which is the answer. Hence WCAG.

    Luma is the only axis, and it is an approximation: it under-rates
    saturated ink, which reads better against a surface than its luma suggests.
    Judged acceptable, because the surface asked about is now always a light
    plate and the ink that decides is the boundary ring rather than the whole
    logo — the case that axis got wrong was a saturated mark against near-black.
    """
    ink = sum(hist)
    if not ink:
        return 0.0
    sl = _LINEAR[int(_luma(surface))] + 0.05
    span = min_ratio - 1.0
    lost = 0.0
    for v, n in enumerate(hist):
        if not n:
            continue
        il = _LINEAR[v] + 0.05
        ratio = il / sl if il > sl else sl / il
        lost += n * min(1.0, max(0.0, (min_ratio - ratio) / span))
    return lost / ink


class Plate(NamedTuple):
    """What goes behind a transparent image: a plate ``color``, and whether
    the artwork needs a ``shadow`` to hold its shape against it."""

    color: tuple
    shadow: bool


def plate_for(image: "Image.Image", light=(240, 240, 240), min_ratio=3.0,
              min_clear=0.10, max_edge=0.35):
    """The plate for a transparent ``image``, or ``None`` to leave it alone.

    Broadcasters ship channel logos as artwork on a transparent background,
    drawn for the white page every other client puts them on. Let through onto
    this UI's near-black surfaces the black ones are invisible, which is no
    better than the solid black block they used to flatten to. So a genuinely
    transparent logo gets a light plate — **all** of them, so a row of channels
    is a row of one kind of chip rather than a per-logo judgement call about
    contrast that lands differently on every tile.

    The one thing a white plate cannot carry is white ink lying *directly* on
    the transparency. That is not "the logo is bright" — a white wordmark
    almost always comes with a keyline or a coloured mark around it, and those
    read on white perfectly well. It is specifically whether the ink at the
    artwork's boundary is itself white, which is what :attr:`AlphaStats.edge`
    measures and ``max_edge`` bounds. Such a logo still gets the same white
    plate, for consistency, plus a :func:`with_shadow` halo to give its
    outermost ink an edge to sit against.

    Deliberately narrow at the top: an image with no alpha channel, or one
    whose transparency is only an anti-aliased fringe (``min_clear``), is
    opaque artwork carrying its own background and is left alone.
    """
    if image.mode != "RGBA":
        return None
    m = image.info.get(ALPHA_INFO) or measure_transparency(image)
    if m is None:
        return None
    if m.clear < min_clear:
        return None
    return Plate(light, _lost_fraction(m.edge, light, min_ratio) > max_edge)


def with_shadow(image: "Image.Image", color=(0, 0, 0), opacity: float = 0.8):
    """``image`` over a soft drop shadow of its own silhouette.

    For the white-on-white case: the shadow is the artwork's alpha, blurred and
    nudged down, so white ink on a white plate keeps a visible boundary. Blur
    and offset scale with the image because this runs on whatever size the
    thumbnail pipeline produced, from a 275px tile down to a 24px art cell.

    The shadow is drawn inside the image's own bounds, so ink that runs to the
    very edge of its bitmap has that side of its shadow clipped. Logos have
    margins; the alternative is growing the bitmap, which would move the art.
    """
    if image.mode != "RGBA":
        return image
    from PIL import ImageFilter

    span = max(image.size)
    blur = max(1.0, span / 60)
    drop = max(1, round(span / 90))
    silhouette = Image.new("L", image.size, 0)
    silhouette.paste(image.getchannel("A"), (0, drop))
    silhouette = silhouette.filter(ImageFilter.GaussianBlur(blur))
    silhouette = silhouette.point(lambda v: int(v * opacity))
    layer = Image.new("RGBA", image.size, tuple(color) + (0,))
    layer.putalpha(silhouette)
    return Image.alpha_composite(layer, image)


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
