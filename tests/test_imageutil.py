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
    """These guarded a dependency on the old optional `display_mirror`
    module. That module is gone — its screen is a browser route now
    (mpvtk_browser/cast.py) — so the original assertions would pass
    vacuously. The invariant underneath them is still real and still worth
    holding: importing the browser must not drag in heavyweight or
    optional third-party packages at module scope.
    """

    # PIL is required for the mpvtk browser (mpv_shim probes it before
    # loading the UI), but the package still defers it, so importing a view
    # to read its ROUTES table does not pay for Pillow — and a build without
    # it fails at the probe with a clear message rather than at a random
    # import. requests likewise.
    DEFERRED = {"PIL", "requests", "numpy"}

    def test_no_browser_module_imports_them_at_module_scope(self):
        from jellyfin_mpv_shim import mpvtk_browser
        pkg = os.path.dirname(inspect.getfile(mpvtk_browser))
        offenders = []
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name)) as fh:
                tree = ast.parse(fh.read())
            for node in tree.body:      # module scope only, not ast.walk
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                for mod in mods:
                    if mod.split(".")[0] in self.DEFERRED:
                        offenders.append("%s:%d %s"
                                         % (name, node.lineno, mod))
        # thumbnails.py is the documented exception: it is only imported
        # from ui.login_servers, after the Pillow probe.
        offenders = [o for o in offenders if not o.startswith("thumbnails.py")]
        self.assertEqual(offenders, [],
                         "module-scope optional imports: %s" % offenders)

    def test_the_banner_composites_from_the_shared_helpers(self):
        """The reason imageutil exists: the banner is composited by the
        browser using helpers that no longer live in a feature module."""
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

        out = MpvtkBrowser._compose_banner(
            Image.new("RGBA", (400, 200), (10, 20, 30, 255)),
            (300, 120), title="A Title", meta="2020",
            context="The Show")
        self.assertEqual(out.size, (300, 120))

    def test_the_cast_screen_uses_the_same_helpers(self):
        """Both composite paths share imageutil rather than each carrying a
        private copy — which is what let the mirror become a route without
        duplicating any of it."""
        import jellyfin_mpv_shim.mpvtk_browser.cast as cast
        for name in ("apply_dark_gradient", "scale_to_cover", "pil_font"):
            self.assertTrue(hasattr(cast, name), name)


if __name__ == "__main__":
    unittest.main()
