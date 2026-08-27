"""Thumbnail fetching/caching for the mpvtk browser.

Pipeline (network + decode off the UI/loop thread):

  request() ── worker pool ──> download bytes / read disk ──> PIL decode+resize
            └─ result queue ─> pump() on the loop thread ─> callbacks(PIL.Image)

Unlike the Tk browser's store, this yields **PIL images** — the strip
compositor pastes them into row bitmaps — so nothing here is thread-affine
and there is no final ``ImageTk`` step. ``notify`` (thread-safe) is called
when a result lands so the owner can wake its loop (``MpvtkApp.invalidate``)
and drain via ``pump()`` on the next render.

The on-disk cache is a persistent artwork store keyed by ``(item, type, tag,
width)`` — the same store the offline-sync feature reuses, which is why it is
not just an in-memory LRU.
"""

import hashlib
import logging
import os
import shutil
from urllib.parse import urlparse
import queue
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO
from typing import Optional

import requests
from PIL import Image

try:
    import pillow_jxl
except ImportError:
    pass


log = logging.getLogger("mpvtk_browser.thumbnails")

# Default in-memory budget for decoded images, sized by bytes (not entry
# count) so a mix of small posters and large backdrops can't balloon memory.
# Kept equal to conf.py's library_image_cache_mb, which is what the app
# actually passes -- see there for why it is a working set and not a library.
DEFAULT_MEM_MB = 96

# ...and on disk, where what is kept is the server's own COMPRESSED bytes,
# not decoded pixels: a poster is 20-80 KiB here against ~300 KiB decoded.
#
# The budget follows the MEDIUM, which is what these two numbers are for.
# Artwork is long-lived -- the cache key folds in the server's own image tag,
# so an entry is never wrong, only unwanted -- and it belongs on a disk that
# keeps it between launches. A gigabyte there is a whole large library's
# artwork at every size it has been drawn at, kept for as long as it is being
# used, on a medium where that is a rounding error.
#
# The small one is the fallback, for when there is nowhere persistent to put
# it and the cache lands in the session's tmpfs instead. That is RAM, on
# machines that may only have 8 GiB of it, and everything in it dies with the
# session anyway, so it gets a sixteenth of the room.
#
# Neither is a promise: see ThumbnailStore.LOW_DISK_SHARE.
DEFAULT_DISK_MB = 1024
SCRATCH_DISK_MB = 64


def disk_cache(app=None):
    """Where the artwork cache goes, and how much it may hold there.

    Persistent config dir by preference, RAM-backed scratch as a fallback --
    and **the budget follows the medium**, because a gigabyte of somebody's
    disk and a gigabyte of their RAM are not the same offer. Measures
    compressed bytes on disk, not decoded pixels.
    See docs/artwork-pipeline.md section 10.1.
    """
    from ..conffile import get_cache_dir
    from ..constants import APP_NAME

    path = get_cache_dir(app or APP_NAME, "artwork")
    if path is not None:
        return path, DEFAULT_DISK_MB
    from ..mpvtk.rawimage import cache_dir

    log.info("No persistent cache directory; artwork will not be kept "
             "between launches.")
    return cache_dir("mpvtk-thumbs-"), SCRATCH_DISK_MB

# Distinct hosts we keep pools for. Small on purpose: this store talks to the
# logged-in Jellyfin servers and nothing else.
THUMB_POOL_HOSTS = 4


def make_key(item_id, image_type, tag, width, height=None, fit=""):
    """Identity of one cached bitmap.

    ``fit`` distinguishes two crops of the same source at the same size --
    the server can be asked to fill the box or to fit inside it, and the two
    are different pictures. Empty by default so every existing key (and every
    file already on disk under one) is unchanged.
    """
    raw = "%s:%s:%s:%s:%s%s" % (item_id, image_type, tag, width, height,
                                (":" + fit) if fit else "")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class MemoryCache:
    """Byte-bounded LRU of decoded images.

    Sizing is approximate (``sizer(value)``); least-recently-used entries are
    evicted until the total is back under budget. No UI dependency, so the
    eviction policy is unit-testable without a display.
    """

    def __init__(self, max_bytes, sizer):
        self._max_bytes = max_bytes
        self._sizer = sizer
        self._items = OrderedDict()   # key -> (value, nbytes)
        self._bytes = 0

    def get(self, key):
        item = self._items.get(key)
        if item is None:
            return None
        self._items.move_to_end(key)
        return item[0]

    def put(self, key, value):
        old = self._items.pop(key, None)
        if old is not None:
            self._bytes -= old[1]
        nbytes = self._sizer(value)
        self._items[key] = (value, nbytes)
        self._bytes += nbytes
        # Keep at least the just-inserted entry so a single oversized image
        # isn't evicted the moment it lands (its caller still wants it).
        while self._bytes > self._max_bytes and len(self._items) > 1:
            _k, (_v, nb) = self._items.popitem(last=False)
            self._bytes -= nb

    def trim(self, max_bytes):
        """Drop least-recently-used entries until the total is within
        ``max_bytes``. Unlike ``put``'s eviction this may empty the cache
        entirely -- it is called when the caller knows the entries have
        stopped being interesting, not when one more has to fit."""
        while self._bytes > max_bytes and self._items:
            _k, (_v, nb) = self._items.popitem(last=False)
            self._bytes -= nb

    def __len__(self):
        return len(self._items)

    @property
    def nbytes(self):
        return self._bytes


def _fit_into(image, box):
    """Crop ``image`` to ``box``'s SHAPE, at the largest size it can manage.

    Cover-crop rather than ``thumbnail()``, and **it never upscales** -- a
    contain would blow a 16:9 still up 2.65x to fill a 200x300 poster tile.
    Pinned by tests/test_grid_artwork.py:271.
    See docs/artwork-pipeline.md section 10.4.
    """
    from ..imageutil import scale_to_cover

    bw, bh = max(1, box[0]), max(1, box[1])
    iw, ih = image.size
    if not iw or not ih:
        return image
    shrink = min(iw / bw, ih / bh)
    if shrink < 1.0:
        bw, bh = max(1, int(bw * shrink)), max(1, int(bh * shrink))
    if (iw, ih) == (bw, bh):
        return image
    return scale_to_cover(image, bw, bh)


def _image_bytes(image):
    """Approximate resident size of a decoded PIL image."""
    try:
        return image.width * image.height * len(image.getbands())
    except Exception:
        return 0


class ThumbnailStore:
    #: Bytes written between prunes. A prune stats the whole cache dir, so
    #: it is paced by traffic rather than run per file -- a few times per
    #: browsing session instead of a few times per screen.
    PRUNE_EVERY = 16 * 1024 * 1024

    #: The most of a filesystem the cache will ever occupy, whatever its
    #: configured budget says: five per cent of what is available to it.
    #:
    #: One rule rather than a "low disk space" mode, because it already is
    #: one. On a roomy disk 5% is far more than the budget and the budget
    #: binds; the share only starts to matter below ~20 GiB free, and from
    #: there it shrinks the cache continuously instead of waiting for a
    #: threshold to trip. It is measured against free space *plus what we
    #: already hold*, or the cache would ratchet its own allowance down
    #: every time it grew.
    LOW_DISK_SHARE = 0.05

    #: ...and the floor that headroom may not push the cache below. A cache
    #: too small for one screenful re-fetches every tile on every scroll,
    #: which costs the server and the user more than the space saves.
    MIN_DISK_BYTES = 24 * 1024 * 1024

    #: How long an untouched entry is kept -- the bound the size limit
    #: cannot supply. The key folds in the image tag and the requested
    #: pixel size, so changing Cover Size, the tile shape or the window
    #: *orphans* every entry made for the old one, and an orphan is only
    #: recognisable by nobody having read it. A month keeps a library you
    #: go back to seasonally warm. docs/artwork-pipeline.md section 10.2.
    MAX_AGE_SECS = 30 * 24 * 60 * 60

    def __init__(self, cache_dir, verify_ssl=True, max_mem_mb=DEFAULT_MEM_MB,
                 max_disk_mb=DEFAULT_DISK_MB, workers=6, notify=None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.verify_ssl = verify_ssl
        self.max_disk_bytes = max_disk_mb * 1024 * 1024
        # Called (thread-safe, no args) when a result is enqueued, so the
        # owner can wake its render loop; it then drains via pump().
        # Use set_notify() to attach one after construction.
        self._notify = notify

        self._session = requests.Session()
        # Size the pool to the worker count and BLOCK on exhaustion.
        # urllib3's pool_block=False default does not queue an over-limit
        # request: it opens a fresh connection and discards it, which is a
        # churn of one-shot TLS handshakes exactly while mpv is opening the
        # stream. docs/artwork-pipeline.md section 10.6.
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=THUMB_POOL_HOSTS,
            pool_maxsize=workers,
            pool_block=True,
        )
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        # origin -> Authorization header, registered by the source when its
        # connections are built. Images are by far the highest-volume
        # first-party requests this app makes, and putting the token in the
        # query string means an admin cannot put Jellyfin behind a proxy
        # that rejects unauthenticated traffic -- every tile would 401.
        #
        # Keyed by origin rather than held as one header because a session
        # can hold several servers at once, and a token is only ever sent to
        # the server it came from.
        self._auth: dict = {}
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="thumb")
        self._results: "queue.Queue[tuple[str, Optional[Image.Image]]]" = \
            queue.Queue()

        # key -> PIL.Image, LRU-evicted by approximate byte size.
        self._mem = MemoryCache(max_mem_mb * 1024 * 1024, _image_bytes)
        self._pending = {}                 # key -> [callback, ...] awaiting load
        # Keys the server answered "there is no such image" for (4xx). The
        # caller is told so it can stop asking, rather than retrying a miss
        # forever. Transient failures (timeout, connection, 5xx) are NOT
        # recorded here — those must stay retryable.
        self._gone = set()
        self._lock = threading.Lock()
        self._closed = False
        # Bytes written since the last prune; see _note_written.
        self._unpruned = 0
        self._prune_lock = threading.Lock()

        # Once at startup -- but NOT on this thread. A listdir plus a stat
        # per entry over a persistent cache is seconds on NTFS, and this
        # runs on the path that opens the browser window.
        #
        # The future is kept so the prune can be waited for. Nothing in the
        # app does; it reaps by mtime against a directory the caller may
        # keep writing to, so a test reasoning about what is in there has
        # to be able to let it finish. docs/artwork-pipeline.md section 10.3.
        self._startup_prune = self._pool.submit(self._prune_disk)

    # -- public API (loop thread) -----------------------------------------

    def set_notify(self, notify):
        """Attach the wake-up callback after construction.

        The store outlives any one owner and is usually built before the
        thing that wants waking — the browser is handed a ready-made store —
        so the constructor argument alone isn't enough, and the browser used
        to reach in and assign ``_notify`` directly.

        A plain assignment: worker threads only ever read it, and swapping
        one callable for another is atomic.
        """
        self._notify = notify

    #: What the decoded cache keeps when the screen changes. Not zero: the
    #: chrome that outlives a navigation draws from this cache too -- the
    #: now-playing bar's album art most visibly -- and clearing outright
    #: would give it a placeholder for a frame or two on every single move
    #: between screens. Anything asked for on the frame just before the
    #: change is at the recent end and survives; a screenful of posters is
    #: ~7 MiB, so this keeps the chrome and sheds the screen.
    ROUTE_KEEP_BYTES = 16 * 1024 * 1024

    def trim_memory(self, max_bytes=None):
        """Shrink the decoded-image cache to ``max_bytes``.

        Decoded artwork is the cheapest thing this app can shed: it exists only
        to composite strips, and a screenful is ~7 MiB against ~400 KB on the
        wire. See docs/artwork-pipeline.md section 10.
        """
        self._mem.trim(self.ROUTE_KEEP_BYTES if max_bytes is None
                       else max_bytes)

    def get_cached(self, key):
        return self._mem.get(key)

    def is_gone(self, key):
        """True once the server has said this image doesn't exist, so the
        caller can stop re-requesting it (see ``request``)."""
        with self._lock:
            return key in self._gone

    def request(self, key, url, box, callback, cover=False):
        """Fetch image for `key` from `url`, resized to fit `box` (w, h).

        ``cover`` asks for the box FILLED rather than fitted -- see
        :func:`_fit_into`. The caller has to say, because whether artwork is
        cover-cropped is a property of the tile it is going into and not of
        the picture (``TileRenderer._contains``), and the key it hands over
        already distinguishes the two.

        `callback(image)` runs on the loop thread (via pump) when ready, with a
        decoded ``PIL.Image``. Multiple callers requesting the same key share
        one fetch but each get the image. A synchronous mem-cache hit calls back
        immediately.

        On failure the callback still runs, with ``image=None``, so the caller
        can release whatever dedup marker it holds and retry later — a fetch
        that fails silently used to leave the tile blank permanently. Use
        ``is_gone`` to tell a permanent miss from a retryable one.
        """
        cached = self.get_cached(key)
        if cached is not None:
            callback(cached)
            return

        with self._lock:
            if self._closed:
                return
            waiters = self._pending.get(key)
            if waiters is not None:
                waiters.append(callback)
                return
            self._pending[key] = [callback]

        self._pool.submit(self._work, key, url, box, cover)

    def cancel(self, key, callback=None):
        """Drop a pending fetch (or a single waiter of one).

        A queued ``_work`` whose key is no longer pending short-circuits before
        downloading, so a fast-scrolled backlog can't delay the next view's
        artwork. If other callers still await the same key, only the given
        callback is removed and the fetch continues.
        """
        with self._lock:
            waiters = self._pending.get(key)
            if waiters is None:
                return
            if callback is not None:
                try:
                    waiters.remove(callback)
                except ValueError:
                    pass
                if waiters:
                    return
            self._pending.pop(key, None)

    def pump(self):
        """Drain finished work and deliver decoded images. Call from the loop.

        Returns True if it delivered at least one image (so the caller can
        decide whether a re-render is warranted)."""
        delivered = False
        while True:
            try:
                key, image = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                callbacks = self._pending.pop(key, [])
            if image is not None:
                self._mem.put(key, image)
            for cb in callbacks:
                try:
                    # Failures are delivered too (image=None). Dropping them
                    # here left every waiter's dedup marker set with nothing
                    # cached, so the tile never loaded again.
                    cb(image)
                    delivered = delivered or image is not None
                except Exception:
                    # Caller likely torn down mid-flight; harmless.
                    log.debug("Thumbnail callback failed", exc_info=True)
        return delivered

    def shutdown(self):
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=False, cancel_futures=True)
        try:
            self._session.close()
        except Exception:
            pass

    # -- worker thread -----------------------------------------------------

    def _work(self, key, url, box, cover=False):
        # Skip work cancelled before this task got a worker (a tile scrolled
        # off or its view was torn down) — clears the fast-scroll backlog
        # without touching the network.
        with self._lock:
            if key not in self._pending:
                return
        try:
            image = self._load_image(key, url, box, cover)
        except Exception as e:
            log.debug("Thumbnail load failed: %s", url, exc_info=True)
            image = None
            # A 4xx means the image isn't there and never will be at this
            # URL; anything else (timeout, connection reset, 5xx, a
            # truncated body) is transient and must stay retryable.
            resp = getattr(e, "response", None)
            status = getattr(resp, "status_code", None)
            if status is not None and 400 <= status < 500:
                with self._lock:
                    self._gone.add(key)
        self._results.put((key, image))
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                log.debug("Thumbnail notify failed", exc_info=True)

    def _load_image(self, key, url, box, cover=False):
        if url.startswith("http://") or url.startswith("https://"):
            data = self._load_remote(key, url)
        else:
            # Local file (offline artwork) — read directly, no network cache.
            with open(url, "rb") as fh:
                data = fh.read()

        # Annotated as the base class: open() hands back an ImageFile and
        # convert() a plain Image, and the name is reused for both.
        image: Image.Image = Image.open(BytesIO(data))
        # RGB unconditionally used to be the rule here, and convert() does not
        # composite — it simply drops the alpha channel and keeps whatever RGB
        # was underneath, which for a transparent PNG is black. Channel logos
        # are shipped exactly that way, so a black-on-transparent logo arrived
        # as a solid black block. Carry the alpha instead and let the
        # compositors decide what goes behind it; RGB is still the path for
        # everything without transparency (every poster and backdrop), which
        # keeps a byte per pixel off the decoded-image cache.
        if image.mode in ("RGBA", "LA", "PA") or "transparency" in image.info:
            image = image.convert("RGBA")
        else:
            image = image.convert("RGB")
        # The checker is wrong below, not the code: PIL installs its filter
        # constants onto its own module with setattr() at import time, so they
        # exist at runtime and are invisible to any static analysis. Spelling
        # it Resampling.LANCZOS instead would require Pillow >= 9.1, which this
        # project does not pin.
        if cover and image.mode != "RGBA":
            # Transparency opts back out: a mark on a transparent background
            # is not a photograph, and the compositor refuses to crop one
            # whatever the tile says (strips._paint_poster's `plate` arm).
            # Cropping it here would be a decision taken before the thing
            # that decides has looked.
            image = _fit_into(image, box)
        else:
            image.thumbnail(box, Image.LANCZOS)  # type: ignore[attr-defined]
        if image.mode == "RGBA":
            # Measured here, on the worker, because the answer is a property of
            # the artwork and the callers that need it are the loop thread and
            # the strip pool. See imageutil.plate_for.
            from ..imageutil import measure_transparency

            measure_transparency(image)
        return image

    def set_auth(self, origins):
        """``{origin: authorization-header}`` for the servers now connected.

        Replaced wholesale rather than merged: a server the user has just
        signed out of must stop receiving its old token.
        """
        self._auth = dict(origins or {})

    def _headers_for(self, url):
        """Headers for an artwork request: our user agent always, and the
        server's ``Authorization`` when the url belongs to a server we are
        signed in to.

        **Per origin, and as a header rather than a query-string token** -- a
        token in the query string breaks a caching proxy, and it lands in
        anybody's access log. See docs/artwork-pipeline.md section 10.6.
        """
        from ..constants import USER_AGENT

        headers = {"User-Agent": USER_AGENT}
        try:
            parts = urlparse(url)
        except Exception:
            return headers
        header = self._auth.get((parts.scheme, parts.hostname, parts.port))
        if header:
            headers["Authorization"] = header
        return headers

    def _load_remote(self, key, url):
        path = os.path.join(self.cache_dir, key + ".img")
        if os.path.exists(path):
            data = None
            try:
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                pass
            if data is not None:
                # AFTER the read, in its own try. The touch is what makes the
                # age bound mean "unused for a month" rather than "fetched a
                # month ago", but sharing a try with the read meant a cache
                # dir that would not take a utime (read-only, wrong owner)
                # re-downloaded and re-wrote every image on every request.
                try:
                    os.utime(path, None)
                except OSError:
                    pass
                return data

        resp = self._session.get(url, timeout=(5, 20), verify=self.verify_ssl,
                                 headers=self._headers_for(url))
        resp.raise_for_status()
        data = resp.content
        # Unique per writer. Two writers for one key is reachable inside a
        # single process -- cancel() drops a pending entry while its worker
        # is still in here and a re-request submits a second one -- and
        # trivially so across instances, now that the cache is shared and
        # persistent. On a shared name the loser's writes land in the inode
        # the winner has already renamed into place.
        tmp = "%s.%d.%d.tmp" % (path, os.getpid(), threading.get_ident())
        written = False
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
            written = True
        except OSError:
            log.debug("Could not write thumbnail cache %s", path, exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
        if written:
            # Outside the try. Inside it, one failed replace -- Windows, over
            # a file another reader has open -- also skipped the accounting,
            # so the prune trigger stalled for the rest of the session.
            self._note_written(len(data))
        return data

    def _note_written(self, nbytes):
        """Count bytes towards the next prune, and prune when they add up.

        The budget used to be applied once, in ``__init__``, against a
        directory that had just been created -- so it measured an empty dir,
        found nothing to do, and the cache then grew without limit for the
        whole session. A long browse writes every poster and backdrop it
        ever fetched, which on a RAM-backed cache dir is a session filling
        the tmpfs the rest of the desktop shares (#3).

        Counting writes rather than pruning per write, because a prune
        stats the whole directory: at PRUNE_EVERY it happens a few times
        per browsing session instead of a few times per screen.
        """
        with self._lock:
            self._unpruned += nbytes
            due = self._unpruned >= self.PRUNE_EVERY
            if due:
                self._unpruned = 0
        if due:
            self._prune_disk()

    # -- internals ---------------------------------------------------------

    def _disk_budget(self, held):
        """The cache's ceiling right now, in bytes, given the ``held`` we
        are already using.

        Usually ``max_disk_bytes``. But free space is not ours alone --
        something else can take it while we are running, and the configured
        budget then describes a cache that no longer fits. So the ceiling is
        also never more than LOW_DISK_SHARE of what is available, and the
        smaller of the two wins. A cache that shrinks as a disk fills gives
        space back rather than merely stopping where it is, which matters
        because somebody else filling it is the common case and holding
        still does not help them.

        MIN_DISK_BYTES is the floor -- a cache too small to hold one
        screenful re-fetches every tile on every scroll, which is worse for
        everyone than the megabytes it saves -- but not an unconditional
        one: on a filesystem where even that is a quarter of everything
        left, the floor is the rudeness, so it gives way too.
        """
        try:
            free = shutil.disk_usage(self.cache_dir).free
        except OSError:
            return self.max_disk_bytes
        # Available = free space plus what this cache is already holding,
        # since freeing our own entries is what makes room for the rest.
        room = max(0, held + free)
        share = int(room * self.LOW_DISK_SHARE)
        floor = min(self.MIN_DISK_BYTES, room // 4)
        return min(self.max_disk_bytes, max(share, floor))

    def _prune_disk(self, now=None):
        """One pruner at a time; the work is in _prune_disk_locked.

        Two workers -- or two app instances sharing the persistent cache --
        each scan, each compute the same total, and each delete towards it.
        But a file the other already removed raises ENOENT, so without this
        the loser never decremented its own running total and kept deleting:
        between them they evicted roughly twice the excess.
        """
        if not self._prune_lock.acquire(blocking=False):
            return
        try:
            self._prune_disk_locked(now)
        finally:
            self._prune_lock.release()

    def _prune_disk_locked(self, now=None):
        """Reap by age, then by size. Both, because they answer different
        questions: the size bound decides what a busy cache may keep, and
        the age bound decides what an idle one is still *for* (see
        MAX_AGE_SECS -- a cache pinned at its ceiling by artwork nobody can
        reach any more is a cache doing no good at its full price)."""
        now = now or time.time()
        try:
            entries = []
            total = 0
            for name in os.listdir(self.cache_dir):
                full = os.path.join(self.cache_dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                if now - st.st_mtime > self.MAX_AGE_SECS:
                    try:
                        os.remove(full)
                        continue
                    except OSError:
                        pass
                entries.append((st.st_mtime, st.st_size, full))
                total += st.st_size
            budget = self._disk_budget(total)
            if total <= budget:
                return
            log.debug("Thumbnail cache at %d MiB, pruning to %d MiB",
                      total // (1024 * 1024), budget // (1024 * 1024))
            entries.sort()  # oldest first
            for _mtime, size, full in entries:
                if total <= budget:
                    break
                try:
                    os.remove(full)
                    total -= size
                except FileNotFoundError:
                    # Another instance got there first. The space is freed
                    # either way, so it still comes off the running total --
                    # not doing so is what made two pruners over-evict.
                    total -= size
                except OSError:
                    pass
        except OSError:
            log.debug("Disk cache prune failed", exc_info=True)
