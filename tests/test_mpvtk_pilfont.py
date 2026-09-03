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
        """The case the "another script won" rule does not cover: Hebrew
        maps to "latin" here (the Latin face has it), so nothing outranks
        the star -- and the line is still RTL and still drawn with one
        face. Segoe UI Symbol has no Hebrew, so this must answer the face
        that does."""
        self.assertTrue(pilfont.has_rtl("★ סרט"))
        self.assertEqual(pilfont.script_of("★ סרט"), "latin")

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
