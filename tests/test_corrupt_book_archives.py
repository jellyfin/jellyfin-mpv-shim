"""A corrupt deflate stream in a comic or an epub is the reader's own error.

Both readers caught BadZipFile and not the decompressors' errors, so a
damaged page reached the error banner as a raw, untranslated ``zlib.error``
(the comic) or escaped the epub layer's EpubError contract.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import sys
import tempfile
import unittest
import zipfile

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import comic  # noqa: E402
from jellyfin_mpv_shim.epub import archive  # noqa: E402
from tests._epub_fixtures import png_bytes  # noqa: E402


def corrupt_zip(path, name, data):
    """A zip whose one deflated entry has its stream scrambled, with the
    directory intact -- so it opens, lists, and fails only when read."""
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(name, data)
        size = zf.getinfo(name).compress_size
    raw = bytearray(open(path, "rb").read())
    start = 30 + len(name)                  # past the local file header
    for i in range(start + 2, start + size - 2):
        raw[i] ^= 0xA5
    with open(path, "wb") as fh:
        fh.write(bytes(raw))


class CorruptDeflateTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.page = png_bytes(64, 64) + bytes(range(256)) * 400

    def test_a_comic_page_raises_comic_error(self):
        path = os.path.join(self.dir, "corrupt.cbz")
        corrupt_zip(path, "001.png", self.page)
        book = comic.ComicArchive(path)
        with self.assertRaises(comic.ComicError):
            book._read(0, "001.png")

    def test_an_epub_entry_raises_epub_error(self):
        path = os.path.join(self.dir, "corrupt.epub")
        corrupt_zip(path, "OEBPS/p.png", self.page)
        with self.assertRaises(archive.EpubError):
            archive.EpubArchive(path).read("OEBPS/p.png")


if __name__ == "__main__":
    unittest.main()
