"""The epub container: a zip, a package document, and a spine.

This is the layer that turns a file on disk into "an ordered list of
documents and a way to read the things they point at". It deliberately
knows nothing about text, layout or progress.

The format, in the order this module walks it:

1. ``META-INF/container.xml`` names the package document (the OPF). It may
   name several *renditions*; the first is the one every reader uses.
2. The OPF carries three things we need — ``<metadata>`` (title, author,
   language), ``<manifest>`` (id -> href + media type for every file), and
   ``<spine>`` (the reading order, as ``idref``\\ s into the manifest).
3. The table of contents is either an EPUB 3 nav document (``properties``
   contains ``nav``) or an EPUB 2 NCX (the spine's ``toc`` attribute). Both
   are read, EPUB 3 first, because a hybrid book carries both and the nav
   document is the one its author maintained.

**Everything that leaves this module is bounded**, by what a read actually
delivers and never by the size the zip header claims, with the cap chosen
per kind of entry. Names are re-checked to be inside the archive. Why a
declared size is not usable — see ``docs/readers.md`` §4.1.
"""


import logging
import os
import posixpath
import zipfile
from urllib.parse import unquote

from . import xmlish
# Re-exported: this is where callers have always imported it from, and it
# is the layer that gives it meaning. See errors.py for why it is defined
# one module lower.
from .errors import EpubError

log = logging.getLogger("epub.archive")

#: Cap on one spine document. The largest single XHTML file in a normal book
#: is a few hundred KB; a book that ships one 32 MB chapter is either broken
#: or hostile, and either way we are not paginating it.
MAX_DOC_BYTES = 32 * 1024 * 1024

#: Cap on one image entry, before Pillow ever sees the bytes. Pillow has its
#: own pixel-count bomb guard (``MAX_IMAGE_PIXELS``), which this complements
#: rather than duplicates: that one catches 50000x50000, this one catches a
#: file that never stops arriving.
MAX_IMAGE_BYTES = 24 * 1024 * 1024

#: Cap on the package/container/nav documents. These are small by nature and
#: a large one is a signal in itself.
MAX_META_BYTES = 4 * 1024 * 1024

#: Media types the reader will try to draw. Anything else in the manifest is
#: still readable through :meth:`EpubArchive.read` — this list is only what
#: the content layer asks for by default.
IMAGE_TYPES = frozenset({
    "image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp",
})


class TooLarge(EpubError):
    """An entry blew its cap. Its own class because the message differs:
    nothing is wrong with the *book*, we simply refused to read it."""


class EpubArchive:
    """Bounded read access to the zip.

    **It holds no open file handle.** Every read opens the zip, takes what
    it came for and closes it again; only the name table is kept. That
    deletes a lifecycle rather than saving a descriptor, and it makes the
    type thread-safe by construction — two threads seeking one `ZipFile` is
    corruption rather than contention. Why the lifecycle was the expensive
    part, and what reopening costs — see ``docs/readers.md`` §4.5.
    """


    def __init__(self, path):
        self.path = path
        # A name->name map so a case-mismatched href (common in books built
        # on Windows and read on Linux) still resolves. First writer wins, so
        # an archive with genuinely both cases keeps the one it declared
        # first rather than flipping between reads.
        self._names = {}
        with self._open() as zf:
            for name in zf.namelist():
                self._names.setdefault(name.lower(), name)

    def _open(self):
        """The zip, for the duration of one read. Callers use ``with``."""
        try:
            return zipfile.ZipFile(self.path)
        except (OSError, zipfile.BadZipFile) as exc:
            raise EpubError("not a readable epub file: %s" % exc) from exc

    # -- entries ----------------------------------------------------------

    def exists(self, name):
        return self._resolve(name) is not None

    def _resolve(self, name):
        """Archive-relative path -> the exact entry name, or None."""
        name = unquote(name or "").split("#", 1)[0]
        # normpath collapses `a/../b`; a name that still escapes after that
        # is not addressing anything inside this archive.
        clean = posixpath.normpath(name).lstrip("/")
        if clean.startswith("../") or clean == "..":
            return None
        return self._names.get(clean.lower())

    def read(self, name, limit=MAX_DOC_BYTES):
        """Bytes of one entry, or raise.

        Reads ``limit + 1`` bytes and fails if it got them all: the declared
        size in the zip header is attacker-controlled, the delivered size is
        not.
        """
        entry = self._resolve(name)
        if entry is None:
            raise EpubError("no entry %r in %s" % (name, self.path))
        try:
            with self._open() as zf, zf.open(entry) as handle:
                data = handle.read(limit + 1)
        except (OSError, zipfile.BadZipFile, RuntimeError, EOFError) as exc:
            # RuntimeError is what zipfile raises for an encrypted entry,
            # which is a DRM'd book — a real case, and one whose message
            # should not read as corruption.
            raise EpubError("cannot read %r: %s" % (name, exc)) from exc
        if len(data) > limit:
            raise TooLarge("%r is larger than %d bytes" % (name, limit))
        return data

    def read_text(self, name, limit=MAX_DOC_BYTES):
        return xmlish.decode(self.read(name, limit))


class SpineItem:
    """One document in the reading order."""

    __slots__ = ("idref", "href", "media_type", "linear", "properties")

    def __init__(self, idref, href, media_type="", linear=True, properties=""):
        self.idref = idref
        self.href = href
        self.media_type = media_type
        #: ``linear="no"`` marks a document outside the reading flow — the
        #: cover page, pop-up footnote files. It stays in the spine (it is
        #: reachable by link) but it is **excluded from the locations index**,
        #: because epub.js excludes it and the whole point of that number is
        #: agreeing with the client that wrote it.
        self.linear = linear
        self.properties = properties

    def __repr__(self):
        return "<SpineItem %s %s%s>" % (self.idref, self.href,
                                        "" if self.linear else " (non-linear)")


class TocEntry:
    __slots__ = ("title", "href", "level", "spine_index")

    def __init__(self, title, href, level=0):
        self.title = title
        self.href = href
        self.level = level
        #: Filled in by :class:`EpubPackage` once the spine is known. None
        #: for an entry pointing at something not in the spine at all, which
        #: happens and must not crash the chapter menu.
        self.spine_index = None


class EpubPackage:
    """A parsed epub: metadata, spine, table of contents, and the archive.

    Construct with :func:`open_epub`. Cheap — it reads three small documents
    and no content.
    """

    def __init__(self, archive):
        self.archive = archive
        self.title = ""
        self.author = ""
        self.language = ""
        self.opf_dir = ""
        self.spine = []
        self.toc = []
        #: manifest id -> (href, media_type, properties)
        self.manifest = {}
        self.cover_href = None

    # -- reading ----------------------------------------------------------

    def doc_bytes(self, index):
        """Raw bytes of spine document ``index``."""
        return self.archive.read(self.spine[index].href)

    def doc_text(self, index):
        return xmlish.decode(self.doc_bytes(index))

    def resolve(self, base_href, href):
        """A link inside a document -> an archive path.

        ``base_href`` is the document the link was found in; a relative href
        is relative to *that*, not to the OPF, which is the mistake that puts
        every image in a `text/` subfolder one directory too high.
        """
        href = (href or "").split("#", 1)[0]
        if not href:
            return None
        if "://" in href:
            return None            # a remote resource; not ours to fetch
        if href.startswith("/"):
            return href.lstrip("/")
        base = posixpath.dirname(base_href or "")
        return posixpath.normpath(posixpath.join(base, unquote(href)))

    def spine_index_of(self, href):
        """Spine position of an archive path, or None. Fragments ignored."""
        if not href:
            return None
        target = posixpath.normpath(unquote(href).split("#", 1)[0]).lower()
        for i, item in enumerate(self.spine):
            if posixpath.normpath(item.href).lower() == target:
                return i
        return None


# -- parsing ---------------------------------------------------------------


def _container_opf(archive):
    """The package document's path, from ``META-INF/container.xml``."""
    try:
        root = xmlish.parse(archive.read("META-INF/container.xml",
                                         MAX_META_BYTES))
    except EpubError:
        root = None
    if root is not None:
        for rootfile in root.find_all("rootfile"):
            path = rootfile.get("full-path")
            if path and archive.exists(path):
                return path
    # No container, or one that points nowhere. Rather than give up, look for
    # a package document the ordinary way — a surprising number of files in
    # the wild are zips of an unpacked epub with the META-INF lost in the
    # round trip, and every one of them has exactly one .opf.
    for name in ("content.opf", "OEBPS/content.opf", "OPS/content.opf"):
        if archive.exists(name):
            log.info("%s has no usable container.xml; using %s",
                     archive.path, name)
            return name
    for name in archive._names.values():
        if name.lower().endswith(".opf"):
            log.info("%s has no usable container.xml; using %s",
                     archive.path, name)
            return name
    raise EpubError("no package document found")


def _text_of(node, tag):
    found = node.find(tag) if node is not None else None
    return (found.text().strip() if found is not None else "")


def _parse_opf(package, opf_path):
    archive = package.archive
    root = xmlish.parse(archive.read(opf_path, MAX_META_BYTES))
    package.opf_dir = posixpath.dirname(opf_path)

    metadata = root.find("metadata")
    if metadata is not None:
        package.title = _text_of(metadata, "title")
        package.language = _text_of(metadata, "language")
        package.author = _text_of(metadata, "creator")

    manifest = root.find("manifest")
    if manifest is not None:
        for item in manifest.find_all("item"):
            iid = item.get("id")
            href = item.get("href")
            if not iid or not href:
                continue
            full = posixpath.normpath(
                posixpath.join(package.opf_dir, unquote(href)))
            package.manifest[iid] = (full, (item.get("media-type") or ""),
                                     (item.get("properties") or ""))

    spine = root.find("spine")
    if spine is None:
        raise EpubError("package document has no spine")
    for ref in spine.find_all("itemref"):
        idref = ref.get("idref")
        entry = package.manifest.get(idref)
        if entry is None:
            continue
        href, media_type, properties = entry
        if not archive.exists(href):
            # A manifest entry naming a file that is not in the zip. Skipping
            # it is right: the alternative is a chapter that raises when the
            # reader reaches it, which looks like a crash rather than a
            # damaged book.
            log.info("spine item %s -> %s is missing from the archive",
                     idref, href)
            continue
        package.spine.append(SpineItem(
            idref, href, media_type,
            (ref.get("linear") or "yes").lower() != "no", properties))
    if not package.spine:
        raise EpubError("package document has an empty spine")

    package.cover_href = _cover_href(package, root, spine)
    _parse_toc(package, root, spine)


def _cover_href(package, root, spine):
    # Two spellings, both current. EPUB 3 marks the manifest item
    # `properties="cover-image"`; EPUB 2 points at it from a metadata
    # `<meta name="cover" content="<id>">`.
    for href, media_type, properties in package.manifest.values():
        if "cover-image" in properties:
            return href
    metadata = root.find("metadata")
    for meta in (metadata.find_all("meta") if metadata is not None else []):
        if (meta.get("name") or "").lower() == "cover":
            entry = package.manifest.get(meta.get("content"))
            if entry:
                return entry[0]
    return None


def _parse_toc(package, root, spine):
    """Fill ``package.toc`` from the nav document or the NCX."""
    nav_href = None
    for href, media_type, properties in package.manifest.values():
        if "nav" in (properties or "").split():
            nav_href = href
            break
    entries = []
    if nav_href:
        try:
            entries = _parse_nav(package, nav_href)
        except EpubError:
            log.debug("nav document unreadable", exc_info=True)
    if not entries:
        ncx = package.manifest.get(spine.get("toc") or "")
        if ncx is None:
            for href, media_type, _p in package.manifest.values():
                if media_type == "application/x-dtbncx+xml":
                    ncx = (href, media_type, "")
                    break
        if ncx is not None:
            try:
                entries = _parse_ncx(package, ncx[0])
            except EpubError:
                log.debug("ncx unreadable", exc_info=True)
    for entry in entries:
        entry.spine_index = package.spine_index_of(entry.href)
    package.toc = entries


def _parse_nav(package, nav_href):
    root = xmlish.parse(package.archive.read(nav_href, MAX_META_BYTES))
    target = None
    for nav in root.find_all("nav"):
        # `epub:type="toc"` is the marker; the local-name matching this
        # module does means the attribute arrives as plain `type`.
        if (nav.get("type") or "").lower() == "toc":
            target = nav
            break
    if target is None:
        return []
    return _walk_list(package, target, nav_href)


def _walk_list(package, node, base_href, level=0):
    """An EPUB 3 nav is nested ``<ol><li><a>``; depth is the TOC level."""
    out = []
    for item in node.children:
        if not isinstance(item, xmlish.Node):
            continue
        if item.tag == "li":
            link = item.find("a")
            if link is not None:
                title = " ".join(link.text().split())
                href = package.resolve(base_href, link.get("href"))
                if title and href:
                    out.append(TocEntry(title, href, level))
            for sub in item.children:
                if isinstance(sub, xmlish.Node) and sub.tag in ("ol", "ul"):
                    out += _walk_list(package, sub, base_href, level + 1)
        elif item.tag in ("ol", "ul"):
            out += _walk_list(package, item, base_href, level)
    return out


def _parse_ncx(package, ncx_href):
    root = xmlish.parse(package.archive.read(ncx_href, MAX_META_BYTES))
    nav_map = root.find("navmap")
    if nav_map is None:
        return []
    return _walk_navpoints(package, nav_map, ncx_href)


def _walk_navpoints(package, node, base_href, level=0):
    out = []
    for point in node.children:
        if not isinstance(point, xmlish.Node) or point.tag != "navpoint":
            continue
        label = point.find("navlabel")
        content = point.find("content")
        title = " ".join(label.text().split()) if label is not None else ""
        href = package.resolve(base_href,
                               content.get("src") if content is not None
                               else None)
        if title and href:
            out.append(TocEntry(title, href, level))
        out += _walk_navpoints(package, point, base_href, level + 1)
    return out


def open_epub(path):
    """Open an epub file. Raises :class:`EpubError` with a reason."""
    if not os.path.exists(path):
        raise EpubError("no such file: %s" % path)
    archive = EpubArchive(path)
    package = EpubPackage(archive)
    _parse_opf(package, _container_opf(archive))
    return package
