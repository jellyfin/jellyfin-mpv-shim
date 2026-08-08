"""A comic archive: the pages, in order, on their way to mpv.

A CBZ is a zip of images and nothing else — no manifest, no spine, no
metadata, no reading order but the filenames. So this module is much
smaller than ``epub/``: it lists the pictures and hands one over as a file
on disk. It never decodes an image and never scales one.

**That is the whole design decision.** A comic page is 1600x2400 or larger
and the zoom goes well past fit-width, so drawing one the way the epub
reader draws a page — Pillow decodes it, we scale the visible part, and the
result goes through the overlay transport — costs a full decode per page
and a viewport-sized BGRA buffer per pan frame, in a bitmap cache sized for
a library's worth of artwork (``strips.StripStore.MAX_BYTES`` is 96-128 MB,
and 32 MB on a machine short of RAM). mpv already decodes pictures, keeps
them on the GPU, and has ``video-zoom`` / ``video-pan-x`` / ``video-pan-y``
— so the page is *played*, the gestures are properties, and none of it
touches Python per frame. Measured: mpv holds an image indefinitely with
``keep-open`` and ``image-display-duration=inf``, and both properties take
effect on it.

What that costs is one temporary file per page, because mpv cannot read
inside an archive. Extraction is a copy of already-compressed bytes, not a
decode.

**Zip and tar only.** ``.cbz`` is a zip and ``.cbt`` is a tar, both of
which Python ships. ``.cbr`` is RAR and ``.cb7`` is 7-Zip, and neither has
a stdlib reader — adding one means a dependency for a format the desktop
already opens, which is not a trade this project makes (CONTRIBUTING.md).
Those keep handing off to whatever the user has.

**Reading order is a natural sort of the filenames**, which is the whole of
what a CBZ says about it. Digits compare as numbers, so ``page2`` comes
before ``page10`` — which a plain sort gets backwards, and which is the
single most visible way to get a comic wrong.
"""

import logging
import os
import posixpath
import re
import shutil
import tarfile
import tempfile
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

#: How many extracted pages to keep on disk. Three, so paging back and
#: forth across a boundary does not re-extract every time.
PAGE_CACHE = 3

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

    ``page2`` before ``page10``, which is the whole of a CBZ's reading
    order and the one thing that is easy to get exactly backwards. Case is
    folded because archives are built on every platform; the path is split
    on ``/`` first so a directory never sorts into the middle of another
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
    ``epub.archive``: a route can sit in the browser's history for as long
    as the user keeps browsing, and on Windows an open handle is a lock on
    a file the downloads screen may want to delete.
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
        hit = self._extracted.get(index)
        if hit and os.path.exists(hit):
            return hit
        name = self.pages[index]
        suffix = os.path.splitext(name)[1].lower() or ".img"
        dest = os.path.join(self.dir(), "page%05d%s" % (index, suffix))
        data = self._read(index, name)
        tmp = dest + ".part"
        with open(tmp, "wb") as handle:
            handle.write(data)
        # Written aside and renamed: mpv is told to load this path from
        # another thread, and a half-written JPEG is a decode error rather
        # than a wait.
        os.replace(tmp, dest)
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
        while len(self._extracted) > PAGE_CACHE:
            for index in list(self._extracted):
                if index == keep:
                    continue
                self._remove(index)
                break
            else:
                return

    def _remove(self, index):
        path = self._extracted.pop(index, None)
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            log.debug("could not remove %s", path, exc_info=True)

    def close(self):
        """Drop every extracted page.

        Called when the reader leaves, and safe to call twice. The archive
        stays usable — the next :meth:`page_path` extracts again — which is
        what lets the shell close a comic without knowing whether the page
        is coming back.
        """
        for index in list(self._extracted):
            self._remove(index)
        if self._own_dir and self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
