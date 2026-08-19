"""A comic archive: the pages, in order, on their way to mpv.

A CBZ is a zip of images and nothing else — no manifest, no spine, no
metadata, no reading order but the filenames. So this module is much
smaller than ``epub/``: it lists the pictures and hands one over as a file
on disk. It never decodes an image and never scales one.

**A page is *played*, not drawn** — handed to mpv as a file, with the
gestures expressed as ``video-zoom`` / ``video-pan-x`` / ``video-pan-y``,
so nothing touches Python per frame. The measurements behind that, and the
cost (one temporary file per page, because mpv cannot read inside an
archive), are ``docs/readers.md`` §5.

**Zip and tar only**, and **reading order is a natural sort of the
filenames** — digits compared as numbers, which a plain sort gets exactly
backwards and which is the single most visible way to get a comic wrong.
See ``docs/readers.md`` §5.1.
"""

import logging
import os
import posixpath
import re
import shutil
import tarfile
import tempfile
import threading
import zipfile

log = logging.getLogger("comic")

#: Extensions holding pages this module can read.
COMIC_EXTENSIONS = ("cbz", "cbt")

#: Comics that need a compressor Python does not ship. Named so the book
#: screens can say *why* one opens externally rather than in the window.
UNREADABLE_COMIC_EXTENSIONS = ("cbr", "cb7")

#: Image suffixes counted as pages. Anything else in the archive (a
#: ``ComicInfo.xml``, a ``Thumbs.db``, a readme) is not one.
PAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif",
                 ".jxl")

#: Cap on one page, applied to what it *delivers* rather than to the size
#: its header claims — the same rule as ``epub.archive``, and for the same
#: reason: the declared size is attacker-controlled and the delivered one
#: is not. A 64 MB entry in a comic is not a page.
MAX_PAGE_BYTES = 64 * 1024 * 1024

#: How many extracted pages to keep on disk.
#:
#: **Above the browser's pool width**, which is what makes it a cache and
#: not a hazard: extractions run on that pool, so with a narrower cache a
#: burst of page turns has one worker trimming a file another worker has
#: just written and handed to mpv. Eight is generous for a few hundred KB
#: each and leaves room for paging back and forth across a boundary.
PAGE_CACHE = 8

_DIGITS = re.compile(r"(\d+)")


class ComicError(Exception):
    """This file cannot be read as a comic, with a reason for the user."""


def comic_format(path):
    """Lowercase extension of a comic path, or None."""
    return os.path.splitext(path or "")[1].lstrip(".").lower() or None


def is_readable_comic(path):
    """Whether this reader can open ``path`` in the window."""
    return comic_format(path) in COMIC_EXTENSIONS


def natural_key(name):
    """Sort key that reads runs of digits as numbers.

    ``page2`` before ``page10`` (``docs/readers.md`` §5.1). Case is folded
    because archives are built on every platform; the path is split on
    ``/`` first so a directory never sorts into the middle of another
    directory's contents.
    """
    out = []
    for part in (name or "").split("/"):
        key = []
        for chunk in _DIGITS.split(part):
            if chunk.isdigit():
                # The leading flag keeps an int from being compared against
                # a str when two names differ in shape at the same
                # position; a number sorts after text there.
                key.append((1, int(chunk), ""))
            elif chunk:
                key.append((0, 0, chunk.lower()))
        out.append(tuple(key))
    return tuple(out)


class ComicArchive:
    """The pages of one comic, extracted on demand.

    Holds no open file handle between reads, for the reasons in
    ``epub.archive`` and ``docs/readers.md`` §4.5.
    """

    def __init__(self, path, cache_dir=None):
        self.path = path
        if not os.path.exists(path):
            raise ComicError("no such file: %s" % path)
        self.kind = "tar" if comic_format(path) == "cbt" else "zip"
        self.pages = self._list()
        if not self.pages:
            raise ComicError("there are no pages in this file")
        self._dir = cache_dir
        self._own_dir = cache_dir is None
        #: index -> extracted path, most recent last.
        self._extracted = {}
        #: Extraction runs on the browser's worker pool, several jobs deep
        #: when a page-turn key is held, while ``close()`` runs on the loop
        #: thread. Without this, one worker's trim raced another's write and
        #: ``close()``'s rmtree landed in the middle of an ``open(...)``.
        self._lock = threading.RLock()
        #: Set by close(). Checked wherever the directory would be created,
        #: so a worker that arrives after the reader has gone gives up
        #: instead of mkdtemp'ing a directory nobody will ever remove.
        self._closed = False

    # -- entries -----------------------------------------------------------

    def _open(self):
        try:
            if self.kind == "tar":
                return tarfile.open(self.path)
            return zipfile.ZipFile(self.path)
        except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
            raise ComicError("not a readable comic file: %s" % exc) from exc

    def _list(self):
        with self._open() as archive:
            if self.kind == "tar":
                names = [m.name for m in archive.getmembers() if m.isfile()]
            else:
                names = [i.filename for i in archive.infolist()
                         if not i.is_dir()]
        # "__MACOSX/" entries and "._" siblings are resource forks, not
        # pages, and every archive built on a Mac has one per real file.
        pages = [n for n in names
                 if os.path.splitext(n)[1].lower() in PAGE_SUFFIXES
                 and not n.startswith("__MACOSX/")
                 and not posixpath.basename(n).startswith("._")]
        pages.sort(key=natural_key)
        return pages

    def __len__(self):
        return len(self.pages)

    @property
    def page_count(self):
        return len(self.pages)

    def page_name(self, index):
        return self.pages[index] if 0 <= index < len(self.pages) else ""

    # -- extraction --------------------------------------------------------

    def dir(self):
        """The extraction directory, made on demand. None once closed."""
        with self._lock:
            if self._closed:
                return None
            if self._dir is None:
                self._dir = tempfile.mkdtemp(prefix="jmpvs-comic-")
            return self._dir

    def page_path(self, index):
        """Page ``index`` as a file on disk, extracted if it is not already.

        The name carries the page number and the original suffix: mpv picks
        its demuxer by content rather than by extension, but a path that
        says what it is makes a log or a crash report legible.
        """
        if not 0 <= index < len(self.pages):
            raise ComicError("no page %d" % index)
        with self._lock:
            hit = self._extracted.get(index)
            if hit and os.path.exists(hit):
                return hit
        name = self.pages[index]
        suffix = os.path.splitext(name)[1].lower() or ".img"
        where = self.dir()
        if where is None:
            raise ComicError("this comic has been closed")
        dest = os.path.join(where, "page%05d%s" % (index, suffix))
        # The read and the write happen OUTSIDE the lock: a page is a few
        # hundred KB of decompression and several of these run at once when
        # a turn key is held, so holding the lock across them would serialise
        # the pool for no benefit. Only the bookkeeping is guarded, and two
        # workers extracting the same page write the same bytes to the same
        # path, which os.replace makes atomic.
        data = self._read(index, name)
        tmp = "%s.%d.part" % (dest, threading.get_ident())
        try:
            with open(tmp, "wb") as handle:
                handle.write(data)
            # Written aside and renamed: mpv is told to load this path from
            # another thread, and a half-written JPEG is a decode error
            # rather than a wait.
            os.replace(tmp, dest)
        except OSError as exc:
            # The reader left while this was in flight and close() took the
            # directory. A ComicError so the caller's error path reports
            # something a person can read rather than a raw errno.
            self._unlink(tmp)
            raise ComicError("page %d could not be unpacked: %s"
                             % (index, exc)) from exc
        with self._lock:
            if self._closed:
                self._unlink(dest)
                raise ComicError("this comic has been closed")
            self._extracted[index] = dest
            self._trim(index)
        return dest

    def _read(self, index, name):
        try:
            with self._open() as archive:
                if self.kind == "tar":
                    handle = archive.extractfile(name)
                    if handle is None:
                        raise ComicError("page %d is not a file" % index)
                else:
                    handle = archive.open(name)
                with handle:
                    data = handle.read(MAX_PAGE_BYTES + 1)
        except ComicError:
            raise
        except (OSError, tarfile.TarError, zipfile.BadZipFile, RuntimeError,
                EOFError) as exc:
            # RuntimeError is zipfile's answer for an encrypted entry — a
            # password-protected comic, which is a real case whose message
            # should not read as corruption.
            raise ComicError("cannot read page %d: %s" % (index, exc)) from exc
        if len(data) > MAX_PAGE_BYTES:
            raise ComicError("page %d is larger than %d bytes"
                             % (index, MAX_PAGE_BYTES))
        return data

    def _trim(self, keep):
        """Drop the oldest extractions. Caller holds the lock."""
        while len(self._extracted) > PAGE_CACHE:
            for index in list(self._extracted):
                if index == keep:
                    continue
                self._remove(index)
                break
            else:
                return

    def _remove(self, index):
        self._unlink(self._extracted.pop(index, None))

    @staticmethod
    def _unlink(path):
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            log.debug("could not remove %s", path, exc_info=True)

    def close(self):
        """Drop every extracted page. Safe to call twice, and final.

        **Final**, unlike the epub reader's close(): this one deletes files
        that a worker may be in the middle of writing, so "usable again
        afterwards" would mean racing the rmtree with a mkdtemp on every
        re-entry. `_closed` makes a late worker give up instead — the page
        object opens a fresh archive when the route is re-entered, which
        costs one central-directory read.
        """
        with self._lock:
            self._closed = True
            for index in list(self._extracted):
                self._remove(index)
            directory, self._dir = self._dir, None
        if self._own_dir and directory:
            shutil.rmtree(directory, ignore_errors=True)
