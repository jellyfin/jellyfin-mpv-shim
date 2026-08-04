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
import shutil
import sys
import tempfile
import time

log = logging.getLogger("mpvtk")

_FILE_ATTRIBUTE_TEMPORARY = 0x100


#: Free space a RAM-backed base has to have left before we will put a cache
#: there: what the caches that go here are actually allowed to hold (64 MiB
#: of thumbnails, 128 MiB of strips on the jsonipc path) plus room to spare.
#:
#: XDG_RUNTIME_DIR is a tmpfs sized as a fraction of RAM -- 13G on a 128G
#: desktop, but 100M on a 2G box -- and it is shared with everything else the
#: session keeps there (pipewire, gvfs, dbus, systemd units). Filling it does
#: not merely evict our own pictures: it presents as ENOSPC to every other
#: program using it, which is a much worse failure than a slower cache. So a
#: base that cannot spare this much loses to the next one, ending at
#: ~/.cache on real disk.
MIN_FREE_BYTES = 256 * 1024 * 1024

#: An unswept cache dir with no pid in its name (an older build's, or one
#: whose owner cannot be checked) is only reclaimed once it is this stale.
STALE_SECS = 24 * 60 * 60


def _process_alive(pid):
    """Whether ``pid`` is still running, erring towards "yes".

    POSIX only. On Windows ``os.kill(pid, 0)`` is not a liveness probe --
    it calls TerminateProcess for any signal that is not a CTRL_ event --
    so there the answer is always yes and dirs are reclaimed by age alone.
    """
    if os.name == "nt":
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        return True     # EPERM: someone else's, so not ours to remove
    return True


def _owner_pid(name, prefix):
    """The pid encoded in a cache dir name, or None if it carries none."""
    rest = name[len(prefix):]
    head = rest.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def sweep_stale(base, prefix, now=None):
    """Remove ``prefix`` cache dirs under ``base`` left by dead processes.

    Returns the number removed. This is the cleanup that actually happens:
    the atexit hook below covers a clean shutdown, and a session that is
    SIGKILLed, crashes, or is killed by the window manager -- which is most
    of how a media player ends -- strands its whole cache. Several hundred
    megabytes each, in a tmpfs, per run.

    Sweeping on the way *in* rather than on the way out is what makes it
    reliable: the process that has to do the work is the one that is
    definitely running. A dir is ours to take when its pid is gone; one with
    no pid in the name is an older build's, and goes by age instead.
    """
    now = now or time.time()
    removed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        if not name.startswith(prefix):
            continue
        path = os.path.join(base, name)
        if not os.path.isdir(path):
            continue
        pid = _owner_pid(name, prefix)
        if pid is not None:
            if pid == os.getpid() or _process_alive(pid):
                continue
        else:
            try:
                if now - os.stat(path).st_mtime < STALE_SECS:
                    continue
            except OSError:
                continue
        shutil.rmtree(path, ignore_errors=True)
        removed += 1
    if removed:
        log.info("Reclaimed %d abandoned cache director%s under %s",
                 removed, "y" if removed == 1 else "ies", base)
    return removed


def _bases():
    """Where a scratch dir may go, best first.

    RAM first (mpv re-reads these files constantly during a scroll and the
    writes never have to reach a disk), then the user's real cache dir.

    Only real candidates: an unset variable is dropped here rather than
    ending the search, or a Linux login without XDG_RUNTIME_DIR would never
    get as far as /dev/shm. Running out of candidates leaves the base unset
    and tempfile picks, which is the answer on the platforms below.

    **XDG only.** Windows and macOS get no candidates at all, not even a
    home-directory one: they have no tmpfs to prefer, and ~/.cache is not a
    place either of them keeps anything (macOS would want ~/Library/Caches,
    which is where the *persistent* artwork cache goes -- see
    conffile.cachedir). Their scratch space is the system temp directory the
    OS already cleans up after, which is where they landed before this
    function existed and where they should keep landing. It matters more on
    macOS than it looks: mpv_ext is forced there, so strips are files rather
    than in-process buffers, and this is the directory they go in.
    """
    if sys.platform.startswith(("win", "darwin")):
        return []
    cache = os.environ.get("XDG_CACHE_HOME") or os.path.expanduser("~/.cache")
    return [c for c in (os.environ.get("XDG_RUNTIME_DIR"), "/dev/shm", cache)
            if c]


def cache_dir(prefix="mpvtk-", min_free=MIN_FREE_BYTES):
    """Create a scratch dir for BGRA files, preferring RAM-backed
    locations with room to spare. Returns the path.

    Two things decide the base, in this order: what is left behind, and
    what is left free. Every candidate is swept of dead sessions' dirs
    first -- so the space this app leaked on its last run counts as free
    -- and only then measured. A tmpfs without ``min_free`` to spare is
    passed over for ~/.cache, because the cost of being wrong there is
    borne by the whole session and not just by us.

    The dir is still removed at interpreter exit, which remains the
    cheapest cleanup when the app gets to exit cleanly.
    """
    base = None
    for cand in _bases():
        if not (os.path.isdir(cand) and os.access(cand, os.W_OK)):
            continue
        sweep_stale(cand, prefix)
        try:
            free = shutil.disk_usage(cand).free
        except OSError:
            continue
        if free < min_free:
            log.info("Not caching in %s: %d MiB free, %d MiB wanted",
                     cand, free // (1024 * 1024), min_free // (1024 * 1024))
            continue
        base = cand
        break
    # The pid is what makes the sweep above possible: it is the only thing
    # in the name that says whether the session that owns this dir is still
    # running. mkdtemp's own suffix keeps two runs of the same pid apart.
    path = tempfile.mkdtemp(prefix="%s%d-" % (prefix, os.getpid()), dir=base)
    import atexit

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
