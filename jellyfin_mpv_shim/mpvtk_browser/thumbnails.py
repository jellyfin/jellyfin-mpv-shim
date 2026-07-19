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
import queue
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from io import BytesIO

import requests
from PIL import Image

log = logging.getLogger("mpvtk_browser.thumbnails")

# Default in-memory budget for decoded images, sized by bytes (not entry
# count) so a mix of small posters and large backdrops can't balloon memory.
DEFAULT_MEM_MB = 128


def make_key(item_id, image_type, tag, width, height=None):
    raw = "%s:%s:%s:%s:%s" % (item_id, image_type, tag, width, height)
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

    def __len__(self):
        return len(self._items)

    @property
    def nbytes(self):
        return self._bytes


def _image_bytes(image):
    """Approximate resident size of a decoded PIL image."""
    try:
        return image.width * image.height * len(image.getbands())
    except Exception:
        return 0


class ThumbnailStore:
    def __init__(self, cache_dir, verify_ssl=True, max_mem_mb=DEFAULT_MEM_MB,
                 max_disk_mb=256, workers=6, notify=None):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.verify_ssl = verify_ssl
        self.max_disk_bytes = max_disk_mb * 1024 * 1024
        # Called (thread-safe, no args) when a result is enqueued, so the
        # owner can wake its render loop; it then drains via pump().
        self._notify = notify

        self._session = requests.Session()
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="thumb")
        self._results = queue.Queue()

        # key -> PIL.Image, LRU-evicted by approximate byte size.
        self._mem = MemoryCache(max_mem_mb * 1024 * 1024, _image_bytes)
        self._pending = {}                 # key -> [callback, ...] awaiting load
        self._lock = threading.Lock()
        self._closed = False

        self._prune_disk()

    # -- public API (loop thread) -----------------------------------------

    def get_cached(self, key):
        return self._mem.get(key)

    def request(self, key, url, box, callback):
        """Fetch image for `key` from `url`, resized to fit `box` (w, h).

        `callback(image)` runs on the loop thread (via pump) when ready, with a
        decoded ``PIL.Image``. Multiple callers requesting the same key share
        one fetch but each get the image. A synchronous mem-cache hit calls back
        immediately.
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

        self._pool.submit(self._work, key, url, box)

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
            if image is None:
                continue
            self._mem.put(key, image)
            for cb in callbacks:
                try:
                    cb(image)
                    delivered = True
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

    def _work(self, key, url, box):
        # Skip work cancelled before this task got a worker (a tile scrolled
        # off or its view was torn down) — clears the fast-scroll backlog
        # without touching the network.
        with self._lock:
            if key not in self._pending:
                return
        try:
            image = self._load_image(key, url, box)
        except Exception:
            log.debug("Thumbnail load failed: %s", url, exc_info=True)
            image = None
        self._results.put((key, image))
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                log.debug("Thumbnail notify failed", exc_info=True)

    def _load_image(self, key, url, box):
        if url.startswith("http://") or url.startswith("https://"):
            data = self._load_remote(key, url)
        else:
            # Local file (offline artwork) — read directly, no network cache.
            with open(url, "rb") as fh:
                data = fh.read()

        image = Image.open(BytesIO(data))
        image = image.convert("RGB")
        image.thumbnail(box, Image.LANCZOS)
        return image

    def _load_remote(self, key, url):
        path = os.path.join(self.cache_dir, key + ".img")
        if os.path.exists(path):
            try:
                os.utime(path, None)  # touch for LRU pruning
                with open(path, "rb") as fh:
                    return fh.read()
            except OSError:
                pass

        resp = self._session.get(url, timeout=(5, 20), verify=self.verify_ssl)
        resp.raise_for_status()
        data = resp.content
        tmp = path + ".tmp"
        try:
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, path)
        except OSError:
            log.debug("Could not write thumbnail cache %s", path, exc_info=True)
        return data

    # -- internals ---------------------------------------------------------

    def _prune_disk(self):
        try:
            entries = []
            total = 0
            for name in os.listdir(self.cache_dir):
                full = os.path.join(self.cache_dir, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                entries.append((st.st_mtime, st.st_size, full))
                total += st.st_size
            if total <= self.max_disk_bytes:
                return
            entries.sort()  # oldest first
            for _mtime, size, full in entries:
                if total <= self.max_disk_bytes:
                    break
                try:
                    os.remove(full)
                    total -= size
                except OSError:
                    pass
        except OSError:
            log.debug("Disk cache prune failed", exc_info=True)
