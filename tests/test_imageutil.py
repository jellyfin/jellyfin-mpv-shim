"""The shared Pillow helpers, and the coupling they were extracted to break.

The browser's detail banner used to import _apply_dark_gradient, _pil_font
and _scale_to_cover out of display_mirror — an optional, Pillow-gated feature
module that the Tk cleanup is expected to churn. A core view silently
depended on it: nothing failed until display_mirror moved.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import inspect
import os
import sys
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from PIL import Image  # noqa: E402

from jellyfin_mpv_shim import imageutil  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import components  # noqa: E402


class TestHelpers(unittest.TestCase):
    def test_scale_to_cover_fills_the_box_exactly(self):
        for src, box in (((100, 100), (200, 50)), ((40, 300), (60, 60)),
                         ((640, 480), (640, 480))):
            with self.subTest(src=src, box=box):
                out = imageutil.scale_to_cover(
                    Image.new("RGBA", src, (255, 0, 0, 255)), *box)
                self.assertEqual(out.size, box)

    def test_scale_to_cover_crops_rather_than_squashing(self):
        """A 2:1 source into a 1:1 box keeps the aspect and loses the SIDES.

        Read off a horizontal ramp, not a flat colour. This asserted that
        the centre pixel of a solid blue source came back blue, which a
        squash satisfies just as well as a crop -- it survived a mutation
        that deleted the cover crop entirely and handed back the whole
        picture stretched into the box.
        """
        src = Image.new("RGBA", (200, 100), (0, 0, 0, 255))
        for x in range(200):
            for y in range(100):
                src.putpixel((x, y), (0, 0, x * 255 // 200, 255))
        out = imageutil.scale_to_cover(src, 100, 100)
        # The kept band is source columns 50..150, i.e. ramp values 64..191.
        # Squashed, the same two pixels would read ~0 and ~254.
        self.assertAlmostEqual(out.getpixel((0, 50))[2], 64, delta=4)
        self.assertAlmostEqual(out.getpixel((99, 50))[2], 190, delta=4)

    def test_the_crop_happens_in_the_source_and_not_after_the_scale(self):
        """Nothing bigger than the box asked for is ever materialised.

        The obvious spelling of cover -- scale the whole picture up, then
        crop -- resamples every pixel it is about to discard: a 1920x1080
        backdrop covering a 6390x412 full-bleed header went through
        6390x3596, which measured 194 ms and +204 MB of peak RSS *on the
        loop thread*, once per pixel of a drag-resize. That is invisible to
        every other assertion here, because the answer is the same picture.
        """
        src = Image.new("RGBA", (1920, 1080), (10, 20, 30, 255))
        seen = []
        real = Image.Image.resize

        def spy(self, size, *a, **kw):
            seen.append(size)
            return real(self, size, *a, **kw)

        Image.Image.resize = spy
        try:
            out = imageutil.scale_to_cover(src, 6390, 412,
                                           gravity_y=imageutil.TOP_HEAVY)
        finally:
            Image.Image.resize = real
        self.assertEqual(out.size, (6390, 412))
        self.assertTrue(seen, "no resample happened at all")
        for size in seen:
            self.assertLessEqual(
                size[0] * size[1], 6390 * 412,
                "resampled %r to produce a 6390x412 banner" % (size,))

    def test_the_default_crop_is_still_centred(self):
        """A 1:2 source into a 1:1 box drops equal bands off both ends."""
        src = Image.new("RGBA", (100, 400), (0, 0, 0, 255))
        for y in range(400):                    # a vertical ramp to read off
            for x in range(100):
                src.putpixel((x, y), (y // 2, y // 2, y // 2, 255))
        out = imageutil.scale_to_cover(src, 100, 100)
        # The kept band is rows 150..250 of 400, whose ramp values are 75..125
        self.assertAlmostEqual(out.getpixel((50, 50))[0], 100, delta=2)

    def test_gravity_moves_the_crop_up_the_source(self):
        """TOP_HEAVY keeps the band centred a third of the way down, which
        on a wide banner is where the subject of a piece of key art is
        rather than where its middle happens to fall [iw]."""
        src = Image.new("RGBA", (100, 400), (0, 0, 0, 255))
        for y in range(400):
            for x in range(100):
                src.putpixel((x, y), (y // 2, y // 2, y // 2, 255))
        out = imageutil.scale_to_cover(src, 100, 100,
                                       gravity_y=imageutil.TOP_HEAVY)
        # Centred on row 133 of 400, i.e. a ramp value of ~66.
        self.assertAlmostEqual(out.getpixel((50, 50))[0], 66, delta=3)

    def test_gravity_is_clamped_to_the_picture(self):
        """A box too tall for the bias to be honoured goes as far that way
        as it can. Nothing may hang off the edge -- a crop box outside the
        image comes back the wrong SIZE, which is the one thing every
        caller here depends on."""
        src = Image.new("RGBA", (400, 210), (0, 0, 0, 255))
        for y in range(210):
            for x in range(400):
                src.putpixel((x, y), (y, y, y, 255))
        out = imageutil.scale_to_cover(src, 400, 200,
                                       gravity_y=imageutil.TOP_HEAVY)
        self.assertEqual(out.size, (400, 200))
        # Wanted to centre on row 70 and could not: it sits at the top.
        # The ALPHA is the half that matters -- back when this cropped the
        # scaled picture, a box hanging off it came back the right size
        # padded with transparent black, so an assertion on the colour
        # alone passed on an unclamped crop. Cropping in source space, an
        # unclamped box raises out of Pillow instead; the assertion catches
        # both, and is kept as written because which one it is depends on a
        # detail of how the crop is spelled.
        self.assertEqual(out.getpixel((200, 0)), (0, 0, 0, 255))

    def test_gravity_never_asks_for_more_than_the_source_has(self):
        """Over the whole range of banner shapes, not one of them: the
        clamp is a boundary and a single shape either hits it or does not.
        """
        src = Image.new("RGBA", (1920, 1080), (10, 20, 30, 255))
        for w, h in ((1100, 412), (1910, 412), (2550, 412), (900, 337),
                     (1280, 720), (400, 900)):
            with self.subTest(box=(w, h)):
                out = imageutil.scale_to_cover(
                    src, w, h, gravity_y=imageutil.TOP_HEAVY)
                self.assertEqual(out.size, (w, h))
                # An out-of-bounds crop is padded with transparent black, so
                # full opacity is the evidence it stayed inside -- and BOTH
                # corners, because the bias only ever runs off the top, so
                # the far corner alone is satisfied by a crop that does.
                for corner in ((0, 0), (w - 1, h - 1)):
                    self.assertEqual(out.getpixel(corner), (10, 20, 30, 255),
                                     "cropped outside the picture at %r"
                                     % (corner,))

    def test_no_width_leaves_a_transparent_hairline(self):
        """`int()` truncates, and the scale is exactly `w / iw` on the
        binding axis, so the product lands a hair under the target for
        about one width in eighty -- 1920x1080 into 999px gave 998, a
        negative `left`, and a crop reaching outside the picture, which
        Pillow pads rather than refuses.

        Swept rather than spot-checked: a full-bleed banner's width tracks
        the window pixel for pixel, so this flickered in and out during a
        drag-resize and any single width is overwhelmingly likely to miss
        it."""
        src = Image.new("RGBA", (1920, 1080), (10, 20, 30, 255))
        bad = []
        for w in range(900, 2600):
            out = imageutil.scale_to_cover(src, w, 412,
                                           gravity_y=imageutil.TOP_HEAVY)
            if (out.size != (w, 412)
                    or out.getpixel((0, 0))[3] != 255
                    or out.getpixel((w - 1, 411))[3] != 255):
                bad.append(w)
        self.assertEqual(bad, [], "padded edge at %d widths, e.g. %r"
                                  % (len(bad), bad[:5]))

    def test_the_banner_asks_for_the_top_of_its_backdrop(self):
        """The pure function having a knob proves nothing about the header
        using it -- and the header is the only caller that wants it."""
        src = Image.new("RGBA", (1920, 1080), (0, 0, 0, 255))
        for y in range(1080):
            for x in range(1920):
                src.putpixel((x, y), (y // 5, y // 5, y // 5, 255))
        box = (1910, 412)
        out = components.banner.compose_banner(src, box)
        centred = imageutil.scale_to_cover(src, *box)
        self.assertLess(out.getpixel((box[0] // 2, box[1] // 2))[0],
                        centred.getpixel((box[0] // 2, box[1] // 2))[0] - 5,
                        "the banner still takes the middle band")

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
        # os.walk, not listdir: pages/, components/ and gateway/ are
        # subpackages, and a flat scan silently stopped covering them when
        # the refactor moved code there -- it kept passing while checking a
        # shrinking fraction of the source.
        sources = [os.path.join(root, fn)
                   for root, _d, files in os.walk(pkg)
                   if "__pycache__" not in root
                   for fn in sorted(files) if fn.endswith(".py")]
        for path in sources:
            name = os.path.relpath(path, pkg).replace(os.sep, "/")
            with open(path, encoding="utf-8") as fh:
                tree = ast.parse(fh.read(), filename=path)
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

        out = components.compose_banner(
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
