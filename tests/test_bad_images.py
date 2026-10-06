"""Every bad or unusual image through every decode path.

`tests/fixtures/bad_images/` is a corpus of ~30 small files: truncated,
empty, HTML and JSON served as a JPEG, header bombs, odd modes, animation, an
EXIF-rotated photo, trickplay tiles of the wrong size. MANIFEST holds each
one's verdict -- "ok", "fail", or "ok-degraded" for one that decodes to
something wrong without any error Pillow reports -- so a Pillow upgrade that
changes a verdict shows up as a diff here rather than as a silent pass.

Each sink has a contract, and that is what is asserted, per fixture:
* the cache gate (`imageutil.decodes`) agrees with the verdict, and the
  offline art writer keeps exactly what it accepts;
* the thumbnail store returns an image with both sides >= 1, or raises;
* the epub painter returns an image or None, and the header readers a
  positive size or None -- never an exception;
* trickplay decoding writes whole frames or raises BadTile;
* whatever decoded composites into a strip without raising.
None of them may hang: each runs under a timeout.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import io
import os
import sys
import tempfile
import threading
import unittest
import warnings

sys.argv = [sys.argv[0]]

from PIL import Image  # noqa: E402

from jellyfin_mpv_shim import bifdecode, imageutil  # noqa: E402
from jellyfin_mpv_shim.epub import paint  # noqa: E402
from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.pages.comic import _picture_size  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.thumbnails import ThumbnailStore  # noqa: E402
from jellyfin_mpv_shim.sync import manager  # noqa: E402

HERE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "fixtures", "bad_images")

MANIFEST = {
    "anim.gif": "ok",               # frame 0
    "anim.webp": "ok",
    "cmyk.jpg": "ok",
    "corrupt_mid.jpg": "fail",
    "empty.jpg": "fail",
    "exif6.jpg": "ok",              # upright: imageutil.decode transposes
    "f32.tif": "ok",
    "hdr_0x0.png": "fail",
    "hdr_bomb_20000.png": "fail",   # Pillow's own bomb error
    "hdr_warn_12000.png": "fail",   # imageutil.TooLarge, before any decode
    "html.jpg": "fail",             # a forward-auth login page
    "i16.png": "ok",
    "i32.tif": "ok",
    "json.jpg": "fail",
    "la.png": "ok",
    "ok.avif": "ok",
    "ok.jpg": "ok",
    "onebit.png": "ok",
    "p_trns.png": "ok",
    "partial_then_full.jpg": "fail",  # the apiclient retry-append shape
    "rgba.png": "ok",
    "tall_1x65535.png": "ok",
    "tile_cmyk.jpg": "ok",
    "tile_off_by_2.jpg": "ok",      # an image; the WRONG SIZE for a tile
    "tile_ok.jpg": "ok",
    "tile_short_rows.jpg": "ok",    # likewise
    "tile_trunc.jpg": "fail",
    "trunc_half.gif": "fail",
    "trunc_half.jpg": "fail",
    "trunc_half.png": "fail",
    "trunc_half.webp": "ok-degraded",  # grey bottom half, no error raised
    "trunc_header.jpg": "fail",
    "wide_65535x1.png": "ok",
}

#: The trickplay manifest the tile_* fixtures are cut for: 10x10 of 32x18.
TILE = (32, 18, 10, 10)


def bounded(fn, timeout=10.0):
    """fn()'s result or exception, failing the test if it does not return."""
    box = {}

    def run():
        try:
            box["value"] = fn()
        except BaseException as exc:    # reported, not swallowed
            box["error"] = exc
    worker = threading.Thread(target=run, daemon=True)
    worker.start()
    worker.join(timeout)
    if worker.is_alive():
        raise AssertionError("did not return within %ss" % timeout)
    if "error" in box:
        raise box["error"]
    return box.get("value")


class BadImageCorpusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.thumbs = ThumbnailStore(os.path.join(cls.tmp, "cache"), workers=1)
        cls.strips = StripStore(mem_store=MemoryStore())

    @classmethod
    def tearDownClass(cls):
        import shutil
        cls.thumbs.shutdown()
        cls.strips.clear()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # The header bombs are the point of two fixtures; Pillow warns on
        # opening them, which is noise here.
        caught = warnings.catch_warnings()
        caught.__enter__()
        self.addCleanup(caught.__exit__, None, None, None)
        warnings.simplefilter("ignore", Image.DecompressionBombWarning)

    def fixtures(self):
        for name in sorted(MANIFEST):
            path = os.path.join(HERE, name)
            with open(path, "rb") as fh:
                yield name, path, fh.read(), MANIFEST[name]

    def test_the_manifest_covers_the_corpus(self):
        self.assertEqual(sorted(os.listdir(HERE)), sorted(MANIFEST))

    def test_the_cache_gate_and_the_art_writer_agree_with_the_verdict(self):
        for name, _path, data, verdict in self.fixtures():
            with self.subTest(name):
                self.assertEqual(bounded(lambda: imageutil.decodes(data)),
                                 verdict != "fail")
                target = os.path.join(self.tmp, "art-" + name)
                try:
                    manager._write_art(target, data)
                except ValueError:
                    pass
                self.assertEqual(os.path.exists(target), verdict != "fail")

    def test_the_thumbnail_store(self):
        for name, path, _data, verdict in self.fixtures():
            for cover in (False, True):
                with self.subTest(name, cover=cover):
                    try:
                        image = bounded(lambda: self.thumbs._load_image(
                            "k", path, (200, 300), cover))
                    except AssertionError:
                        raise                   # a hang, from bounded()
                    except Exception:
                        self.assertEqual(verdict, "fail", "raised on an ok "
                                         "image")
                        continue
                    self.assertNotEqual(verdict, "fail", "accepted a bad one")
                    self.assertGreaterEqual(min(image.size), 1)
                    # ...and it composites.
                    self.strips.bitmap(("corpus", name, cover), image)

    def test_the_epub_painter_never_raises(self):
        for name, path, data, verdict in self.fixtures():
            with self.subTest(name):
                picture = bounded(lambda: paint.decode_image(data))
                self.assertEqual(picture is None, verdict == "fail")
                for size in (bounded(lambda: paint.image_size(data)),
                             bounded(lambda: _picture_size(path))):
                    if size is not None:
                        self.assertGreater(min(size), 0)

    def test_trickplay_writes_whole_frames_or_raises_bad_tile(self):
        w, h, tw, th = TILE
        for name, _path, data, _verdict in self.fixtures():
            with self.subTest(name):
                out = io.BytesIO()
                try:
                    n = bounded(lambda: bifdecode.decompress_tiles(
                        w, h, tw, th, tw * th, [data], out))
                except bifdecode.BadTile:
                    continue
                self.assertIn(name, ("tile_ok.jpg", "tile_cmyk.jpg"),
                              "accepted a tile that is not 320x180")
                self.assertEqual(len(out.getvalue()), n * w * h * 4)


if __name__ == "__main__":
    unittest.main()
