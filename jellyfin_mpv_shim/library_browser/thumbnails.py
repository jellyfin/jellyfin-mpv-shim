"""Thumbnail fetching/caching for the library browser.

Pipeline (so Tk stays on its own thread):

  request() ── worker pool ──> download bytes / read disk ──> PIL decode+resize
            └─ result queue ─> pump() on the UI thread ─> ImageTk.PhotoImage ─> callbacks

Tk image objects are thread-affine, so only the final ``ImageTk.PhotoImage``
construction happens on the UI thread; the network and decode work is offloaded.

The on-disk cache is a persistent artwork store keyed by ``(item, type, tag,
width)`` — the same store the eventual offline-sync feature will reuse, which is
why it is not just an in-memory LRU.
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
from PIL import Image, ImageTk

log = logging.getLogger("library_browser.thumbnails")


def make_key(item_id, image_type, tag, width, height=None):
    raw = "%s:%s:%s:%s:%s" % (item_id, image_type, tag, width, height)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


class ThumbnailStore:
    def __init__(self, cache_dir, verify_ssl=True, max_mem=600,
                 max_disk_mb=256, workers=6):
        self.cache_dir = cache_dir
        os.makedirs(cache_dir, exist_ok=True)
        self.verify_ssl = verify_ssl
        self.max_disk_bytes = max_disk_mb * 1024 * 1024

        self._session = requests.Session()
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="thumb")
        self._results = queue.Queue()

        self._mem = OrderedDict()          # key -> ImageTk.PhotoImage (LRU)
        self._max_mem = max_mem
        self._pending = {}                 # key -> [callback, ...] awaiting load
        self._lock = threading.Lock()
        self._closed = False

        self._prune_disk()

    # -- public API (UI thread) -------------------------------------------

    def get_cached(self, key):
        photo = self._mem.get(key)
        if photo is not None:
            self._mem.move_to_end(key)
        return photo

    def request(self, key, url, box, callback):
        """Fetch image for `key` from `url`, resized to fit `box` (w, h).

        `callback(photo)` runs on the UI thread (via pump) when ready. Multiple
        tiles requesting the same key share one fetch but each get their image.
        A synchronous mem-cache hit calls back immediately.
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

    def pump(self):
        """Drain finished work, build Tk images, deliver. Call from the UI loop."""
        while True:
            try:
                key, image = self._results.get_nowait()
            except queue.Empty:
                break
            with self._lock:
                callbacks = self._pending.pop(key, [])
            if image is None:
                continue
            try:
                photo = ImageTk.PhotoImage(image)
            except Exception:
                log.debug("Failed to build Tk image for %s", key, exc_info=True)
                continue
            self._store_mem(key, photo)
            for cb in callbacks:
                try:
                    cb(photo)
                except Exception:
                    # Widget likely destroyed mid-flight; harmless.
                    log.debug("Thumbnail callback failed", exc_info=True)

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
        try:
            image = self._load_image(key, url, box)
        except Exception:
            log.debug("Thumbnail load failed: %s", url, exc_info=True)
            image = None
        self._results.put((key, image))

    def _load_image(self, key, url, box):
        path = os.path.join(self.cache_dir, key + ".img")
        data = None
        if os.path.exists(path):
            try:
                os.utime(path, None)  # touch for LRU pruning
                with open(path, "rb") as fh:
                    data = fh.read()
            except OSError:
                data = None

        if data is None:
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

        image = Image.open(BytesIO(data))
        image = image.convert("RGB")
        image.thumbnail(box, Image.LANCZOS)
        return image

    # -- internals ---------------------------------------------------------

    def _store_mem(self, key, photo):
        self._mem[key] = photo
        self._mem.move_to_end(key)
        while len(self._mem) > self._max_mem:
            self._mem.popitem(last=False)

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
