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


def pil_font(size, bold=False, text=None):
    """Font for a baked text block. ``text`` picks a face that covers the
    string's script — Pillow has no fallback, so a CJK title drawn with the
    Latin face is tofu (see mpvtk.pilfont)."""
    from .mpvtk import pilfont

    if text is None:
        return pilfont.font("latin", size, bold)
    return pilfont.font_for(text, size, bold)
