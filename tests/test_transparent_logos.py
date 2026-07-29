"""Transparent artwork — channel logos, in practice.

Broadcasters ship logos as PNGs with a transparent background, and a good
number of them are *black* artwork drawn for a white page. The decode step
used to convert("RGB"), which does not composite: it drops the alpha channel
and keeps whatever RGB was under it, which for those files is black. So the
Live TV channel grid and the guide's channel column rendered a wall of solid
black blocks.

Two things have to hold for that to stay fixed: the alpha has to survive
decode and compositing, and an image that would be unreadable against the
near-black surface behind it has to get a plate of its own.
"""

import os
import sys
import tempfile
import threading
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from PIL import Image, ImageDraw  # noqa: E402

from jellyfin_mpv_shim import imageutil  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import theme  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.strips import (  # noqa: E402
    StripStore, Tile, TileGeom,
)
from jellyfin_mpv_shim.mpvtk_browser.thumbnails import (  # noqa: E402
    ThumbnailStore, make_key,
)

WINDOW_BG = (0x15, 0x17, 0x1a)


def _logo(color, size=(120, 120), margin=20):
    """Artwork of ``color`` on a transparent background — the shape of a
    channel logo: a solid block inset in empty space."""
    img = Image.new("RGBA", size, (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle(
        [margin, margin, size[0] - margin - 1, size[1] - margin - 1],
        fill=color + (255,))
    return img


def _two_tone(bright, dark, split=0.5, size=(120, 120), margin=20):
    """The NBC shape: one bright mark beside one dark one, on transparency.
    Its *mean* luma is a colour neither half of the artwork contains."""
    img = _logo(bright, size=size, margin=margin)
    edge = margin + int((size[0] - 2 * margin) * split)
    ImageDraw.Draw(img).rectangle(
        [edge, margin, size[0] - margin - 1, size[1] - margin - 1],
        fill=dark + (255,))
    return img


class TestMeasurement(unittest.TestCase):
    def test_luma_ignores_the_black_under_the_transparency(self):
        """The whole point of the mask: an unmasked mean would call a white
        logo on a transparent background dark, because Pillow leaves black
        under every clear pixel."""
        stats = imageutil.measure_transparency(_logo((255, 255, 255)))
        self.assertGreater(stats.luma, 250)
        self.assertAlmostEqual(stats.clear, 1 - (80 * 80) / (120 * 120),
                               places=2)

    def test_black_artwork_measures_dark(self):
        self.assertLess(imageutil.measure_transparency(_logo((0, 0, 0))).luma,
                        5)

    def test_the_histogram_covers_the_visible_pixels_only(self):
        """It is the ink's distribution, so its total is the ink, not the
        frame — the clear pixels are excluded, not counted as black."""
        stats = imageutil.measure_transparency(_logo((0, 0, 0)))
        self.assertEqual(sum(stats.hist), 80 * 80)
        self.assertEqual(stats.hist[0], 80 * 80)

    def test_an_image_without_alpha_is_not_measured(self):
        self.assertIsNone(
            imageutil.measure_transparency(Image.new("RGB", (10, 10))))

    def test_the_measurement_is_parked_on_the_image(self):
        """Measured once on the thumbnail worker; the compositors read it."""
        img = _logo((0, 0, 0))
        got = imageutil.measure_transparency(img)
        self.assertEqual(img.info[imageutil.ALPHA_INFO], got)


class TestPlateDecision(unittest.TestCase):
    def test_dark_logo_on_a_dark_surface_gets_a_light_plate(self):
        plate = imageutil.plate_color(_logo((0, 0, 0)), WINDOW_BG)
        self.assertIsNotNone(plate)
        self.assertGreater(imageutil._luma(plate), 200)

    def test_light_logo_on_a_dark_surface_is_left_alone(self):
        """These already read, and plating them would be the same bug with
        the colours swapped."""
        self.assertIsNone(imageutil.plate_color(_logo((255, 255, 255)),
                                                WINDOW_BG))

    def test_light_logo_on_a_light_surface_gets_a_dark_plate(self):
        plate = imageutil.plate_color(_logo((255, 255, 255)),
                                      (240, 240, 240))
        self.assertIsNotNone(plate)
        self.assertLess(imageutil._luma(plate), 40)

    def test_opaque_art_is_never_plated(self):
        """A poster carries its own background. Even a very dark one."""
        self.assertIsNone(imageutil.plate_color(
            Image.new("RGB", (60, 60), (5, 5, 5)), WINDOW_BG))
        self.assertIsNone(imageutil.plate_color(
            Image.new("RGBA", (60, 60), (5, 5, 5, 255)), WINDOW_BG))

    def test_a_soft_edge_is_not_a_transparent_background(self):
        """min_clear: art that fills its frame apart from a few anti-aliased
        corner pixels is opaque art, and plating it would show as a border."""
        img = _logo((0, 0, 0), size=(120, 120), margin=1)
        self.assertIsNone(imageutil.plate_color(img, WINDOW_BG))

    def test_a_dark_wordmark_beside_a_bright_mark_is_plated(self):
        """The NBC logo. Half its ink is a black wordmark, invisible on this
        UI's surfaces; the bright half drags the *mean* to mid-grey, which
        reads as "contrasts fine" and left the wordmark unreadable. The
        decision is about how much ink the surface swallows, not the mean."""
        logo = _two_tone((230, 60, 60), (0, 0, 0))
        self.assertGreater(imageutil.measure_transparency(logo).luma, 48)
        plate = imageutil.plate_color(logo, WINDOW_BG)
        self.assertIsNotNone(plate)
        self.assertGreater(imageutil._luma(plate), 200)

    def test_the_plate_decision_survives_a_downscale(self):
        """It used to sit on the threshold: the full-size logo was plated and
        the guide's smaller copy of the same file was not, because resampling
        moved the mean by a fraction of a luma step."""
        logo = _two_tone((230, 60, 60), (0, 0, 0), size=(275, 206), margin=24)
        sizes = [(275, 206), (96, 72), (48, 36), (24, 18)]
        plates = []
        for size in sizes:
            small = logo.resize(size, Image.LANCZOS)
            imageutil.measure_transparency(small)
            plates.append(imageutil.plate_color(small, WINDOW_BG))
        self.assertEqual(plates, [plates[0]] * len(sizes), plates)
        self.assertIsNotNone(plates[0])

    def test_art_no_flat_plate_can_rescue_is_left_alone(self):
        """Half white ink, half black: every neutral loses as much as the
        surface did. Plating buys nothing and costs a white box."""
        logo = _two_tone((255, 255, 255), (0, 0, 0))
        self.assertIsNone(imageutil.plate_color(logo, WINDOW_BG))

    def test_a_semi_transparent_panel_is_judged_by_the_ink_on_it(self):
        """The alpha threshold cuts both ways, pinned so it is not a surprise.

        Artwork that is a translucent panel with opaque text on it measures as
        the *text* — a panel at alpha 89 is under the mask's 128 and counts
        towards ``clear``, so 5% of the pixels decide for all of them. Right
        here, since the text is what has to be readable and the panel is what
        the plate would sit behind: white text is left alone on a dark surface
        and plated dark on a light one. It would be wrong for a translucent
        *bright* panel carrying white text, which no measurement taken through
        this mask can see.
        """
        logo = Image.new("RGBA", (120, 120), (150, 140, 200, 89))
        ImageDraw.Draw(logo).rectangle([30, 50, 90, 70],
                                       fill=(255, 255, 255, 255))
        stats = imageutil.measure_transparency(logo)
        self.assertLess(sum(stats.hist), 120 * 120 * 0.1)   # the text, not the panel
        self.assertIsNone(imageutil.plate_color(logo, WINDOW_BG))
        self.assertIsNotNone(imageutil.plate_color(logo, (0xf2, 0xf2, 0xf2)))

    def test_saturated_mid_luma_ink_is_plated_too(self):
        """A known approximation, pinned so a change to it is deliberate.

        Luma under-rates saturated ink — the PBS blue is luma 66 and reads on
        near-black better than that suggests — so a logo like it is plated
        where a colour-aware measure might leave it. Judged acceptable: at
        2.2:1 against the surface it is genuinely low contrast, and a white
        chip is the page these logos were drawn for. Measuring value as a
        second axis fixes this one and breaks the NBC peacock against a white
        plate; what would settle the rest is spatial, and no histogram sees it.
        """
        logo = _logo((38, 56, 196))
        self.assertIsNotNone(imageutil.plate_color(logo, WINDOW_BG))

    def test_a_small_dark_detail_does_not_plate_a_bright_logo(self):
        """max_lost: a bright mark with a little dark detail in it reads fine,
        and plating everything with a dark pixel in it is the old bug with the
        colours swapped."""
        logo = _two_tone((235, 235, 235), (0, 0, 0), split=0.9)
        self.assertIsNone(imageutil.plate_color(logo, WINDOW_BG))

    def test_flatten_onto_makes_the_clear_pixels_the_plate(self):
        out = imageutil.flatten_onto(_logo((0, 0, 0)), (240, 240, 240))
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((2, 2)), (240, 240, 240, 255))
        self.assertEqual(out.getpixel((60, 60)), (0, 0, 0, 255))

    def test_flatten_onto_can_round_its_corners(self):
        out = imageutil.flatten_onto(_logo((0, 0, 0)), (240, 240, 240),
                                     radius=12)
        self.assertEqual(out.getpixel((0, 0))[3], 0)      # corner cut away
        self.assertEqual(out.getpixel((60, 2)), (240, 240, 240, 255))


class TestDecode(unittest.TestCase):
    """ThumbnailStore._load_image — the step that used to flatten to black."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-logo-")
        self.store = ThumbnailStore(os.path.join(self.tmp, "cache"))
        self.addCleanup(self.store.shutdown)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _decode(self, img, name, box=(120, 120)):
        path = os.path.join(self.tmp, name)
        img.save(path)
        return self.store._load_image(make_key(name, "P", "t", box[0]),
                                      path, box)

    def test_a_transparent_logo_keeps_its_alpha(self):
        out = self._decode(_logo((0, 0, 0)), "logo.png")
        self.assertEqual(out.mode, "RGBA")
        self.assertEqual(out.getpixel((2, 2))[3], 0)

    def test_a_palette_png_with_transparency_keeps_it(self):
        """The other way a PNG carries transparency: mode P plus a
        ``transparency`` info key, which reads as opaque without the check."""
        src = _logo((0, 0, 0)).convert("P", palette=Image.ADAPTIVE)
        src.info["transparency"] = 0
        out = self._decode(src, "pal.png")
        self.assertEqual(out.mode, "RGBA")

    def test_an_opaque_image_stays_rgb(self):
        """RGBA everywhere would cost a byte per pixel across every poster
        and backdrop in the decoded-image cache."""
        out = self._decode(Image.new("RGB", (120, 120), (10, 20, 30)),
                           "poster.png")
        self.assertEqual(out.mode, "RGB")

    def test_the_decoded_logo_carries_its_measurement(self):
        out = self._decode(_logo((0, 0, 0)), "measured.png")
        self.assertIn(imageutil.ALPHA_INFO, out.info)

    def test_delivery_through_the_pool_keeps_the_alpha(self):
        """End to end, not just the private: the callback is what the tile
        renderer caches as the poster."""
        path = os.path.join(self.tmp, "wire.png")
        _logo((0, 0, 0)).save(path)
        got, done = [], threading.Event()

        def cb(img):
            got.append(img)
            done.set()

        self.store.request(make_key("w", "P", "t", 120), path, (120, 120), cb)
        for _ in range(200):
            if self.store.pump():
                break
            if done.wait(0.01):
                self.store.pump()
                break
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0].mode, "RGBA")


class TestTileCompositing(unittest.TestCase):
    """StripStore._paint_poster: what a channel tile actually looks like."""

    def _painted(self, poster, rounded=False):
        g = TileGeom().physical()
        img = Image.new("RGBA", (g.tile_w, g.strip_h), (0, 0, 0, 0))
        store = StripStore(cache_dir=None, mem_store=None)
        orig = theme.active
        if rounded:
            theme.active = lambda: dict(orig() or {}, rounded=True)
        try:
            store._paint_poster(img, ImageDraw.Draw(img), 0,
                                Tile(key="k", title="T", poster=poster), g)
        finally:
            theme.active = orig
        return img, g

    def test_a_black_logo_does_not_come_out_as_a_black_block(self):
        """The bug, stated as a test. The tile used to be uniformly black
        wherever the logo was; now the artwork sits on a light plate."""
        logo = _logo((0, 0, 0), size=(140, 140))
        imageutil.measure_transparency(logo)
        img, g = self._painted(logo)
        # The plate is behind the transparent part of the logo...
        self.assertGreater(imageutil._luma(img.getpixel((4, 4))), 200)
        # ...and the artwork itself is still black, i.e. legible against it.
        self.assertLess(imageutil._luma(img.getpixel((g.tile_w // 2,
                                                      g.tile_h // 2))), 20)

    def test_a_white_logo_keeps_the_card_behind_it(self):
        """No plate: the card colour shows through the transparency, which is
        exactly what the alpha is for."""
        logo = _logo((255, 255, 255), size=(140, 140))
        imageutil.measure_transparency(logo)
        img, g = self._painted(logo)
        card = theme.rgb(theme.CARD_BG, 255)
        self.assertEqual(img.getpixel((4, 4)), card)
        self.assertGreater(
            imageutil._luma(img.getpixel((g.tile_w // 2, g.tile_h // 2))), 240)

    def test_the_rounded_theme_composites_the_alpha_too(self):
        """The rounded path passes paste() a corner mask, and paste takes only
        one — the art's own alpha has to be folded into it or the black comes
        straight back."""
        logo = _logo((255, 255, 255), size=(140, 140))
        imageutil.measure_transparency(logo)
        img, g = self._painted(logo, rounded=True)
        self.assertEqual(img.getpixel((g.tile_w // 2, 6)),
                         theme.rgb(theme.CARD_BG, 255))

    def test_an_opaque_poster_is_unchanged(self):
        """Every non-logo tile in the app goes down this path."""
        img, g = self._painted(Image.new("RGB", (140, 210), (200, 40, 40)))
        self.assertEqual(img.getpixel((g.tile_w // 2, g.tile_h // 2))[:3],
                         (200, 40, 40))


class TestStripsEndToEnd(unittest.TestCase):
    def test_a_logo_strip_composites_without_error(self):
        """_paint_poster is exercised above; this pins that a transparent
        poster survives the full strip path (keying, BGRA store) too."""
        tmp = tempfile.mkdtemp(prefix="mpvtk-logo-strip-")
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp,
                                                            ignore_errors=True))
        store = StripStore(cache_dir=tmp)
        self.addCleanup(store.shutdown)
        logo = _logo((0, 0, 0), size=(140, 210))
        imageutil.measure_transparency(logo)
        out = store.strip([Tile(key="a", title="A", poster=logo)])
        self.assertEqual(out["regions"][0]["key"], "a")


if __name__ == "__main__":
    unittest.main()
