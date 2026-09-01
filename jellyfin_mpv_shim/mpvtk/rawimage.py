"""Rasterize images to the raw BGRA files mpv's overlay-add consumes.

overlay-add wants premultiplied-alpha BGRA. `cache_dir` keeps the writes
off physical disk (RAM-backed bases on POSIX, FILE_ATTRIBUTE_TEMPORARY on
Windows) and callers bound their own caches; `MemoryStore` is the libmpv
backend's file-free ``&<address>`` form. See `mpvtk/GUIDE.md` section 5.

**The renderer does not scale, so images must be rasterized at their
display size.** Most of the length below is not about BGRA at all -- it is
the scratch-directory sweep, which deletes directories out of shared,
world-writable locations. Read those docstrings before touching them.
"""

import logging
import os
import shutil
import stat
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
    so there the answer is always yes -- which is why the app namespaces its
    scratch caches instead (see set_instance_namespace), and why nothing was
    ever reclaimed on Windows before it did.
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


#: Bigger than any pid any OS hands out, and small enough for os.kill's C
#: long. A name claiming more than this is not a pid, it is garbage or bait.
_MAX_PID = 2 ** 31 - 1

#: Directories this process created, which nothing may sweep.
_ours = set()

#: Directory name this process's scratch caches live under, inside whichever
#: base is chosen. None outside the app (tests, the demo, an embedder), which
#: keeps the flat layout and the pid-liveness rules below.
_namespace = None


def set_instance_namespace(name):
    """Put this process's scratch caches in their own directory, and claim
    everything already in it.

    ``name`` identifies the CONFIGURATION this process is running, and the
    app calls this once it holds that configuration's single-instance lock.
    Those two facts together are what make the claim sound: at most one live
    process per configuration, so anything in the namespace that is not ours
    was left by a copy that is gone.

    Namespacing rather than tagging names, because the isolation should be
    structural. The lock lives on a file inside the config directory while
    scratch space is machine-wide -- two copies started with different
    ``--config`` directories are perfectly legal and share a %TEMP% -- so
    "I hold a lock, therefore every cache directory here is dead" is false
    in general. Give each configuration its own directory and it is true
    within it, with no liveness probe at all. That matters most on Windows,
    where ``os.kill(pid, 0)`` terminates rather than probes and nothing
    could ever be reclaimed.

    It is also simply the right layout: scratch directories used to sit
    loose at the top of ~/.cache under names that did not say whose they
    were.
    """
    global _namespace
    _namespace = name or None


def _owner_pid(name, prefix):
    """The pid encoded in a cache dir name, or None if it carries none.

    ``isdecimal`` rather than ``isdigit``: the latter is true for "²" and
    every other Unicode digit form, and ``int()`` then raises. These names
    come off a shared, world-writable directory (/dev/shm, /tmp), so a
    malformed one is not only a typo -- and an exception here is thrown
    from the browser's startup path.
    """
    head = name[len(prefix):].split("-", 1)[0]
    if not head.isdecimal():
        return None
    pid = int(head)
    return pid if 0 < pid <= _MAX_PID else None


def _created_pid(name):
    """The pid out of a name this module made, whatever prefix it carries.

    ``<prefix><pid>-<random>``, and the prefix has dashes of its own
    ("mpvtk-thumbs-"), so the pid is the second-to-last dash-separated part
    -- mkdtemp's suffix has no dashes. Inside a namespace this is the only
    way to read it, because the prefixes in there are other stores'.
    """
    parts = name.rsplit("-", 2)
    if len(parts) == 3 and parts[1].isdecimal():
        pid = int(parts[1])
        return pid if 0 < pid <= _MAX_PID else None
    return None


def _reclaimable(base, name, prefix, now):
    """Whether ``base/name`` is an abandoned cache directory. Never raises
    for a caller that has already checked it is a directory."""
    if os.path.join(base, name) in _ours:
        # Made by this process, this run. Belt and braces on top of the pid
        # rules below: a directory we are actively writing to must never be
        # reclaimable by any argument.
        return False
    if _namespace:
        # Inside our own namespace, ownership is not a question -- the only
        # live process that may write here is this one -- so the pid is only
        # asked to tell our own directories from a dead run's.
        return _created_pid(name) != os.getpid()
    if not name.startswith(prefix):
        return False
    pid = _owner_pid(name, prefix)
    if pid is not None:
        return pid != os.getpid() and not _process_alive(pid)
    if "-" in name[len(prefix):]:
        # Not ours to age out. A name with further dashes after the prefix
        # is another prefix's directory seen through a shorter one
        # ("mpvtk-" sees "mpvtk-browser-1234-ab"), and its pid is not where
        # we looked -- so treating it as pid-less and reclaiming it by age
        # would delete a running session's cache. mkdtemp's own suffix has
        # no dashes, so this cannot exclude a real pre-namespace leftover.
        return False
    try:
        return now - os.stat(os.path.join(base, name)).st_mtime >= STALE_SECS
    except OSError:
        return False


def sweep_stale(base, prefix, now=None):
    """Remove abandoned cache dirs under ``base``. Returns the number gone.

    This is the cleanup that actually happens: the atexit hook below covers
    a clean shutdown, and a session that is SIGKILLed, crashes, or is killed
    by the window manager -- which is most of how a media player ends --
    strands its whole cache. Several hundred megabytes each, per run.

    Sweeping on the way *in* rather than on the way out is what makes it
    reliable: the process that has to do the work is the one that is
    definitely running.

    Inside a namespace (see set_instance_namespace) ``prefix`` is ignored
    and every directory but this process's own is taken, which is both
    stronger and simpler -- it needs no liveness probe, so it works on
    Windows, and it reclaims every prefix at once rather than only the one
    a given store happens to ask for.

    Outside one it is required, and required to be non-empty, because it is
    then the only thing that says a directory has anything to do with us:
    ``base`` is a shared location (/tmp, /dev/shm, ~/.cache) full of other
    programs' state. Everything ``startswith("")``, so an empty prefix does
    not sweep our directories a bit more eagerly -- it puts every neighbour
    in scope of the rules below, and the age rule then reclaims any of them
    that has gone a day without being written to.
    """
    if not _namespace and not prefix:
        raise ValueError(
            "sweep_stale needs a prefix outside a namespace: without one it "
            "would consider every directory in %s ours" % (base,))
    now = now or time.time()
    removed = 0
    try:
        names = os.listdir(base)
    except OSError:
        return 0
    for name in names:
        try:
            path = os.path.join(base, name)
            if not os.path.isdir(path) or os.path.islink(path):
                continue
            if not _reclaimable(base, name, prefix, now):
                continue
            shutil.rmtree(path, ignore_errors=True)
            removed += 1
        except Exception:
            # One unreadable or oddly-named entry must not take the app's
            # startup with it -- this runs before the browser exists.
            log.debug("could not consider %s for sweeping", name,
                      exc_info=True)
    if removed:
        log.info("Reclaimed %d abandoned cache director%s under %s",
                 removed, "y" if removed == 1 else "ies", base)
    return removed


def _is_own_directory(path):
    """Whether ``path`` is a real directory belonging to this user.

    ``lstat``, so a symlink is not a directory here whatever it points at.
    That is the question worth asking, because ``makedirs(exist_ok=True)``
    is perfectly happy with a symlink that resolves to a directory, and the
    sweep that follows would then empty out whatever it aims at.

    Owner as well as kind, for the other half of it: a directory somebody
    else created is not one to write scratch files into either, whatever
    the mode on it says. There is no ``st_uid`` to read on Windows, so
    there the kind is the whole answer -- which is still the half that
    matters, since %TEMP% is per-user there rather than shared.
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    return os.name == "nt" or st.st_uid == os.getuid()


def _namespaced(base):
    """``base`` itself, or this instance's own directory inside it. None if
    that directory cannot be made or is not ours, either of which takes the
    base out of the running.

    The check matters more here than anywhere else in this module: inside a
    namespace the sweep takes *every* directory it finds, so the claim that
    everything in there was left by a dead copy of us is only as good as the
    directory itself. The bases include /tmp and /dev/shm, which any local
    user may create entries in, under a name derived from a config path that
    is not hard to guess -- so the directory has to be shown to be ours
    rather than assumed to be, and a base that fails simply loses to the
    next one.

    ``mode=0o700`` for the same reason, on the way in: nothing here is
    anyone else's business, and a directory only this user may add entries
    to is one nobody else can plant anything inside later.
    """
    if not _namespace:
        return base
    path = os.path.join(base, _namespace)
    try:
        os.makedirs(path, mode=0o700, exist_ok=True)
    except OSError:
        return None
    if not _is_own_directory(path):
        log.warning("Not caching in %s: it is not a directory this user "
                    "owns", path)
        return None
    return path


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
    # Every candidate is swept, not just the one that wins: a run that
    # spilled to /dev/shm because the runtime dir was tight leaves its
    # directory THERE, and a later run that fits in the runtime dir again
    # would never look. tempfile's own base is in the list for the same
    # reason -- it is where Windows and macOS put everything, so leaving it
    # out meant those platforms swept nothing at all, ever.
    for cand in _bases() + [tempfile.gettempdir()]:
        if not (os.path.isdir(cand) and os.access(cand, os.W_OK)):
            continue
        home = _namespaced(cand)
        if home is None:
            continue
        sweep_stale(home, prefix)
        if base is not None:
            continue        # already chosen; this pass is only the sweep
        try:
            free = shutil.disk_usage(home).free
        except OSError:
            continue
        if free < min_free:
            log.info("Not caching in %s: %d MiB free, %d MiB wanted",
                     home, free // (1024 * 1024), min_free // (1024 * 1024))
            continue
        base = home
    # The pid is what a later run has to go on -- whose directory this was,
    # and (outside a namespace) whether that session is still running.
    # mkdtemp's own suffix keeps two runs of the same pid apart.
    path = tempfile.mkdtemp(prefix="%s%d-" % (prefix, os.getpid()), dir=base)
    _ours.add(path)
    import atexit

    atexit.register(shutil.rmtree, path, ignore_errors=True)
    return path


def cleanup_this_process():
    """Remove every cache dir this process made. Returns the number gone.

    The atexit hook in :func:`cache_dir` is the normal path. This exists for
    a caller that ends in ``os._exit`` and so runs no atexit hook at all --
    ``tools/run_tests_parallel``'s workers, which is where these accumulate:
    on Windows :func:`_process_alive` cannot say a pid is dead, so a leaked
    dir is never reclaimed by a later sweep either. 3,264 of them, ~3.4 GB,
    filled a test VM's disk and stopped the run.
    """
    removed = 0
    for path in list(_ours):
        shutil.rmtree(path, ignore_errors=True)
        _ours.discard(path)
        removed += 1
    return removed


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
