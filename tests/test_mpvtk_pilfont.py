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
