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
    locations. Returns the path.

    The dir is removed at interpreter exit as a backstop: stores that
    shut down cleanly delete their files themselves, but unit-test runs
    and crashed sessions used to strand thousands of these (a full
    XDG_RUNTIME_DIR tmpfs presents as ENOSPC everywhere)."""
    base = None
    if not sys.platform.startswith("win"):
        for cand in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm"):
            if cand and os.path.isdir(cand) and os.access(cand, os.W_OK):
                base = cand
                break
    path = tempfile.mkdtemp(prefix=prefix, dir=base)
    import atexit
    import shutil

    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


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


def bgra_bytes(pil_image):
    """Pillow image -> (premultiplied BGRA bytes, w, h)."""
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
    return bgra.tobytes(), img.width, img.height


def write_bgra(pil_image, path):
    """Write a Pillow image as premultiplied BGRA raw. Returns (w, h)."""
    data, w, h = bgra_bytes(pil_image)
    tmp = path + ".tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    _mark_temporary(tmp)
    os.replace(tmp, path)
    return w, h


class MemoryStore:
    """BGRA buffers for overlay-add's same-process ``&<address>`` form.

    Only valid when Python and mpv share a process (the libmpv
    backend): no files, no fs latency on mpv's command path — each
    re-issued crop during scrolling reads straight from this memory.

    Lifetime rules: a buffer must outlive every scene that references
    its src. Callers keep entries alive while referenced (an LRU whose
    recency tracks the current build satisfies this — anything visible
    was just requested); remove() parks the buffer in a small graveyard
    rather than freeing immediately, covering a renderer re-issue
    racing a scene push.
    """

    GRAVEYARD = 8

    def __init__(self):
        import collections

        self._bufs = {}  # src -> ctypes buffer
        self._graveyard = collections.deque(maxlen=self.GRAVEYARD)

    def add(self, pil_image):
        """Returns (src, w, h) with src usable as an Image/ImageMap
        source."""
        import ctypes

        data, w, h = bgra_bytes(pil_image)
        buf = ctypes.create_string_buffer(data, len(data))
        src = "&%d" % ctypes.addressof(buf)
        self._bufs[src] = buf
        return src, w, h

    def remove(self, src):
        buf = self._bufs.pop(src, None)
        if buf is not None:
            self._graveyard.append(buf)
