"""Rasterize images to the raw BGRA files mpv's overlay-add consumes.

overlay-add wants premultiplied-alpha BGRA. Files are written once per
image into a cache dir; mpv's repeated reads during scrolling come from
the page cache. To keep WRITES off the physical disk too (a browsing
session can composite hundreds of MB of strips):

- cache_dir() prefers RAM-backed locations (XDG_RUNTIME_DIR, /dev/shm)
  on POSIX, falling back to the system temp dir;
- on Windows, written files are marked FILE_ATTRIBUTE_TEMPORARY, which
  tells the cache lazy-writer to avoid flushing them to disk as long as
  memory allows — the classic pattern for short-lived scratch files;
- callers should bound their caches (see demo.StripStore's LRU) so the
  footprint stays small either way.

The endgame for the libmpv backend is overlay-add's ``&<address>``
same-process memory form (no files at all); the file path is what works
identically over jsonipc.

The renderer does not scale, so images must be rasterized at their
display size.
"""

import logging
import os
import sys
import tempfile

log = logging.getLogger("mpvtk")

_FILE_ATTRIBUTE_TEMPORARY = 0x100


def cache_dir(prefix="mpvtk-"):
    """Create a scratch dir for BGRA files, preferring RAM-backed
    locations. Returns the path."""
    base = None
    if not sys.platform.startswith("win"):
        for cand in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):
            if cand and os.path.isdir(cand) and os.access(cand, os.W_OK):
                base = cand
                break
    return tempfile.mkdtemp(prefix=prefix, dir=base)


def _mark_temporary(path):
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetFileAttributesW(
            path, _FILE_ATTRIBUTE_TEMPORARY
        )
    except Exception:  # never let a hint break the write
        log.debug("could not set FILE_ATTRIBUTE_TEMPORARY", exc_info=True)


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
    _mark_temporary(tmp)
    os.replace(tmp, path)
    return img.width, img.height
