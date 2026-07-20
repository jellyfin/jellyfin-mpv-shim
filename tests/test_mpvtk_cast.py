"""The cast screen: metadata build, backdrop+text compositing into a
full-window bitmap, and the route renderer.

This was display_mirror.DisplayMirror, a second UI that owned the mpv window
itself; it is a route on the browser now (mpvtk_browser/cast.py). The
compositing is unchanged and so are these tests — that is the point: the
screen has to look and behave exactly as it did, only hosted differently.

Runs headless (PIL only, no network — the backdrop url is omitted so the
solid-canvas path is used).
"""

import unittest

import sys

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk.rawimage import cache_dir  # noqa: E402
from jellyfin_mpv_shim.mpvtk.widgets import Image as ImageNode  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.cast import _wrap  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore  # noqa: E402

from tests.test_mpvtk_browser_shell import FakeSource, _SyncPool  # noqa: E402


ITEM_DATA = {"title": "The Movie", "overview": "A long overview. " * 20,
             "misc": "2020    118min", "rating": "★ 8.1",
             "backdrop_url": None}


class TestCastScreen(unittest.TestCase):
    def _cast(self):
        b = MpvtkBrowser(
            app=None, source=FakeSource(),
            strips=StripStore(cache_dir=cache_dir("mpvtk-cast-test-")))
        b._pool = _SyncPool()
        b._cast_size = (800, 600)
        return b   # app is None -> invalidate() is a guarded no-op

    def test_build_item_data(self):
        d = MpvtkBrowser._build_item_data(
            {"Name": "Movie", "Overview": "o", "Type": "Movie",
             "ProductionYear": 2020}, "http://srv")
        self.assertEqual(d["title"], "Movie")
        self.assertEqual(d["overview"], "o")
        self.assertIn("2020", d["misc"])

    def test_composite_bakes_full_window_bitmap(self):
        m = self._cast()
        m._composite(ITEM_DATA, (800, 600))
        self.assertIsNotNone(m._cast_entry)
        self.assertEqual(m._cast_entry["iw"], 800)
        self.assertEqual(m._cast_entry["ih"], 600)

    def test_the_route_renders_the_baked_bitmap(self):
        m = self._cast()
        m._composite(ITEM_DATA, (800, 600))
        node = m._render_cast({"kind": "cast"}, (800, 600))
        self.assertIsInstance(node, ImageNode)
        self.assertEqual(node.w, 800)
        self.assertEqual(node.h, 600)

    def test_nothing_baked_yet_is_an_empty_scene(self):
        """Rather than a flash of whatever the previous page left up."""
        m = self._cast()
        node = m._render_cast({"kind": "cast"}, (800, 600))
        self.assertEqual(getattr(node, "children", None), [])

    def test_backdrop_is_fetched_once_per_data_change(self):
        """A window resize must re-composite from the cached image, not go
        back to the network (and, when idle, not re-roll the random
        backdrop)."""
        m = self._cast()
        calls = []

        def fake_fetch(url, timeout=10):
            calls.append(url)
            return None
        import jellyfin_mpv_shim.mpvtk_browser.cast as dm
        real, dm._fetch_image = dm._fetch_image, fake_fetch
        try:
            data = dict(ITEM_DATA, backdrop_url="http://srv/bd.jpg")
            m._cast = data
            m._composite(data, (800, 600))
            m._composite(data, (1024, 768))
            m._composite(data, (1280, 720))
            self.assertEqual(len(calls), 1)
            m._set_cast_data(dict(data, title="Other"))   # new item -> refetch
            m._composite(m._cast, (800, 600))
            self.assertEqual(len(calls), 2)
        finally:
            dm._fetch_image = real

    def test_bitmap_key_is_content_addressed(self):
        """A monotonic key was a guaranteed cache miss, so each resize tick
        retained another full-window buffer."""
        m = self._cast()
        m._composite(ITEM_DATA, (800, 600))
        first = m._cast_entry["src"]
        m._composite(ITEM_DATA, (800, 600))
        self.assertEqual(m._cast_entry["src"], first)
        self.assertEqual(len(m.strips._cache), 1)

    def test_wrap_breaks_long_text(self):
        from PIL import Image as PILImage, ImageDraw
        from jellyfin_mpv_shim.imageutil import pil_font
        draw = ImageDraw.Draw(PILImage.new("RGBA", (10, 10)))
        lines = _wrap(draw, "word " * 100, pil_font(24), 300)
        self.assertGreater(len(lines), 1)


if __name__ == "__main__":
    unittest.main()
