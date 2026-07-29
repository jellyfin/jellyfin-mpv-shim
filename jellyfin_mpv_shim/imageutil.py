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

from typing import NamedTuple

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


class AlphaStats(NamedTuple):
    """What :func:`measure_transparency` records about a transparent image.

    ``clear`` is the share of pixels that are at least half transparent.
    ``luma`` is the 0-255 luma averaged over the pixels that are not — a
    summary, nothing decides on it. ``hist`` is the 256-bin luma histogram
    that mean was taken over, and it is what :func:`plate_color` asks about.
    """

    clear: float
    luma: float
    hist: tuple


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
    hist = image.convert("L").histogram(mask=opaque)
    ink = sum(hist)
    luma = sum(v * n for v, n in enumerate(hist)) / ink if ink else 0.0
    stats = AlphaStats(clear, luma, tuple(hist))
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
    saturated ink, which reads better against a dark surface than its luma
    suggests. Two axes were tried and are not worth their complexity — the
    thing that would decide the remaining cases is *spatial* (whether the PBS
    logo's white glyph sits inside its blue disc), and no histogram sees that.
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


def plate_color(image: "Image.Image", behind, light=(240, 240, 240),
                dark=(16, 16, 18), min_ratio=3.0, min_clear=0.10,
                max_lost=0.25):
    """Colour to flatten a transparent ``image`` onto, or ``None`` to let its
    transparency through to ``behind``.

    Broadcasters ship channel logos as artwork on a transparent background,
    and a good few of them are *black* artwork — drawn for a white page. Let
    through onto this UI's near-black surfaces they are invisible, which is no
    better than the solid black block they used to flatten to. So when a
    genuinely transparent image has too much of its ink swallowed by what is
    behind it, it gets a contrasting neutral plate of its own instead.

    One question, asked twice: :func:`_lost_fraction` against ``behind`` says
    whether the artwork needs rescuing, and against each neutral says whether
    that one would rescue it. ``max_lost`` is the share of ink allowed to
    disappear, and art no flat plate can get under that — half light ink, half
    dark — is left alone rather than given a white box for nothing.

    "Too much" is measured over the ink's distribution, never its mean: the
    mean is the average of a distribution the artwork need not have anywhere.
    A bright mark beside a black wordmark averages to mid-grey, which reads as
    "contrasts fine against a dark surface" while half the logo is invisible.

    Deliberately narrow, because plating art that does not need it is its own
    kind of wrong: an image with no alpha channel, one whose transparency is
    just an anti-aliased edge (``min_clear``), or one that already reads
    against ``behind`` is all left alone.
    """
    if image.mode != "RGBA":
        return None
    m = image.info.get(ALPHA_INFO) or measure_transparency(image)
    if m is None:
        return None
    clear, _mean, hist = m
    if clear < min_clear:
        return None
    if _lost_fraction(hist, behind, min_ratio) <= max_lost:
        return None
    best = min((light, dark), key=lambda c: _lost_fraction(hist, c, min_ratio))
    if _lost_fraction(hist, best, min_ratio) > max_lost:
        return None
    return best


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
