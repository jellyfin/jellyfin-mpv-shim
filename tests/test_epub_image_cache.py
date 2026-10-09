"""The epub image cache is bounded by memory, not only by count.

It keeps decoded ORIGINALS -- up to 40 M px each -- and was bounded by count
alone, so 24 high-resolution plates (an art book, a photo book) held ~1.7 GB.
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
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.epub import book as book_module  # noqa: E402
from jellyfin_mpv_shim.epub.book import EpubDocument  # noqa: E402
from tests._epub_fixtures import build_epub, png_bytes  # noqa: E402

SIDE = 300
ONE = SIDE * SIDE * 3          # one decoded RGB plate


class ImageCacheBudgetTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        plates = {"p%d.png" % i: png_bytes(SIDE, SIDE, (i * 20, 0, 0))
                  for i in range(10)}
        path = os.path.join(tmp.name, "plates.epub")
        build_epub(path, ["<p>plates</p>"], extra=plates)
        self.doc = EpubDocument(path)
        self.srcs = ["OEBPS/" + name for name in sorted(plates)]
        patcher = mock.patch.object(book_module, "IMAGE_CACHE_BYTES",
                                    3 * ONE, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def held(self):
        return sum(p.width * p.height * 3
                   for p in self.doc._images.values() if p is not None)

    def test_paging_through_plates_stays_under_the_budget(self):
        for src in self.srcs:
            self.assertIsNotNone(self.doc._load_image(src), src)
            self.assertLessEqual(self.held(), 3 * ONE,
                                 "holding %d plates" % len(self.doc._images))
            self.assertIn(src, self.doc._images, "dropped the one drawn")

    def test_the_plate_on_screen_survives_its_neighbours_loading(self):
        """Least recently DRAWN goes first: a page redraws its plate on every
        paint, so it must not age out while it is being looked at."""
        on_screen = self.srcs[0]
        for src in self.srcs[1:6]:
            self.doc._load_image(on_screen)       # the page repaints
            self.doc._load_image(src)
        self.assertIn(on_screen, self.doc._images)


if __name__ == "__main__":
    unittest.main()
