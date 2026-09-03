"""Script detection + font selection for text baked into bitmaps.

Pillow does no font fallback, so the wrong face renders CJK as tofu. These
tests pin the script classifier (which is what picks the face); which
concrete font file gets loaded is a property of the host, so we only assert
that *something* usable comes back.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

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


class TestHebrew(unittest.TestCase):
    """Hebrew has a face of its own (F33).

    It was folded into "latin" on the strength of DejaVu having it. So does
    Arial. `NotoSans-Regular.ttf` -- third in the Latin chain -- does not,
    so on a host with Noto Sans and no DejaVu every Hebrew title was a row
    of boxes in every baked bitmap. Exactly #713's shape, in a line nobody
    had edited.
    """

    def test_the_hebrew_block_is_its_own_script(self):
        for cp in (0x05D0, 0x05EA, 0x0590, 0x05FF):
            self.assertEqual(pilfont.script_of_char(cp), "hebrew",
                             "U+%04X" % cp)

    def test_the_presentation_forms_go_with_it(self):
        """U+FB1D-FB4F is Hebrew too, and was landing in the `cp >= 0x2E80`
        CJK catch-all -- measured: NotoSansCJK cannot draw it."""
        for cp in (0xFB1D, 0xFB2A, 0xFB4F):
            self.assertEqual(pilfont.script_of_char(cp), "hebrew",
                             "U+%04X" % cp)

    def test_the_latin_ligatures_next_door_are_left_alone(self):
        """U+FB00-FB1C is the Latin/Armenian half of the same block, and the
        CJK face it currently resolves to *can* draw it (measured). Moving
        it would be churn against nothing."""
        self.assertEqual(pilfont.script_of_char(0xFB01), "cjk")

    def test_arabic_presentation_forms_b_reaches_the_arabic_face(self):
        """U+FE70-FEFF: `has_rtl` already called it RTL while
        `script_of_char` sent it to CJK, so the whole line was drawn with a
        face that cannot draw a word of it."""
        for cp in (0xFE70, 0xFE8D, 0xFEFC):
            self.assertEqual(pilfont.script_of_char(cp), "arabic",
                             "U+%04X" % cp)

    def test_every_rtl_codepoint_maps_to_an_rtl_face(self):
        """The invariant behind all four tests above, and the one that
        catches the next range added to either table.

        `has_rtl` decides that a line is drawn with ONE face; `script_of`
        decides which. If a codepoint is RTL and its script is neither
        Hebrew nor Arabic, the one face chosen is by definition not a face
        for it -- which is how U+FE70-FEFF ended up on a CJK face.
        """
        for lo, hi in pilfont._RTL_RANGES:
            for cp in (lo, (lo + hi) // 2, hi):
                self.assertIn(pilfont.script_of_char(cp),
                              ("hebrew", "arabic"),
                              "U+%04X is RTL but maps to %r"
                              % (cp, pilfont.script_of_char(cp)))

    def test_the_hebrew_face_carries_the_punctuation_around_it(self):
        """An RTL face has to cover the whole LINE, not just the script.

        `has_rtl` gives an RTL line to one face because Pillow reorders
        bidi within a draw call and cannot across several -- so unlike
        every other script here, there is no Latin run for the full stop
        and the year to fall back to. `NotoSansHebrew-Regular.ttf` is 145
        codepoints and has no ASCII at all, so ordering it first drew every
        Hebrew sentence with its stop as a box.
        """
        from PIL import Image, ImageDraw

        face = pilfont.font("hebrew", 28)

        def bitmap(text):
            img = Image.new("L", (200, 48), 0)
            ImageDraw.Draw(img).text((2, 2), text, font=face, fill=255)
            return img.tobytes()

        tofu = bitmap("\U000FFFFF")
        if bitmap("א") == tofu:
            self.skipTest("no Hebrew-carrying face installed on this host")
        for ch in (".", ",", "0", "9", "(", ")"):
            self.assertNotEqual(bitmap(ch), tofu,
                                "the Hebrew face draws %r as tofu, and an "
                                "RTL line has no other face to use" % ch)

    def test_a_hebrew_title_reaches_a_face_that_has_it(self):
        """Against a Latin face measured to lack Hebrew -- which is what a
        box with Noto Sans and no DejaVu resolves."""
        from PIL import Image, ImageDraw, ImageFont

        def bitmap(font, text="א"):
            img = Image.new("L", (60, 48), 0)
            ImageDraw.Draw(img).text((2, 2), text, font=font, fill=255)
            return img.tobytes()

        saved = dict(pilfont._CANDIDATES)
        self.addCleanup(pilfont.clear_cache)
        self.addCleanup(pilfont._CANDIDATES.update, saved)
        self.addCleanup(pilfont._CANDIDATES.clear)

        hebrewless = None
        for name in ("NotoSans-Regular.ttf",
                     "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf"):
            try:
                face = ImageFont.truetype(name, 28)
            except OSError:
                continue
            if bitmap(face) == bitmap(face, "\U000FFFFF"):
                hebrewless = name
                break
        if hebrewless is None:
            self.skipTest("no Latin face measured to lack Hebrew here")

        pilfont._CANDIDATES["latin"] = [hebrewless]
        pilfont.clear_cache()
        hebrew = pilfont.font_for("סרט", 28)
        self.assertNotEqual(bitmap(hebrew), bitmap(hebrew, "\U000FFFFF"),
                            "a Hebrew title still resolves a face without it")


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


class TestSymbolRuns(unittest.TestCase):
    """#713 — the rating star drew as a tofu box on Windows.

    Pillow has no fallback and the Latin face it lands on there is Arial,
    which has no U+2605 (nor U+2713, U+25B6; measured against segoeui,
    tahoma, verdana and calibri too, none of which has it either). So the
    symbol blocks get a face of their own.
    """

    def test_a_star_is_a_symbol_run_not_a_latin_one(self):
        self.assertEqual(pilfont.runs("★ 8.1"),
                         [("symbol", "★ "), ("latin", "8.1")])

    def test_the_meta_line_splits_around_the_rating(self):
        """The string the bug was reported against: components.detail
        builds "year · runtime · rating · genres" and the banner bakes it."""
        self.assertEqual(
            pilfont.runs("1998   ·   R   ·   ★ 8.1   ·   Drama"),
            [("latin", "1998   ·   R   ·   "),
             ("symbol", "★ "),
             ("latin", "8.1   ·   Drama")])

    def test_ordinary_typography_is_not_sent_to_the_symbol_face(self):
        """The cost of over-claiming. Dashes, ellipses, middots, currency,
        maths and letterlike marks are in every Latin face; classifying them
        as symbols would split almost every caption the app draws."""
        for text in ("Fahrenheit 451 — the sequel", "WALL·E", "Naïve…",
                     "Alien³", "™ Studios", "a ≥ b", "€10", "«Léon»"):
            self.assertEqual(pilfont.runs(text), [("latin", text)], text)

    def test_a_symbol_never_hijacks_a_cjk_string(self):
        self.assertEqual(pilfont.runs("★ 進撃"),
                         [("symbol", "★ "), ("cjk", "進撃")])

    def test_a_symbol_does_not_outrank_a_real_script(self):
        """`script_of` picks the ONE face an unsplittable string is drawn
        with, and for an RTL string that is every character of it -- Pillow
        reorders bidi within a single draw call and cannot across several,
        so `draw_text` refuses to split. A star appearing before the Arabic
        must not therefore choose the face: measured, Segoe UI Symbol (the
        Windows symbol face) has no Arabic and no Hebrew at all, so the
        whole genre would draw as boxes.
        """
        self.assertEqual(pilfont.script_of("1998 · ★ 8.1 · دراما"), "arabic")
        self.assertEqual(pilfont.script_of("★ 進撃"), "cjk")
        self.assertEqual(pilfont.script_of("★ ภาพยนตร์"), "thai")

    def test_the_stamp_is_what_lets_a_face_be_asked_what_it_is_for(self):
        """`_run_face`'s contract, in isolation. What it is *for* is
        asserted through `draw_text` in
        `test_a_wrapped_symbol_only_line_is_re_resolved_through_draw_text`,
        because a unit test of the helper cannot show that anything calls
        it."""
        latin = pilfont.font("latin", 28)
        self.assertEqual(getattr(latin, "_jms_script", None), "latin")
        self.assertIs(pilfont._run_face(latin, "symbol"),
                      pilfont.font("symbol", 28))
        self.assertIs(pilfont._run_face(latin, "latin"), latin)

    def test_an_unstamped_face_still_gets_a_real_face_per_run(self):
        """The symmetric direction of the rule below, and the one it must
        NOT be extended to.

        Leaving a hand-built font alone is right for a string that is one
        run: the caller asked for that face and the face can draw it. It is
        wrong for a MIXED string, which is the whole reason this path
        exists -- a Latin face cannot draw the CJK run, and answering "the
        font you passed" for every run puts the tofu back.
        """
        from PIL import Image, ImageDraw, ImageFont

        latin_path = pilfont._resolved.get(("latin", False))
        pilfont.font("latin", 20)                     # ensure it resolved
        latin_path = pilfont._resolved.get(("latin", False))
        if not latin_path:
            self.skipTest("no Latin face resolved on this host")
        home_made = ImageFont.truetype(latin_path, 20)
        self.assertIsNone(getattr(home_made, "_jms_script", None))

        text = "進撃 (2013)"
        cjk = pilfont.font("cjk", 20)
        if cjk is pilfont.font("latin", 20):
            self.skipTest("no separate CJK face installed on this host")

        img = Image.new("L", (300, 40), 0)
        pilfont.draw_text(ImageDraw.Draw(img), (3, 5), text, home_made,
                          fill=255)
        plain = Image.new("L", (300, 40), 0)
        ImageDraw.Draw(plain).text((3, 5), text, font=home_made, fill=255)
        self.assertNotEqual(
            img.tobytes(), plain.tobytes(),
            "every run was drawn with the caller's own face, so the CJK "
            "run is whatever that face does with characters it lacks")

    def test_an_unstamped_face_is_left_exactly_as_it_was_passed(self):
        """A caller who built a face by hand gets that face, whatever the
        run is: this is what keeps the single-run path byte-identical to
        what it drew before any of the script machinery existed."""
        from PIL import ImageFont

        home_made = ImageFont.load_default()
        self.assertIs(pilfont._run_face(home_made, "symbol"), home_made)

    def test_a_symbol_does_not_outrank_hebrew_either(self):
        """Hebrew was the case the "another script won" rule did not cover:
        it mapped to "latin", so nothing outranked the star, while the line
        was still RTL and still drawn with one face -- and Segoe UI Symbol
        has no Hebrew. F33 gave Hebrew a script of its own, so it is caught
        by the same early return as every other script now, rather than by
        the "are there words at all" half. The property is unchanged; the
        answer is a better face than it used to be."""
        self.assertTrue(pilfont.has_rtl("★ סרט"))
        self.assertEqual(pilfont.script_of("★ סרט"), "hebrew")

    def test_a_symbol_is_never_the_answer_for_a_string_with_words_in_it(self):
        """`script_of` picks the face for the *words*, and a symbol is not
        one. It is also the face a caller hands `draw_text` for a wrapped
        or ellipsized SUBSTRING, which the symbol need not have survived
        into -- see `test_a_line_ellipsized_before_the_star...`."""
        self.assertEqual(pilfont.script_of("1998 · ★ 8.1 · Drama"), "latin")
        self.assertEqual(pilfont.script_of("Drama"), "latin")
        # ...and the runs still send the star somewhere that has one.
        self.assertIn(("symbol", "★ "), pilfont.runs("★ 8.1"))

    def test_a_string_that_is_ONLY_symbols_does_answer_the_symbol_face(self):
        """There are no words to protect, and something has to draw it.

        This is not a nicety: `components.placeholder_glyph` answers with
        the first character of a title when no icon fits, and
        `strips._paint_poster` draws that glyph with a bare
        ``ImageDraw.text`` -- no runs, no `_run_face`, just the face
        `font_for` picked. An album called "★" gets a tofu box on Windows
        if this answers "latin", which is #713 again on a second path.
        """
        for text in ("★", "♪", "★ ★ ★", " ▶ "):
            self.assertEqual(pilfont.script_of(text), "symbol", text)

    def test_the_placeholder_glyph_path_resolves_a_face_that_has_it(self):
        """The call `strips._paint_poster` actually makes, end to end."""
        from jellyfin_mpv_shim.mpvtk_browser.components import (
            placeholder_glyph)

        glyph = placeholder_glyph({"Type": "Video", "Name": "★ Picks"})
        self.assertEqual(glyph, "★")
        self.assertEqual(pilfont.script_of(glyph), "symbol")


class TestSymbolFace(unittest.TestCase):
    """That the split actually reaches a face carrying the glyph.

    The bug is invisible on a host whose Latin face already has the star --
    which is every Linux box with DejaVu, i.e. every machine this suite
    normally runs on. So the Latin candidate list is pointed at a real face
    measured to lack U+2605, and the property is asserted against that.
    """

    #: Faces on this box that do not carry U+2605. Verified in the test
    #: rather than trusted: a font package changing under us must skip,
    #: not pass.
    STARLESS = ("/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
                "/usr/share/fonts/truetype/crosextra/Carlito-Regular.ttf",
                "C:\\Windows\\Fonts\\arial.ttf")

    def setUp(self):
        self._saved = dict(pilfont._CANDIDATES)
        pilfont.clear_cache()

    def tearDown(self):
        pilfont._CANDIDATES.clear()
        pilfont._CANDIDATES.update(self._saved)
        pilfont.clear_cache()

    def _bitmap(self, font, text="★"):
        from PIL import Image, ImageDraw

        img = Image.new("L", (60, 48), 0)
        ImageDraw.Draw(img).text((2, 2), text, font=font, fill=255)
        return img.tobytes()

    def _starless(self):
        import os

        from PIL import ImageFont

        for path in self.STARLESS:
            if not os.path.exists(path):
                continue
            font = ImageFont.truetype(path, 28)
            # .notdef against a codepoint nothing assigns: if the star draws
            # the same box, this face is the fixture we want.
            if self._bitmap(font) == self._bitmap(font, "\U000FFFFF"):
                return path
        return None

    def test_the_star_is_not_the_tofu_the_latin_face_would_have_drawn(self):
        starless = self._starless()
        if starless is None:
            self.skipTest("no face measured to lack U+2605 on this host")
        pilfont._CANDIDATES["latin"] = [starless]
        pilfont.clear_cache()
        symbol = pilfont.font("symbol", 28)
        latin = pilfont.font("latin", 28)
        if self._bitmap(symbol) == self._bitmap(symbol, "\U000FFFFF"):
            self.skipTest("no symbol-carrying face installed on this host")
        self.assertNotEqual(self._bitmap(symbol), self._bitmap(latin),
                            "the symbol run drew the Latin face's tofu")

    def test_draw_text_puts_the_star_on_the_symbol_face_AND_nothing_else(self):
        """End to end, through the call the banner actually makes.

        Asserted against an explicit run-by-run rendering rather than
        against "not what the Latin face would have drawn". That weaker
        form passes just as happily against drawing the WHOLE string in the
        symbol face -- which is a different bug (Segoe UI Symbol's digits
        are not Arial's, and it has no Arabic at all) that the weaker
        assertion cannot see. Same construction as the CJK case above.
        """
        from PIL import Image, ImageDraw

        starless = self._starless()
        if starless is None:
            self.skipTest("no face measured to lack U+2605 on this host")
        pilfont._CANDIDATES["latin"] = [starless]
        pilfont.clear_cache()
        symbol, latin = pilfont.font("symbol", 28), pilfont.font("latin", 28)
        if self._bitmap(symbol) == self._bitmap(symbol, "\U000FFFFF"):
            self.skipTest("no symbol-carrying face installed on this host")
        if symbol is latin:
            self.skipTest("this host resolves both to one face")

        text = "2001   ·   ★ 8.1"
        parts = pilfont.runs(text)
        self.assertEqual([script for script, _c in parts],
                         ["latin", "symbol", "latin"],
                         "the premise: this string has three runs")

        got = Image.new("L", (400, 48), 0)
        pilfont.draw_text(ImageDraw.Draw(got), (3, 5), text,
                          pilfont.font_for(text, 28), fill=255)

        want = Image.new("L", (400, 48), 0)
        drawer = ImageDraw.Draw(want)
        faces = [symbol if script == "symbol" else latin
                 for script, _c in parts]
        baseline = 5 + max(f.getmetrics()[0] for f in faces)
        x = 3.0
        for (_script, chunk), face in zip(parts, faces):
            drawer.text((x, baseline), chunk, font=face, fill=255,
                        anchor="ls")
            x += drawer.textlength(chunk, font=face)
        self.assertEqual(got.tobytes(), want.tobytes())

    def test_a_wrapped_symbol_only_line_is_re_resolved_through_draw_text(self):
        """`_run_face` asserted through the call that uses it, not directly.

        The reachable case now that `script_of` protects words: a caption
        whose face was chosen from "★ 8.1" (Latin) and which then wraps
        down to the star alone. One run, and it is not the font's script.
        """
        from PIL import Image, ImageDraw

        starless = self._starless()
        if starless is None:
            self.skipTest("no face measured to lack U+2605 on this host")
        pilfont._CANDIDATES["latin"] = [starless]
        pilfont.clear_cache()
        symbol, latin = pilfont.font("symbol", 28), pilfont.font("latin", 28)
        if symbol is latin:
            self.skipTest("this host resolves both to one face")

        # ...and that the symbol face actually has a star. With no symbol
        # candidate installed, `font("symbol")` falls back down the LATIN
        # list -- which this test has just pointed at a starless face -- so
        # `got` and `want` would agree on identical tofu and the assertion
        # would pass having tested nothing.
        if self._bitmap(symbol) == self._bitmap(symbol, "\U000FFFFF"):
            self.skipTest("no symbol-carrying face installed on this host")

        chosen = pilfont.font_for("★ 8.1", 28)          # -> the Latin face
        self.assertIs(chosen, latin, "the premise: a Latin-stamped font")

        got = Image.new("L", (120, 48), 0)
        pilfont.draw_text(ImageDraw.Draw(got), (3, 5), "★", chosen, fill=255)
        want = Image.new("L", (120, 48), 0)
        ImageDraw.Draw(want).text((3, 5), "★", font=symbol, fill=255)
        self.assertEqual(got.tobytes(), want.tobytes(),
                         "the star kept the Latin face it was handed")
        # ...and measuring agrees with it, or a caption ellipsizes against
        # a width it is not drawn at.
        d = ImageDraw.Draw(Image.new("L", (10, 10)))
        self.assertEqual(pilfont.text_length(d, "★", chosen),
                         d.textlength("★", font=symbol))

    def test_a_wrapped_rtl_line_is_re_resolved_too(self):
        """The symmetric direction of the wrapped-substring repair.

        `has_rtl` makes `draw_text` take a single draw call with the face it
        was handed -- necessary, because Pillow reorders bidi within a call
        and cannot across several. But "the face it was handed" was chosen
        for a longer string: wrap "東京 دراما" and the Arabic-only line
        inherits the CJK face. One draw call is still one draw call with
        the *right* face, so the bypass must re-resolve rather than skip.
        """
        from PIL import Image, ImageDraw

        cjk, arabic = pilfont.font("cjk", 28), pilfont.font("arabic", 28)
        latin = pilfont.font("latin", 28)
        if cjk is latin or arabic is latin or cjk is arabic:
            self.skipTest("this host has no separate CJK and Arabic faces")

        chosen = pilfont.font_for("東京 دراما", 28)
        self.assertIs(chosen, cjk, "the premise: a CJK-stamped font")

        line = "دراما"
        got = Image.new("L", (200, 48), 0)
        pilfont.draw_text(ImageDraw.Draw(got), (3, 5), line, chosen, fill=255)
        want = Image.new("L", (200, 48), 0)
        ImageDraw.Draw(want).text((3, 5), line, font=arabic, fill=255)
        self.assertEqual(got.tobytes(), want.tobytes(),
                         "the Arabic line kept the CJK face")
        d = ImageDraw.Draw(Image.new("L", (10, 10)))
        self.assertEqual(pilfont.text_length(d, line, chosen),
                         d.textlength(line, font=arabic))

    def test_a_line_ellipsized_before_the_star_is_still_drawn_in_Latin(self):
        """The face is chosen from the WHOLE string and then a *substring*
        is drawn with it.

        `components/banner.py` picks `pil_font(text=meta)` for the whole
        meta line, wraps/ellipsizes it to the width, and draws the result;
        `cast.py` picks one face for a synopsis and draws it line by line;
        `strips.py` does the same for a caption. So a line the star did not
        survive into is handed the star's face -- and Segoe UI Symbol's
        Latin is not Arial's, which turns one rating into a different
        typeface for a whole paragraph.
        """
        from PIL import Image, ImageDraw

        starless = self._starless()
        if starless is None:
            self.skipTest("no face measured to lack U+2605 on this host")
        pilfont._CANDIDATES["latin"] = [starless]
        pilfont.clear_cache()
        symbol, latin = pilfont.font("symbol", 28), pilfont.font("latin", 28)
        if symbol is latin:
            self.skipTest("this host resolves both to one face")

        full = "2001   ·   PG-13   ·   ★ 8.1   ·   Drama"
        line = "2001   ·   PG-13   ·   …"      # what wrap_pil hands back
        font = pilfont.font_for(full, 28)      # chosen from the FULL string

        got = Image.new("L", (400, 48), 0)
        pilfont.draw_text(ImageDraw.Draw(got), (3, 5), line, font, fill=255)
        want = Image.new("L", (400, 48), 0)
        ImageDraw.Draw(want).text((3, 5), line, font=latin, fill=255)
        self.assertEqual(got.tobytes(), want.tobytes(),
                         "a Latin-only line was drawn in the symbol face")

    def test_the_latin_runs_are_not_drawn_by_the_symbol_face(self):
        """A guard on the premise of the test above: if the symbol face
        happened to draw digits identically to the Latin one, that
        assertion would pass against a whole-string symbol render too."""
        starless = self._starless()
        if starless is None:
            self.skipTest("no face measured to lack U+2605 on this host")
        pilfont._CANDIDATES["latin"] = [starless]
        pilfont.clear_cache()
        symbol, latin = pilfont.font("symbol", 28), pilfont.font("latin", 28)
        if symbol is latin:
            self.skipTest("this host resolves both to one face")
        if self._bitmap(symbol, "2001") == self._bitmap(latin, "2001"):
            self.skipTest("the two faces draw digits identically here")
        self.assertNotEqual(self._bitmap(symbol, "2001"),
                            self._bitmap(latin, "2001"))


class TestEmojiTable(unittest.TestCase):
    """The classifier half of F31, and the regression it must not cause.

    Everything astral is safe to move by construction -- the CJK catch-all
    that owns it today draws none of it (NotoSansCJK covers 0 of
    U+1F300-1F5FF, msgothic 0 of every emoji block, measured) -- so these
    guard the BMP half, where the codepoints being moved *are* currently
    drawn by a face that has them.
    """

    def test_the_713_marks_are_not_swept_up_as_emoji(self):
        """The whole reason the BMP half of the table is a list of small
        ranges and not the symbol blocks it sits inside.

        Measured: U+2605 is .notdef in BOTH colour faces -- NotoColorEmoji
        and seguiemj -- so calling it emoji is calling it tofu, which is
        exactly the bug #713 fixed.
        """
        for cp in (0x2605, 0x2713, 0x25B6, 0x266A, 0x2764, 0x2600):
            self.assertEqual(pilfont.script_of_char(cp), "symbol",
                             "U+%04X left the symbol face" % cp)

    def test_the_emoji_presentation_marks_do_reach_the_emoji_face(self):
        for cp in (0x2B50, 0x2705, 0x274C, 0x2728, 0x26BD, 0x231A):
            self.assertEqual(pilfont.script_of_char(cp), "emoji",
                             "U+%04X did not reach the emoji face" % cp)

    def test_the_astral_pictographs_leave_the_cjk_catch_all(self):
        for cp in (0x1F3AC, 0x1F600, 0x1F680, 0x1F9E0, 0x1FA79, 0x1F1FA,
                   0x1F7E2):
            self.assertEqual(pilfont.script_of_char(cp), "emoji",
                             "U+%05X is still CJK's" % cp)

    def test_the_astral_symbol_blocks_leave_it_too(self):
        """Found while fixing F31, same catch-all. Measured: Symbols2 draws
        all of these and the CJK face draws none of them."""
        for cp in (0x1F0A1, 0x1F660, 0x1F810, 0x1FA00, 0x1FB00, 0x1F7A0):
            self.assertEqual(pilfont.script_of_char(cp), "symbol",
                             "U+%05X is still CJK's" % cp)

    def test_the_enclosed_alphanumerics_stay_with_the_cjk_face(self):
        """The block U+1F100-1F2FF is *mostly* CJK's and is picked at, not
        moved: U+1F110 is .notdef in both colour faces and drawn by
        NotoSansCJK, while U+1F192 next door is the other way round."""
        self.assertEqual(pilfont.script_of_char(0x1F110), "cjk")
        self.assertEqual(pilfont.script_of_char(0x1F202), "emoji")
        self.assertEqual(pilfont.script_of_char(0x1F192), "emoji")

    def test_no_codepoint_is_claimed_by_two_tables(self):
        """The tables overlap by block -- U+2B50 sits inside the symbol
        range U+2B00-2BFF -- so the ORDER of the tests in `script_of_char`
        is what separates them. That is invisible at the call site and
        would fail silently if a range were ever added to the wrong one.
        """
        symbols = {cp for lo, hi in pilfont._SYMBOL_RANGES
                   for cp in range(lo, hi + 1)}
        symbols |= {cp for lo, hi in pilfont._ASTRAL_SYMBOL_RANGES
                    for cp in range(lo, hi + 1)}
        for lo, hi in pilfont._EMOJI_RANGES:
            for cp in range(lo, hi + 1):
                if cp in symbols:
                    self.assertEqual(
                        pilfont.script_of_char(cp), "emoji",
                        "U+%05X is in both tables and the symbol one won"
                        % cp)

    def test_the_bmp_table_is_only_codepoints_a_colour_face_has(self):
        """The transcription guard.

        The BMP half of `_EMOJI_RANGES` is Unicode's Emoji_Presentation
        list, typed in by hand, and a codepoint wrongly included is a glyph
        moved from a face that draws it to one that does not -- silently,
        and only on the hosts that have the emoji font. So it is checked
        against the font rather than trusted.
        """
        face = self._colour_face()
        if face is None:
            self.skipTest("no colour emoji face installed on this host")
        missing = [cp for lo, hi in pilfont._EMOJI_RANGES
                   for cp in range(lo, hi + 1) if cp < 0x10000
                   and self._tofu(face, chr(cp))]
        self.assertEqual(missing, [],
                         "not drawn by %r: %s"
                         % (pilfont._resolved.get(("emoji", False)),
                            " ".join("U+%04X" % cp for cp in missing)))

    def _colour_face(self):
        """The emoji face, but only when it really is one: `font()` falls
        through to the symbol chain where no emoji font is installed, and
        that face measures nothing about this table."""
        face = pilfont.font("emoji", 28)
        name = pilfont._resolved.get(("emoji", False))
        if name is None or name not in pilfont._CANDIDATES["emoji"]:
            return None
        return face

    def _tofu(self, face, text):
        from PIL import Image, ImageDraw

        def shot(s):
            size = getattr(face, "size", 28)
            img = Image.new("RGBA", (size * 3, size * 3), (0, 0, 0, 0))
            ImageDraw.Draw(img).text((2, 2), s, font=face,
                                     fill=(255, 255, 255, 255),
                                     embedded_color=True)
            return img.tobytes()

        # U+FFFF is a noncharacter: no font maps it, so it is this face's
        # .notdef and anything drawing the same is not drawn at all.
        return shot(text) == shot("￿")


class TestEmojiIsNeverAWholeStringsFace(unittest.TestCase):
    """`script_of` must not answer "emoji", and the reason is metrics.

    Its answer reserves a line's height and picks a whole book's face. A
    colour-emoji face is very often available at one pixel size only --
    NotoColorEmoji at 109 -- so that answer would be five times too tall.
    """

    def test_a_string_of_nothing_but_emoji_answers_the_symbol_face(self):
        self.assertEqual(pilfont.script_of("\U0001F3AC"), "symbol")
        self.assertEqual(pilfont.script_of("\U0001F3AC \U0001F37F"), "symbol")

    def test_an_emoji_does_not_outrank_words_or_a_real_script(self):
        self.assertEqual(pilfont.script_of("\U0001F3AC Movie Night"), "latin")
        self.assertEqual(pilfont.script_of("\U0001F3AC 進撃の巨人"), "cjk")
        self.assertEqual(pilfont.script_of("\U0001F3AC مسلسل"), "arabic")

    def test_the_face_a_caller_gets_is_the_size_it_asked_for(self):
        """The property the rule above exists for, asserted rather than
        inferred: whatever `script_of` answers, the face that answer
        resolves to has to have the metrics of the size requested."""
        for text in ("\U0001F3AC", "\U0001F3AC Movie", "★", "Plain"):
            font = pilfont.font_for(text, 20)
            ascent, descent = font.getmetrics()
            self.assertLess(ascent + descent, 20 * 2,
                            "%r resolved a face with a %dpx body"
                            % (text, ascent + descent))


class TestEmojiRuns(unittest.TestCase):
    def setUp(self):
        from PIL import Image, ImageDraw

        self.img = Image.new("RGBA", (400, 60), (0, 0, 0, 0))
        self.draw = ImageDraw.Draw(self.img)
        self.font = pilfont.font("latin", 20)

    def test_an_emoji_is_its_own_run(self):
        self.assertEqual(
            pilfont.runs("Movie \U0001F3AC 2024"),
            [("latin", "Movie "), ("emoji", "\U0001F3AC"),
             ("latin", " 2024")])

    def test_a_zwj_sequence_stays_in_one_run(self):
        """A run boundary is a separate draw call and shaping does not
        cross one, so splitting at the joiner draws two emoji where the
        font has a single glyph."""
        self.assertEqual(pilfont.runs("\U0001F469‍\U0001F4BB"),
                         [("emoji", "\U0001F469‍\U0001F4BB")])
        self.assertEqual(pilfont.runs("⭐️"),
                         [("emoji", "⭐️")])

    def test_a_space_after_an_emoji_is_not_an_emoji_wide_space(self):
        """Measured, not asserted about run shape: a colour-emoji face
        advances a full em and a bit for U+0020 (135.7 of NotoColorEmoji's
        109px em against DejaVu's 6.4 at 20px), so leaving the space in the
        emoji run put a four-space hole in the middle of every caption
        carrying one."""
        space = self.draw.textlength(" ", font=self.font)
        with_gap = pilfont.text_length(self.draw, "\U0001F3AC A", self.font)
        tight = pilfont.text_length(self.draw, "\U0001F3ACA", self.font)
        # A delta, not places=3. What is compared is the advance of " A"
        # minus "A" against the advance of " " alone, and a shaper does not
        # promise those agree to the 1/64px: measured 5.984375 against 6.0
        # on Windows, where Pillow has no Raqm and lays out glyph by glyph.
        # The bug this guards is a 25px space against a 6px one.
        self.assertAlmostEqual(with_gap - tight, space, delta=0.5)

    def test_measuring_and_drawing_split_the_same_way(self):
        """The two have to agree or a caption is ellipsized against a width
        it is not drawn at. Both go through `_split`; this is the assertion
        that they still do."""
        for text in ("Movie \U0001F3AC 2024", "\U0001F3AC", "★\U0001F3AC",
                     "進撃 \U0001F600 (2013)", "plain text"):
            self.assertAlmostEqual(
                pilfont.text_length(self.draw, text, self.font),
                pilfont.length(text, self.font), places=3,
                msg=repr(text))


class TestFixedStrikeFaces(unittest.TestCase):
    """A face that only loads at its own pixel size.

    `NotoColorEmoji.ttf` is a CBDT bitmap face with a single 109px strike
    and raises OSError at every other size. Adding it to the candidate list
    and stopping there is the fix that changes nothing: `truetype(name, 20)`
    fails, the loop moves on, and the run lands back on a face with no
    emoji in it.
    """

    def _emoji(self, size=20):
        face = pilfont.font("emoji", size)
        name = pilfont._resolved.get(("emoji", False))
        if name is None or name not in pilfont._CANDIDATES["emoji"]:
            self.skipTest("no emoji face installed on this host")
        return face

    def test_the_strike_is_found_rather_than_the_face_being_skipped(self):
        face = self._emoji()
        self.assertEqual(getattr(face, "_jms_size", None), 20)
        self.assertTrue(getattr(face, "_jms_native", None),
                        "the face was not told what size it opened at")

    def test_a_scalable_face_needs_no_scaling(self):
        """The no-op half. Every face that loads at the size asked for --
        which is all of them but the bitmap emoji ones -- must come back
        with a scale of exactly 1.0, or every path below changes for text
        that was never broken."""
        for script in ("latin", "cjk", "symbol", "hebrew", "arabic"):
            self.assertEqual(pilfont._scale_of(pilfont.font(script, 20)), 1.0,
                             "%s picked up a scale" % script)

    def test_an_emoji_is_measured_at_the_size_asked_for(self):
        """A 109px strike measures 135.7px for one emoji. Unscaled, a
        two-emoji caption would be ellipsized to nothing and a banner would
        be laid out around a 270px rating."""
        face = self._emoji()
        from PIL import Image, ImageDraw

        draw = ImageDraw.Draw(Image.new("RGBA", (10, 10)))
        width = pilfont.text_length(draw, "\U0001F3AC",
                                    pilfont.font("latin", 20))
        self.assertLess(width, 20 * 2.5,
                        "one 20px emoji measured %.1fpx" % width)
        self.assertGreater(width, 20 * 0.5)


class TestEmojiDrawing(unittest.TestCase):
    def setUp(self):
        face = pilfont.font("emoji", 20)
        name = pilfont._resolved.get(("emoji", False))
        if name is None or name not in pilfont._CANDIDATES["emoji"]:
            self.skipTest("no emoji face installed on this host")
        self.emoji_face = face
        self.font = pilfont.font("latin", 20)

    def _row_ink(self, text):
        """Which rows of a strip of latin text have ink in them."""
        from PIL import Image, ImageDraw

        img = Image.new("L", (300, 60), 0)
        draw = ImageDraw.Draw(img)
        pilfont.draw_text(draw, (4, 10), text, self.font, fill=255)
        return {y for y in range(60)
                if any(img.getpixel((x, y)) for x in range(4, 60))}

    def test_an_emoji_later_in_the_line_does_not_move_the_words(self):
        """The scaled-metrics property, over the part of the line the emoji
        is not in. A 109px face's ascent picked as the line's would put the
        latin text five lines down; asserting on the emoji's own rows would
        not have caught that, because the emoji moves with it.
        """
        self.assertEqual(self._row_ink("Movie Night"),
                         self._row_ink("Movie Night \U0001F3AC"))

    def _ink_box(self, text, size=20):
        from PIL import Image, ImageDraw

        img = Image.new("RGBA", (400, 300), (0, 0, 0, 0))
        pilfont.draw_text(ImageDraw.Draw(img), (4, 4), text,
                          pilfont.font("latin", size),
                          fill=(255, 255, 255, 255))
        return img.getbbox()

    def test_a_lone_emoji_is_drawn_at_the_size_it_was_asked_for(self):
        """The `_split` exclusion, asserted on the *size* of what lands.

        A one-run string normally goes straight to `draw.text` with the
        face the run resolved -- and for a bitmap-strike emoji face that
        face is 109px, so a tile showing nothing but an emoji drew it five
        times over its caption. Comparing against what the Latin face would
        have drawn does not see this: both answers differ from that one.
        """
        box = self._ink_box("\U0001F3AC")
        self.assertIsNotNone(box, "nothing was drawn at all")
        self.assertLess(box[3] - box[1], 20 * 2,
                        "a 20px emoji drew %dpx tall" % (box[3] - box[1]))

    def test_it_scales_with_the_size_asked_for(self):
        """Over three sizes, because one size cannot tell a scaled face
        from a fixed one that happens to look right."""
        heights = [self._ink_box("\U0001F3AC", size)[3]
                   - self._ink_box("\U0001F3AC", size)[1]
                   for size in (14, 28, 56)]
        self.assertEqual(heights, sorted(heights))
        self.assertLess(heights[0], heights[2],
                        "the emoji is the same size whatever is asked for")

    def test_a_greyscale_plate_gets_a_greyscale_emoji_not_an_exception(self):
        """The branch Linux cannot reach on its own.

        `embedded_color` is refused by Pillow on anything but RGB and RGBA
        (`ValueError: Embedded color supported only in RGB and RGBA
        modes`), and the run only reaches that call when the emoji face
        loads at the size asked for. On this host it does not -- the strike
        sends it through `_draw_scaled` and its own RGBA scratch -- so the
        whole suite was green here while every L-mode draw raised on
        Windows, where seguiemj is COLR-outlined. Forcing a *scalable*
        emoji face is what puts this box on the same path.
        """
        from PIL import Image, ImageDraw, ImageFont

        scalable = None
        for name in ("NotoEmoji-Regular.ttf",
                     "/usr/share/fonts/truetype/noto/NotoEmoji-Regular.ttf",
                     "Symbola.ttf",
                     "/usr/share/fonts/truetype/ancient-scripts/"
                     "Symbola_hint.ttf",
                     "seguiemj.ttf"):
            try:
                ImageFont.truetype(name, 20)
            except OSError:
                continue
            scalable = name
            break
        if scalable is None:
            self.skipTest("no emoji face that loads at an arbitrary size")

        saved = list(pilfont._CANDIDATES["emoji"])
        self.addCleanup(pilfont.clear_cache)
        self.addCleanup(pilfont._CANDIDATES.__setitem__, "emoji", saved)
        pilfont._CANDIDATES["emoji"] = [scalable]
        pilfont.clear_cache()
        self.assertEqual(pilfont._scale_of(pilfont.font("emoji", 20)), 1.0,
                         "the forced face still needs scaling, so this test "
                         "is not on the path it is named after")
        for mode, fill in (("L", 255), ("RGB", (255, 255, 255)),
                           ("RGBA", (255, 255, 255, 255))):
            with self.subTest(mode=mode):
                img = Image.new(mode, (200, 60))
                pilfont.draw_text(ImageDraw.Draw(img), (4, 10),
                                  "Movie \U0001F600", pilfont.font("latin", 20),
                                  fill=fill)
                self.assertIsNotNone(img.getbbox(), "nothing was drawn")

    def test_an_emoji_is_drawn_in_its_own_colours(self):
        """`embedded_color`, which is the difference between an emoji and a
        silhouette of one.

        Whether this host *can* show colour is decided from the face, not
        from the drawing under test: asking the output would let a
        `draw_text` that lost `embedded_color` skip itself, which is how
        this test passed a mutation that removed exactly that.
        """
        from PIL import Image, ImageDraw

        def hues(drawer):
            img = Image.new("RGBA", (80, 60), (0, 0, 0, 0))
            drawer(ImageDraw.Draw(img))
            return {px[:3] for px in img.get_flattened_data() if px[3] > 200}

        native = hues(lambda d: d.text(
            (4, 4), "\U0001F600", font=self.emoji_face,
            fill=(255, 255, 255, 255), embedded_color=True))
        if not any(r != g or g != b for r, g, b in native):
            self.skipTest("the emoji face on this host is monochrome")
        drawn = hues(lambda d: pilfont.draw_text(
            d, (4, 4), "\U0001F600", self.font, fill=(255, 255, 255, 255)))
        self.assertTrue(any(r != g or g != b for r, g, b in drawn),
                        "drawn as a silhouette of the fill colour")

    def test_the_run_after_an_emoji_starts_where_it_was_measured_to(self):
        """Drawing advances by the *scaled* width. Unscaled, everything
        after the first emoji in a caption is drawn off the end of the
        tile -- and the caption still measures as fitting, because
        measuring is the half that was already right."""
        from PIL import Image, ImageDraw

        img = Image.new("L", (400, 60), 0)
        draw = ImageDraw.Draw(img)
        pilfont.draw_text(draw, (4, 10), "\U0001F3AC End", self.font, fill=255)
        columns = [x for x in range(400)
                   if any(img.getpixel((x, y)) for y in range(60))]
        self.assertTrue(columns)
        self.assertLess(
            max(columns),
            4 + pilfont.text_length(draw, "\U0001F3AC End", self.font) + 6)


if __name__ == "__main__":
    unittest.main()
