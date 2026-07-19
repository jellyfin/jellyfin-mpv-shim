"""Unit tests for the mpvtk display mirror (display_mirror.DisplayMirror):
metadata build, backdrop+text compositing into a full-window bitmap, and
the visible/hidden build() branch. Headless (PIL only, no network — backdrop
url omitted so the solid-canvas path is used).
"""

import unittest

from jellyfin_mpv_shim.display_mirror import DisplayMirror, _wrap
from jellyfin_mpv_shim.mpvtk.rawimage import cache_dir
from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore


ITEM_DATA = {"title": "The Movie", "overview": "A long overview. " * 20,
             "misc": "2020    118min", "rating": "★ 8.1",
             "backdrop_url": None}


class TestMirror(unittest.TestCase):
    def _mirror(self):
        m = DisplayMirror()
        m._store = StripStore(cache_dir=cache_dir("mpvtk-mirror-test-"))
        m._size = (800, 600)
        return m   # _app stays None -> invalidate() is a guarded no-op

    def test_build_item_data(self):
        d = DisplayMirror._build_item_data(
            {"Name": "Movie", "Overview": "o", "Type": "Movie",
             "ProductionYear": 2020}, "http://srv")
        self.assertEqual(d["title"], "Movie")
        self.assertEqual(d["overview"], "o")
        self.assertIn("2020", d["misc"])

    def test_composite_bakes_full_window_bitmap(self):
        m = self._mirror()
        m._composite(ITEM_DATA, (800, 600))
        self.assertIsNotNone(m._entry)
        self.assertEqual(m._entry["iw"], 800)
        self.assertEqual(m._entry["ih"], 600)

    def test_build_visible_returns_image(self):
        m = self._mirror()
        m._composite(ITEM_DATA, (800, 600))
        node = m._build((800, 600))
        self.assertIsInstance(node, ImageNode)
        self.assertEqual(node.w, 800)
        self.assertEqual(node.h, 600)

    def test_hidden_returns_empty_scene(self):
        m = self._mirror()
        m._composite(ITEM_DATA, (800, 600))
        m.hide()
        node = m._build((800, 600))
        self.assertEqual(node.children, [])   # empty Column -> clears overlays

    def test_wrap_breaks_long_text(self):
        from PIL import Image as PILImage, ImageDraw
        from jellyfin_mpv_shim.display_mirror import _pil_font
        draw = ImageDraw.Draw(PILImage.new("RGBA", (10, 10)))
        lines = _wrap(draw, "word " * 100, _pil_font(24), 300)
        self.assertGreater(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
