"""Transparent artwork — channel logos, in practice.

Broadcasters ship logos as PNGs with a transparent background, and a good
number of them are *black* artwork drawn for a white page. The decode step
used to convert("RGB"), which does not composite: it drops the alpha channel
and keeps whatever RGB was under it, which for those files is black. So the
Live TV channel grid and the guide's channel column rendered a wall of solid
black blocks.

Two things have to hold for that to stay fixed: the alpha has to survive
decode and compositing, and the artwork has to get the light plate it was
drawn for — every transparent logo, so a row of channels is a row of one kind
of chip. The one thing that plate cannot carry is white ink lying directly on
the transparency, and that gets a drop shadow rather than a different plate.
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
import threading
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from PIL import Image, ImageDraw  # noqa: E402

from jellyfin_mpv_shim import imageutil  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import theme  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.strips import (  # noqa: E402
    StripStore, Tile, TileGeom, logo_plate,
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


def _keylined(fill, keyline, size=(120, 120), margin=20, width=4):
    """``fill`` artwork with a ``keyline`` around it — the shape of every white
    wordmark that ships with an outline. Same ink as ``_logo(fill)`` bar the
    border; what differs is which of it touches the background."""
    img = _logo(keyline, size=size, margin=margin)
    ImageDraw.Draw(img).rectangle(
        [margin + width, margin + width,
         size[0] - margin - width - 1, size[1] - margin - width - 1],
        fill=fill + (255,))
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

    def test_the_edge_ring_is_the_ink_that_meets_the_background(self):
        """A keylined block and a bare one have near-identical ink; they
        differ in which of it is on the boundary, and that is the whole
        question a white plate asks."""
        bare = imageutil.measure_transparency(_logo((255, 255, 255)))
        keyed = imageutil.measure_transparency(
            _keylined((255, 255, 255), (0, 0, 0)))
        self.assertGreater(sum(bare.edge[250:]), sum(bare.edge) * 0.9)
        self.assertGreater(sum(keyed.edge[:5]), sum(keyed.edge) * 0.9)
        # ...while the ink as a whole is white in both cases.
        self.assertGreater(keyed.luma, 200)

    def test_ink_thinner_than_the_erosion_is_all_edge(self):
        """A hairline stroke has no interior, and the ring must not come back
        empty — an empty measurement reads as "nothing is lost"."""
        img = Image.new("RGBA", (60, 60), (0, 0, 0, 0))
        ImageDraw.Draw(img).line([(5, 30), (55, 30)], fill=(255, 255, 255, 255))
        stats = imageutil.measure_transparency(img)
        self.assertEqual(sum(stats.edge), sum(stats.hist))


class TestPlateDecision(unittest.TestCase):
    def test_a_transparent_logo_gets_the_light_plate(self):
        plate = imageutil.plate_for(_logo((0, 0, 0)))
        self.assertIsNotNone(plate)
        self.assertGreater(imageutil._luma(plate.color), 200)
        self.assertFalse(plate.shadow)

    def test_every_transparent_logo_gets_the_same_plate(self):
        """The point of the rule. A dark logo, a bright one, a saturated one:
        one plate, so a row of channels is a row of one kind of chip instead
        of a per-logo contrast judgement that lands differently on each."""
        plates = [imageutil.plate_for(_logo(c)).color
                  for c in ((0, 0, 0), (255, 255, 255), (38, 56, 196),
                            (230, 60, 60), (128, 128, 128))]
        self.assertEqual(plates, [plates[0]] * len(plates))

    def test_white_ink_against_the_background_gets_a_shadow(self):
        """The one thing a white plate cannot carry. Same plate — consistency
        is the point — with a shadow to give the outer ink an edge."""
        self.assertTrue(imageutil.plate_for(_logo((255, 255, 255))).shadow)

    def test_a_keyline_is_what_the_white_sits_against(self):
        """Nearly every white wordmark ships with an outline or a coloured
        mark around it, and those read on white perfectly well. It is not
        "the logo is bright" that wants a shadow — the same white ink behind a
        4px keyline is left flat."""
        self.assertFalse(
            imageutil.plate_for(_keylined((255, 255, 255), (0, 0, 0))).shadow)
        self.assertFalse(
            imageutil.plate_for(_keylined((255, 255, 255), (200, 30, 30))).shadow)

    def test_opaque_art_is_never_plated(self):
        """A poster carries its own background. Even a very dark one."""
        self.assertIsNone(imageutil.plate_for(
            Image.new("RGB", (60, 60), (5, 5, 5))))
        self.assertIsNone(imageutil.plate_for(
            Image.new("RGBA", (60, 60), (5, 5, 5, 255))))

    def test_a_soft_edge_is_not_a_transparent_background(self):
        """min_clear: art that fills its frame apart from a few anti-aliased
        corner pixels is opaque art, and plating it would show as a border."""
        img = _logo((0, 0, 0), size=(120, 120), margin=1)
        self.assertIsNone(imageutil.plate_for(img))

    def test_a_dark_wordmark_beside_a_bright_mark_needs_no_shadow(self):
        """The NBC logo: a saturated mark against the background, a black
        wordmark beside it. Its *mean* luma is mid-grey, a colour neither half
        of the artwork contains — nothing here decides on the mean, and the
        boundary ink is what a white plate has to carry."""
        logo = _two_tone((230, 60, 60), (0, 0, 0))
        self.assertGreater(imageutil.measure_transparency(logo).luma, 48)
        self.assertFalse(imageutil.plate_for(logo).shadow)

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
            plates.append(imageutil.plate_for(small))
        self.assertEqual(plates, [plates[0]] * len(sizes), plates)
        self.assertIsNotNone(plates[0])

    def test_half_white_half_black_is_plated_and_shadowed(self):
        """No flat plate carries both halves, which used to mean neither got
        one. White plus a shadow does: the black half reads on the plate and
        the white half against its own edge."""
        plate = imageutil.plate_for(_two_tone((255, 255, 255), (0, 0, 0)))
        self.assertGreater(imageutil._luma(plate.color), 200)
        self.assertTrue(plate.shadow)

    def test_a_semi_transparent_panel_is_judged_by_the_ink_on_it(self):
        """The alpha threshold cuts both ways, pinned so it is not a surprise.

        Artwork that is a translucent panel with opaque text on it measures as
        the *text* — a panel at alpha 89 is under the mask's 128 and counts
        towards ``clear``, so 5% of the pixels decide for all of them. Right
        here, since the text is what has to be readable and the panel is what
        the plate sits behind: white text on it is shadowed, as bare white ink
        should be. It would be wrong for a translucent *bright* panel carrying
        white text, which no measurement taken through this mask can see.
        """
        logo = Image.new("RGBA", (120, 120), (150, 140, 200, 89))
        ImageDraw.Draw(logo).rectangle([30, 50, 90, 70],
                                       fill=(255, 255, 255, 255))
        stats = imageutil.measure_transparency(logo)
        self.assertLess(sum(stats.hist), 120 * 120 * 0.1)   # the text, not the panel
        self.assertTrue(imageutil.plate_for(logo).shadow)

    def test_a_small_bright_detail_does_not_shadow_a_dark_logo(self):
        """max_edge: a dark mark with a bright corner to it reads on the plate
        as it is, and shadowing everything with a white pixel on its boundary
        is the old all-or-nothing decision wearing a different hat."""
        logo = _two_tone((0, 0, 0), (255, 255, 255), split=0.9)
        self.assertFalse(imageutil.plate_for(logo).shadow)

    def test_the_shadow_darkens_only_around_the_ink(self):
        """It has to reach *outside* the silhouette — that is the edge the
        white ink is given — without dulling the artwork itself."""
        logo = _logo((255, 255, 255), size=(120, 120), margin=20)
        out = imageutil.flatten_onto(imageutil.with_shadow(logo),
                                     (240, 240, 240))
        self.assertEqual(out.getpixel((60, 60))[:3], (255, 255, 255))
        self.assertLess(imageutil._luma(out.getpixel((60, 102))), 200)
        self.assertGreater(imageutil._luma(out.getpixel((2, 2))), 200)

    def test_the_shadow_leaves_opaque_art_alone(self):
        opaque = Image.new("RGB", (40, 40), (10, 20, 30))
        self.assertIs(imageutil.with_shadow(opaque), opaque)

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

    def _painted(self, poster, rounded=False, live=True):
        """``live`` defaults TRUE because this class is about channel logos
        -- dark ink on transparency, which is the convention the plate exists
        for and the one that ships plated. The library convention is the
        other way round and is covered by TestLogoLegibilitySettings."""
        g = TileGeom().physical()
        img = Image.new("RGBA", (g.tile_w, g.strip_h), (0, 0, 0, 0))
        store = StripStore(cache_dir=None, mem_store=None)
        orig = theme.active
        if rounded:
            theme.active = lambda: dict(orig() or {}, rounded=True)
        try:
            store._paint_poster(
                img, ImageDraw.Draw(img), 0,
                Tile(key="k", title="T", poster=poster, live=live), g)
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

    def test_a_white_logo_gets_the_same_card_and_a_shadow(self):
        """The card is the light plate every other logo gets — a row of
        channels should not be half light chips and half dark ones — and the
        white artwork is held off it by its shadow."""
        logo = _logo((255, 255, 255), size=(140, 140))
        imageutil.measure_transparency(logo)
        img, g = self._painted(logo)
        self.assertGreater(imageutil._luma(img.getpixel((4, 4))), 200)
        self.assertGreater(
            imageutil._luma(img.getpixel((g.tile_w // 2, g.tile_h // 2))), 240)
        # Between the plate and the artwork, all the way across the tile.
        band = [imageutil._luma(img.getpixel((x, g.tile_h // 2)))
                for x in range(g.tile_w)]
        self.assertLess(min(band), 180, band)

    def test_the_rounded_theme_composites_the_alpha_too(self):
        """The rounded path passes paste() a corner mask, and paste takes only
        one — the art's own alpha has to be folded into it or the black comes
        straight back."""
        logo = _logo((255, 255, 255), size=(140, 140))
        imageutil.measure_transparency(logo)
        img, g = self._painted(logo, rounded=True)
        self.assertGreater(imageutil._luma(img.getpixel((g.tile_w // 2, 6))),
                           200)

    def test_an_opaque_poster_is_unchanged(self):
        """Every non-logo tile in the app goes down this path."""
        img, g = self._painted(Image.new("RGB", (140, 210), (200, 40, 40)))
        self.assertEqual(img.getpixel((g.tile_w // 2, g.tile_h // 2))[:3],
                         (200, 40, 40))

    def test_the_rounded_theme_does_not_crop_a_wide_logo(self):
        """Cover-crop is right for a poster or a still -- both are frames of
        a photograph and lose nothing at the edges -- and wrong for a
        wordmark, where it takes a bite out of the name. A Logo view is a
        grid of exactly that.

        Drawn as a wide bar of ink with transparent margins: cover-cropping
        it into the (portrait) tile scales the bar until it spans the width
        and beyond, so the ink reaches both edges. Contained, the margins
        survive.
        """
        logo = _logo((0, 0, 0), size=(400, 100), margin=10)
        imageutil.measure_transparency(logo)
        img, g = self._painted(logo, rounded=True)
        mid = g.tile_h // 2
        self.assertGreater(imageutil._luma(img.getpixel((1, mid))), 200,
                           "the left edge should still be plate, not ink")
        self.assertGreater(imageutil._luma(img.getpixel((g.tile_w - 2, mid))),
                           200, "the right edge should still be plate")


class TestLogoLegibilitySettings(unittest.TestCase):
    """"Make ... logos more legible" -- two settings, one per convention.

    Transparent artwork arrives in two flavours that want opposite
    treatment, and the item type is what tells them apart. A broadcaster's
    channel logo is dark ink drawn for a white page and is invisible here
    without the plate; a film's Logo artwork is white by convention, reads
    on a dark surface already, and it is the plate that then makes it need
    a drop shadow. So they default opposite ways and are configurable
    separately (#637).

    ``strips.logo_plate`` is the single place that decides, so the tile
    compositor and the table's art cells cannot answer it differently.
    """

    KEYS = ("logo_legibility_live_tv", "logo_legibility_library")

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        self.settings = settings
        for key in self.KEYS:
            self.addCleanup(setattr, settings, key, getattr(settings, key))

    def _set(self, live_tv=None, library=None):
        if live_tv is not None:
            self.settings.logo_legibility_live_tv = live_tv
        if library is not None:
            self.settings.logo_legibility_library = library

    def test_the_defaults_split_by_convention(self):
        """The whole point of there being two. Asserted on the class
        annotations rather than the live values, which a sibling test may
        have moved."""
        from jellyfin_mpv_shim.conf import Settings
        self.assertIs(Settings.logo_legibility_live_tv, True)
        self.assertIs(Settings.logo_legibility_library, False)

    def test_a_channel_logo_is_plated_and_a_library_logo_is_not(self):
        """Both at the shipped defaults, which is the behaviour that
        matters: the same picture, two answers, chosen by where it came
        from."""
        self._set(live_tv=True, library=False)
        black = _logo((0, 0, 0))
        self.assertGreater(imageutil._luma(logo_plate(black, True).color), 200)
        self.assertEqual(tuple(logo_plate(black, False).color),
                         theme.rgb(theme.CARD_BG))

    def test_each_setting_moves_only_its_own_half(self):
        """They are separate settings, not one with a scope."""
        self._set(live_tv=False, library=True)
        black = _logo((0, 0, 0))
        self.assertEqual(tuple(logo_plate(black, True).color),
                         theme.rgb(theme.CARD_BG))
        self.assertGreater(imageutil._luma(logo_plate(black, False).color), 200)

    def test_off_never_shadows(self):
        """Not "recompute the verdict against the grey" -- against a dark
        plate the question simply flips to the black ink, and the point of
        turning this off is that there are no drop shadows at all."""
        self._set(live_tv=False, library=False)
        for colour in ((255, 255, 255), (0, 0, 0), (230, 60, 60)):
            for live in (True, False):
                with self.subTest(colour=colour, live=live):
                    self.assertFalse(logo_plate(_logo(colour), live).shadow)

    def test_a_missing_key_falls_back_to_that_half_s_default(self):
        """Not to True. A settings object without the keys must not plate a
        library -- the fallback is the half's own default, per half."""
        import jellyfin_mpv_shim.conf as conf
        real, conf.settings = conf.settings, type("S", (), {})()
        self.addCleanup(setattr, conf, "settings", real)
        black = _logo((0, 0, 0))
        self.assertGreater(imageutil._luma(logo_plate(black, True).color), 200)
        self.assertEqual(tuple(logo_plate(black, False).color),
                         theme.rgb(theme.CARD_BG))

    def test_it_does_not_change_WHETHER_there_is_a_plate(self):
        """The callers read that for a second thing: artwork on a
        transparent background is a mark rather than a photograph, so it is
        letterboxed rather than cover-cropped. True whichever backing it
        gets, so an opaque poster is still left alone and a logo is still
        recognised."""
        for on in (True, False):
            for live in (True, False):
                with self.subTest(on=on, live=live):
                    self._set(live_tv=on, library=on)
                    self.assertIsNone(logo_plate(
                        Image.new("RGB", (60, 60), (5, 5, 5)), live))
                    self.assertIsNotNone(logo_plate(_logo((0, 0, 0)), live))

    def test_the_tile_card_follows_it(self):
        """End to end through the compositor: the card behind a black logo
        is the theme's, not a light plate."""
        self._set(live_tv=False)
        logo = _logo((0, 0, 0), size=(140, 140))
        imageutil.measure_transparency(logo)
        img, _g = TestTileCompositing()._painted(logo, live=True)
        self.assertEqual(img.getpixel((4, 4))[:3], theme.rgb(theme.CARD_BG))

    def test_the_tile_carries_which_half_it_is(self):
        """The compositor has the picture and not the item it came from, so
        the answer has to ride along -- and it has to be part of the cache
        key, or one tile's card colour is served to the other."""
        store = StripStore(cache_dir=None, mem_store=None)
        self.addCleanup(store.shutdown)
        art = _logo((0, 0, 0))
        a = Tile(key="k", title="T", poster=art, live=True)
        b = Tile(key="k", title="T", poster=art, live=False)
        self.assertNotEqual(store._tile_key(a), store._tile_key(b))

    def test_the_type_is_what_decides(self):
        """Every Live TV DTO that can resolve to a channel logo, and nothing
        else.

        ``Timer`` and ``SeriesTimer`` are the ones easily missed and were:
        a plain ``TimerInfoDto`` has neither ``ImageTags`` nor
        ``ParentPrimaryImage*``, so ``image_spec`` always falls through to
        the channel-logo branch for it -- which is also what jellyfin-web's
        schedule draws, via showChannelLogo. Left out, the Schedule tab was
        the one Live TV screen whose channel logos went unplated.

        A finished recording is excluded and does not need including: the
        server hands it back as an ordinary item, and ``recordings_page``
        never asks for ``ChannelImage``, so it cannot reach that branch.
        """
        from jellyfin_mpv_shim.mpvtk_browser import live_tv
        for itype in ("TvChannel", "Program", "Timer", "SeriesTimer"):
            with self.subTest(itype):
                self.assertTrue(live_tv.is_channel_artwork({"Type": itype}))
        for itype in ("Movie", "Episode", "Series", "Recording", "Video",
                      "MusicAlbum", "Studio"):
            with self.subTest(itype):
                self.assertFalse(live_tv.is_channel_artwork({"Type": itype}))
        self.assertFalse(live_tv.is_channel_artwork(None))

    def test_a_timer_and_its_channel_resolve_to_the_SAME_artwork(self):
        """Which is why the type test above has to cover both: the two DTOs
        produce a byte-identical image spec, so if they disagreed about the
        setting the identical logo would be plated on one screen and not on
        the next."""
        from jellyfin_mpv_shim.mpvtk_browser import live_tv
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource

        src = LibrarySource.__new__(LibrarySource)
        timer = {"Type": "Timer", "Id": "t1", "ChannelId": "c1",
                 "ChannelPrimaryImageTag": "ctag"}
        channel = {"Type": "TvChannel", "Id": "c1",
                   "ImageTags": {"Primary": "ctag"}}
        self.assertEqual(src.image_spec(timer, "Primary", 280),
                         src.image_spec(channel, "Primary", 280))
        self.assertEqual(live_tv.is_channel_artwork(timer),
                         live_tv.is_channel_artwork(channel))

    def test_a_toggle_makes_the_cached_strips_unreachable(self):
        """The plate is baked into the composited bitmap, so a strip that is
        already cached would go on showing the old backing until it aged out
        of the LRU -- which for the rows on screen is never."""
        store = StripStore(cache_dir=None, mem_store=None)
        self.addCleanup(store.shutdown)
        store.set_theme_tag("default")
        seen = [store.tag]
        for _i in range(3):
            store.retag()
            self.assertNotIn(store.tag, seen, "a retag reused a tag")
            seen.append(store.tag)
        # ...and the two axes are independent: a theme change after a retag
        # still invalidates, and does not undo one.
        store.set_theme_tag("nordic")
        self.assertNotIn(store.tag, seen)
        self.assertIn("nordic", store.tag)


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
