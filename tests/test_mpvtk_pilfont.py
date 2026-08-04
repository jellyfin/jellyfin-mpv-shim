"""Script detection + font selection for text baked into bitmaps.

Pillow does no font fallback, so the wrong face renders CJK as tofu. These
tests pin the script classifier (which is what picks the face); which
concrete font file gets loaded is a property of the host, so we only assert
that *something* usable comes back.
"""

import unittest

from jellyfin_mpv_shim.mpvtk import pilfont


class TestScriptOf(unittest.TestCase):
    def test_ascii_is_latin(self):
        self.assertEqual(pilfont.script_of("Blade Runner 2049"), "latin")

    def test_accents_and_cyrillic_stay_latin(self):
        # DejaVu covers these, so they must not pull in a CJK face.
        self.assertEqual(pilfont.script_of("Amélie"), "latin")
        self.assertEqual(pilfont.script_of("Иван"), "latin")

    def test_japanese_and_korean_are_cjk(self):
        self.assertEqual(pilfont.script_of("進撃の巨人"), "cjk")
        self.assertEqual(pilfont.script_of("ハウルの動く城"), "cjk")
        self.assertEqual(pilfont.script_of("오징어 게임"), "cjk")

    def test_mixed_string_follows_the_first_non_latin_char(self):
        self.assertEqual(pilfont.script_of("進撃の巨人 (2013)"), "cjk")

    def test_other_scripts(self):
        self.assertEqual(pilfont.script_of("مسلسل"), "arabic")
        self.assertEqual(pilfont.script_of("ภาพยนตร์"), "thai")

    def test_empty_is_latin(self):
        self.assertEqual(pilfont.script_of(""), "latin")
        self.assertEqual(pilfont.script_of(None), "latin")


class TestFontFor(unittest.TestCase):
    def test_returns_a_font_and_caches_it(self):
        a = pilfont.font_for("Hello", 20)
        b = pilfont.font_for("Goodbye", 20)
        self.assertIsNotNone(a)
        self.assertIs(a, b)          # same script+size -> same cached face

    def test_cjk_request_never_raises(self):
        # Falls back to the Latin face (tofu) rather than blowing up when no
        # CJK font is installed.
        self.assertIsNotNone(pilfont.font_for("進撃", 20))

    def test_size_is_part_of_the_key(self):
        self.assertIsNot(pilfont.font_for("Hi", 20), pilfont.font_for("Hi", 30))


if __name__ == "__main__":
    unittest.main()


class TestRuns(unittest.TestCase):
    """Splitting a string into the faces it needs.

    The bug this exists for: a face named for a script is very often a face
    for *only* that script. Measured against the fonts on a plain Debian box,
    DroidSansFallback (the CJK face you land on without Noto CJK installed),
    NotoSansThai and NotoSansArabic all draw the letter A as .notdef — so
    "進撃の巨人 (2013)" came out with the year as tofu boxes.
    """

    def test_pure_latin_is_one_run(self):
        # The common case, and the one that must keep taking the old path:
        # one run means one draw call with the font the caller resolved.
        self.assertEqual(pilfont.runs("Blade Runner 2049"),
                         [("latin", "Blade Runner 2049")])

    def test_a_mixed_title_splits_at_the_script_boundary(self):
        self.assertEqual(pilfont.runs("進撃の巨人 (2013)"),
                         [("cjk", "進撃の巨人 "), ("latin", "(2013)")])

    def test_whitespace_does_not_start_a_run(self):
        """A space is blank in every face, so splitting on it would double
        the run count of an ordinary sentence for no visible difference."""
        self.assertEqual(pilfont.runs("привет мир"),
                         [("latin", "привет мир")])
        self.assertEqual(len(pilfont.runs("東京 の 夜")), 1)

    def test_digits_are_latin_not_neutral(self):
        """The whole bug in one assertion. If digits rode along with the
        surrounding run they would be drawn with the CJK face, which is
        exactly where the tofu came from."""
        self.assertEqual(pilfont.runs("巨人2013"),
                         [("cjk", "巨人"), ("latin", "2013")])

    def test_alternating_scripts_keep_their_order(self):
        self.assertEqual(pilfont.runs("A東B"),
                         [("latin", "A"), ("cjk", "東"), ("latin", "B")])

    def test_empty(self):
        self.assertEqual(pilfont.runs(""), [])
        self.assertEqual(pilfont.runs(None), [])


class TestRtl(unittest.TestCase):
    """Right-to-left strings are deliberately left on one face.

    Pillow reorders bidi text within a single draw call and cannot across
    several, so drawing runs left to right in logical order would put the
    Arabic in the wrong place. Tofu in one run is an ugly line; reordered
    text is a wrong one.
    """

    def test_arabic_and_hebrew_are_flagged(self):
        self.assertTrue(pilfont.has_rtl("الفيلم (2013)"))
        self.assertTrue(pilfont.has_rtl("סרט"))

    def test_everything_else_is_not(self):
        for text in ("Blade Runner", "進撃の巨人 (2013)", "ภาพยนตร์", ""):
            self.assertFalse(pilfont.has_rtl(text), text)


class TestDrawAndMeasure(unittest.TestCase):
    """Drawing and measuring have to split identically.

    They disagree by a lot when they disagree at all: a face that cannot draw
    a run renders it as .notdef boxes, which are *wider* than the digits they
    stand in for (measured: 248px against 189px for one caption), so
    measuring one way and drawing the other ellipsizes a caption that fitted.
    """

    def setUp(self):
        from PIL import Image, ImageDraw
        self.img = Image.new("L", (400, 40), 0)
        self.draw = ImageDraw.Draw(self.img)

    def measure(self, text, font):
        return pilfont.text_length(self.draw, text, font)

    def test_length_matches_pil_for_a_single_run(self):
        font = pilfont.font_for("Blade Runner", 20)
        self.assertEqual(self.measure("Blade Runner", font),
                         self.draw.textlength("Blade Runner", font=font))

    def test_a_mixed_string_measures_as_the_sum_of_its_runs(self):
        font = pilfont.font_for("進撃の巨人 (2013)", 20)
        parts = pilfont.runs("進撃の巨人 (2013)")
        expected = sum(
            self.draw.textlength(chunk,
                                 font=pilfont.font(script, 20, False))
            for script, chunk in parts)
        self.assertAlmostEqual(self.measure("進撃の巨人 (2013)", font),
                               expected, places=3)

    def test_a_mixed_string_is_drawn_run_by_run_on_one_baseline(self):
        """What draw_text promises, asserted against the drawing it claims
        to be equivalent to: each run with its own face, all of them sharing
        the tallest ascent in the line.

        The shared baseline is not a detail. PIL's default vertical anchor is
        the *ascender*, and two faces do not share one, so anchoring each run
        that way staggers them by the difference -- which on a CJK face
        against DejaVu is several pixels of visible step mid-caption.
        """
        from PIL import Image, ImageDraw

        text = "進撃の巨人 (2013)"
        font = pilfont.font_for(text, 20)
        parts = pilfont.runs(text)
        if len(parts) < 2:
            self.skipTest("this host resolves the whole string to one face")
        faces = [pilfont.font(script, 20, False) for script, _c in parts]
        if len({id(f) for f in faces}) == 1:
            self.skipTest("no separate CJK face installed on this host")

        got = Image.new("L", (400, 40), 0)
        pilfont.draw_text(ImageDraw.Draw(got), (3, 5), text, font, fill=255)

        want = Image.new("L", (400, 40), 0)
        drawer = ImageDraw.Draw(want)
        baseline = 5 + max(f.getmetrics()[0] for f in faces)
        x = 3.0
        for (_script, chunk), face in zip(parts, faces):
            drawer.text((x, baseline), chunk, font=face, fill=255,
                        anchor="ls")
            x += drawer.textlength(chunk, font=face)
        self.assertEqual(got.tobytes(), want.tobytes())

    def test_the_latin_run_is_not_what_the_cjk_face_would_have_drawn(self):
        """A guard on the premise. If the host's CJK face happens to cover
        Latin, everything above passes with the fix reverted -- so say so
        rather than reporting a pass that proves nothing."""
        from PIL import Image, ImageDraw

        text = "進撃の巨人 (2013)"
        cjk = pilfont.font_for(text, 20)
        latin = pilfont.font("latin", 20)
        if cjk is latin:
            self.skipTest("no separate CJK face installed on this host")

        def bitmap(font):
            img = Image.new("L", (200, 40), 0)
            ImageDraw.Draw(img).text((2, 4), "(2013)", font=font, fill=255)
            return img.tobytes()

        if bitmap(cjk) == bitmap(latin):
            self.skipTest("this host's CJK face draws Latin identically")
        self.assertNotEqual(
            bitmap(cjk), bitmap(latin),
            "the two faces draw Latin the same, so this file cannot tell a "
            "fixed render from a broken one")

    def test_a_single_run_string_is_drawn_exactly_as_before(self):
        """The safety property: only strings that were broken change."""
        from PIL import Image, ImageDraw

        text, font = "Blade Runner 2049", pilfont.font("latin", 20)
        old = Image.new("L", (400, 40), 0)
        ImageDraw.Draw(old).text((3, 5), text, font=font, fill=255)
        new = Image.new("L", (400, 40), 0)
        pilfont.draw_text(ImageDraw.Draw(new), (3, 5), text, font, fill=255)
        self.assertEqual(old.tobytes(), new.tobytes())
