"""Real epub files, written to a temp directory.

Real ones, not fakes: the thing under test is a zip parser, a markup parser
and a character counter, and every one of those is exactly where a stand-in
would agree with the code instead of with the format. The cost is a
tempfile per test, which is microseconds.

``build_epub`` takes the pieces a test cares about and fills in the rest of
the container, so a test that is about the locations index does not have to
carry an OPF.
"""

import os
import zipfile

CONTAINER = """<?xml version="1.0"?>
<container version="1.0"
  xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf"
      media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>"""


def xhtml(body, head=""):
    return ("""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml"><head>%s</head>
<body>%s</body></html>""" % (head, body))


def build_epub(path, chapters, title="A Book", author="An Author",
               css=None, toc=(), extra=None, linear=None):
    """Write an epub at ``path``.

    ``chapters`` is a list of XHTML *body* strings (or ``(name, markup)``
    pairs when a test needs to control the filenames). ``toc`` is a list of
    ``(label, href)``; ``extra`` is a dict of additional archive entries
    (images, stylesheets); ``linear`` is a set of chapter indexes to mark
    ``linear="no"``.
    """
    linear = linear or set()
    docs = []
    for i, chapter in enumerate(chapters):
        if isinstance(chapter, tuple):
            name, markup = chapter
        else:
            name, markup = "ch%d.xhtml" % (i + 1), chapter
        head = ('<link rel="stylesheet" type="text/css" href="style.css"/>'
                if css else "")
        docs.append((name, markup if markup.lstrip().startswith("<?xml")
                     else xhtml(markup, head)))

    manifest = "\n".join(
        '<item id="c%d" href="%s" media-type="application/xhtml+xml"/>'
        % (i, name) for i, (name, _m) in enumerate(docs))
    spine = "\n".join(
        '<itemref idref="c%d"%s/>'
        % (i, ' linear="no"' if i in linear else "")
        for i in range(len(docs)))
    if css:
        manifest += '\n<item id="css" href="style.css" media-type="text/css"/>'
    if toc:
        manifest += ('\n<item id="nav" href="nav.xhtml" properties="nav" '
                     'media-type="application/xhtml+xml"/>')
    for name in (extra or {}):
        manifest += ('\n<item id="x%s" href="%s" media-type="%s"/>'
                     % (abs(hash(name)) % 10000, name, _media_type(name)))

    opf = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"
         unique-identifier="id">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>%s</dc:title>
    <dc:creator>%s</dc:creator>
    <dc:language>en</dc:language>
    <dc:identifier id="id">urn:uuid:test</dc:identifier>
  </metadata>
  <manifest>%s</manifest>
  <spine>%s</spine>
</package>""" % (title, author, manifest, spine)

    with zipfile.ZipFile(path, "w") as archive:
        # Stored, uncompressed, first — as the specification requires.
        archive.writestr(
            zipfile.ZipInfo("mimetype"), "application/epub+zip",
            compress_type=zipfile.ZIP_STORED)
        archive.writestr("META-INF/container.xml", CONTAINER)
        archive.writestr("OEBPS/content.opf", opf)
        for name, markup in docs:
            archive.writestr("OEBPS/" + name, markup)
        if css:
            archive.writestr("OEBPS/style.css", css)
        if toc:
            links = "".join('<li><a href="%s">%s</a></li>' % (href, label)
                            for label, href in toc)
            archive.writestr("OEBPS/nav.xhtml", xhtml(
                '<nav epub:type="toc"><ol>%s</ol></nav>' % links))
        for name, data in (extra or {}).items():
            if isinstance(data, str):
                data = data.encode("utf-8")
            archive.writestr("OEBPS/" + name, data)
    return path


def _media_type(name):
    ext = os.path.splitext(name)[1].lower()
    return {".png": "image/png", ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg", ".gif": "image/gif",
            ".css": "text/css"}.get(ext, "application/octet-stream")


def png_bytes(width=40, height=30, color=(200, 60, 60)):
    """A real PNG, so the image path decodes something Pillow accepts."""
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), color).save(buffer, "PNG")
    return buffer.getvalue()


def paragraphs(count, words=40, word="word"):
    """``count`` paragraphs of roughly ``words`` words each."""
    return "".join("<p>%s</p>" % " ".join([word] * words)
                   for _i in range(count))
