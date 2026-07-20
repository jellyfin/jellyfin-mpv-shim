"""The shared Pillow helpers, and the coupling they were extracted to break.

The browser's detail banner used to import _apply_dark_gradient, _pil_font
and _scale_to_cover out of display_mirror — an optional, Pillow-gated feature
module that the Tk cleanup is expected to churn. A core view silently
depended on it: nothing failed until display_mirror moved.
"""

import ast
import inspect
import os
import sys
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from PIL import Image  # noqa: E402

from jellyfin_mpv_shim import imageutil  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_scale_to_cover_fills_the_box_exactly(self):
        for src, box in (((100, 100), (200, 50)), ((40, 300), (60, 60)),
                         ((640, 480), (640, 480))):
            with self.subTest(src=src, box=box):
                out = imageutil.scale_to_cover(
                    Image.new("RGBA", src, (255, 0, 0, 255)), *box)
                self.assertEqual(out.size, box)

    def test_scale_to_cover_crops_rather_than_squashing(self):
        """A 2:1 source into a 1:1 box keeps the aspect and loses the sides,
        so the centre column stays the colour it started."""
        src = Image.new("RGBA", (200, 100), (0, 0, 255, 255))
        out = imageutil.scale_to_cover(src, 100, 100)
        self.assertEqual(out.getpixel((50, 50)), (0, 0, 255, 255))

    def test_the_gradient_darkens_the_bottom_and_spares_the_top(self):
        src = Image.new("RGBA", (50, 100), (255, 255, 255, 255))
        out = imageutil.apply_dark_gradient(src, height_fraction=0.5,
                                            max_alpha=255)
        self.assertEqual(out.getpixel((25, 5))[:3], (255, 255, 255))
        self.assertLess(out.getpixel((25, 99))[0], 40)

    def test_pil_font_picks_a_face_that_covers_the_script(self):
        """Pillow has no font fallback, so the wrong face renders tofu."""
        latin = imageutil.pil_font(24, text="Hello")
        cjk = imageutil.pil_font(24, text="日本語")
        self.assertIsNotNone(latin)
        self.assertIsNotNone(cjk)


class TestTheCouplingIsGone(unittest.TestCase):
    def test_the_browser_does_not_import_display_mirror(self):
        from jellyfin_mpv_shim import mpvtk_browser
        pkg = os.path.dirname(inspect.getfile(mpvtk_browser))
        offenders = []
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name)) as fh:
                tree = ast.parse(fh.read())
            for node in ast.walk(tree):
                mod = getattr(node, "module", None)
                if isinstance(node, ast.ImportFrom) and mod == "display_mirror":
                    offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(offenders, [],
                         "a browser view depends on the optional mirror")

    def test_the_banner_composites_without_display_mirror(self):
        """The proof that matters: block the module outright and draw one."""
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        blocked = {}
        for key in [k for k in sys.modules
                    if k.endswith("display_mirror")]:
            blocked[key] = sys.modules.pop(key)
        sys.modules["jellyfin_mpv_shim.display_mirror"] = None  # ImportError
        try:
            out = MpvtkBrowser._compose_banner(
                Image.new("RGBA", (400, 200), (10, 20, 30, 255)),
                (300, 120), title="A Title", meta="2020",
                context="The Show")
            self.assertEqual(out.size, (300, 120))
        finally:
            del sys.modules["jellyfin_mpv_shim.display_mirror"]
            sys.modules.update(blocked)


if __name__ == "__main__":
    unittest.main()
