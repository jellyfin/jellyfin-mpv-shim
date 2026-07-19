"""Rasterize images to the raw BGRA files mpv's overlay-add consumes.

overlay-add wants premultiplied-alpha BGRA. Files are written once per
image into a cache dir (plain tempfile — page cache makes mpv's repeated
reads during scrolling memory-speed on both Linux and Windows; no tmpfs
assumption). The renderer does not scale, so images must be rasterized
at their display size.
"""

import os


def write_bgra(pil_image, path):
    """Write a Pillow image as premultiplied BGRA raw. Returns (w, h)."""
    img = pil_image.convert("RGBA")
    r, g, b, a = img.split()
    # Premultiply only when there is actual transparency (cheap check).
    lo, hi = a.getextrema()
    if lo < 255:
        from PIL import ImageChops

        r = ImageChops.multiply(r, a)
        g = ImageChops.multiply(g, a)
        b = ImageChops.multiply(b, a)
    from PIL import Image

    bgra = Image.merge("RGBA", (b, g, r, a))
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(bgra.tobytes())
    os.replace(tmp, path)
    return img.width, img.height
