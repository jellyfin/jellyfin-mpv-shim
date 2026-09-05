"""The fallback .po compiler has to agree with GNU msgfmt, on every catalog.

``tools/msgfmt.py`` runs instead of GNU ``msgfmt`` wherever gettext is absent,
which today is the Windows ARM64 CI runner. Nothing downstream would notice it
being subtly wrong: a catalog missing entries still loads, and the app just
shows English. So the check is not "does it produce a .mo" but "does it produce
the same translations as the real tool", asserted against all 86 real catalogs
rather than a fixture, because the interesting inputs (msgctxt, fuzzy entries
seeded from jellyfin-web, obsolete blocks) are all in there already.

Skipped where GNU msgfmt is not installed -- there is nothing to compare to.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import gettext
import io
import os
import shutil
import subprocess
import tempfile
import unittest

from tools.msgfmt import PoError, compile_po, parse

MESSAGES = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jellyfin_mpv_shim",
    "messages",
)

MSGFMT = shutil.which("msgfmt")


def catalog_of(data: bytes) -> dict:
    """Read a .mo back the way the app does, via the stdlib."""
    return gettext.GNUTranslations(io.BytesIO(data))._catalog


def po_files():
    for root, _, files in os.walk(MESSAGES):
        for name in files:
            if name.endswith(".po"):
                yield os.path.join(root, name)


class TestAgainstGnuMsgfmt(unittest.TestCase):
    @unittest.skipIf(MSGFMT is None, "GNU msgfmt is not installed")
    def test_every_catalog_matches_gnu_msgfmt(self):
        paths = sorted(po_files())
        self.assertTrue(paths, "no .po files found to compare")

        with tempfile.TemporaryDirectory() as work:
            for path in paths:
                with self.subTest(catalog=os.path.relpath(path, MESSAGES)):
                    reference = os.path.join(work, "reference.mo")
                    subprocess.run(
                        [MSGFMT, path, "-o", reference],
                        check=True,
                        capture_output=True,
                    )
                    with open(reference, "rb") as handle:
                        expected = catalog_of(handle.read())

                    with open(path, "r", encoding="utf-8") as handle:
                        actual = catalog_of(compile_po(handle.read()))

                    self.assertEqual(expected, actual)

    @unittest.skipIf(MSGFMT is None, "GNU msgfmt is not installed")
    def test_the_template_itself_matches(self):
        """base.pot is all-untranslated, so both should emit only a header."""
        pot = os.path.join(MESSAGES, "base.pot")
        with tempfile.TemporaryDirectory() as work:
            reference = os.path.join(work, "reference.mo")
            subprocess.run(
                [MSGFMT, pot, "-o", reference], check=True, capture_output=True
            )
            with open(reference, "rb") as handle:
                expected = catalog_of(handle.read())
        with open(pot, "r", encoding="utf-8") as handle:
            self.assertEqual(expected, catalog_of(compile_po(handle.read())))


class TestParsing(unittest.TestCase):
    """The rules that decide whether an entry reaches the .mo at all.

    These are asserted directly as well as through the comparison above,
    because which of them a given catalog happens to exercise is an accident
    of what the translators have done this month.
    """

    def test_untranslated_entries_are_dropped(self):
        self.assertEqual(parse('msgid "Play"\nmsgstr ""\n'), {})

    def test_fuzzy_entries_are_dropped(self):
        source = '#, fuzzy\nmsgid "Play"\nmsgstr "Jouer"\n'
        self.assertEqual(parse(source), {})

    def test_a_fuzzy_entry_with_a_context_is_dropped_too(self):
        # The flag line precedes msgctxt, so an entry boundary drawn at the
        # msgctxt keyword loses it. Four real catalogs have exactly this
        # shape, and every one of them leaked a fuzzy translation.
        source = '#, fuzzy\nmsgctxt "button"\nmsgid "Record"\nmsgstr "Enregistrer"\n'
        self.assertEqual(parse(source), {})

    def test_entries_run_together_without_a_blank_line(self):
        source = 'msgid "Play"\nmsgstr "Jouer"\nmsgid "Stop"\nmsgstr "Arrêter"\n'
        self.assertEqual(parse(source), {"Play": "Jouer", "Stop": "Arrêter"})

    def test_a_fuzzy_header_is_kept(self):
        # seed_from_jellyfin_web.py marks what it writes fuzzy, and msgmerge
        # marks the header fuzzy routinely; dropping it would take the
        # charset and plural rule with it.
        source = '#, fuzzy\nmsgid ""\nmsgstr "Content-Type: text/plain\\n"\n'
        self.assertEqual(parse(source), {"": "Content-Type: text/plain\n"})

    def test_obsolete_entries_are_dropped(self):
        source = '#~ msgid "Gone"\n#~ msgstr "Parti"\n\nmsgid "Play"\nmsgstr "Jouer"\n'
        self.assertEqual(parse(source), {"Play": "Jouer"})

    def test_context_is_part_of_the_key(self):
        source = (
            'msgctxt "button"\nmsgid "Record"\nmsgstr "Enregistrer"\n'
            '\nmsgctxt "picker"\nmsgid "Record"\nmsgstr "Enregistrement"\n'
        )
        self.assertEqual(
            parse(source),
            {
                "button\x04Record": "Enregistrer",
                "picker\x04Record": "Enregistrement",
            },
        )

    def test_multiline_strings_are_concatenated(self):
        source = 'msgid ""\n"Play "\n"this"\nmsgstr ""\n"Jouer "\n"ceci"\n'
        self.assertEqual(parse(source), {"Play this": "Jouer ceci"})

    def test_plural_forms_are_joined_with_nul(self):
        source = (
            'msgid "%d item"\nmsgid_plural "%d items"\n'
            'msgstr[0] "%d élément"\nmsgstr[1] "%d éléments"\n'
        )
        self.assertEqual(
            parse(source), {"%d item\x00%d items": "%d élément\x00%d éléments"}
        )

    def test_escapes_are_resolved(self):
        source = r'msgid "a\tb"' + "\n" + r'msgstr "c\\d\"e\n"' + "\n"
        self.assertEqual(parse(source), {"a\tb": 'c\\d"e\n'})

    def test_a_duplicate_message_is_an_error(self):
        source = 'msgid "Play"\nmsgstr "A"\n\nmsgid "Play"\nmsgstr "B"\n'
        with self.assertRaises(PoError):
            parse(source)

    def test_an_unknown_keyword_is_an_error(self):
        with self.assertRaises(PoError):
            parse('msgwhat "Play"\n')


class TestGeneration(unittest.TestCase):
    def test_keys_are_sorted_as_bytes_not_as_text(self):
        """A reader with no hash table binary-searches the encoded keys.

        Sorting the str keys instead orders by code point, which differs from
        UTF-8 byte order for anything outside the BMP -- and a mis-sorted
        table makes a C gettext miss entries it should find while Python's,
        which scans, still sees them all. That asymmetry is why this is
        asserted on the bytes rather than left to the comparison test.
        """
        data = compile_po(
            'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
            '\nmsgid "\U0001F600"\nmsgstr "a"\n\nmsgid "Ａ"\nmsgstr "b"\n'
        )
        # Both keys survived, so this is about their order, not their presence.
        self.assertEqual({"", "\U0001F600", "Ａ"}, set(catalog_of(data)))
        # U+FF21 encodes to EF BC A1 and U+1F600 to F0 9F 98 80, so byte order
        # puts FF21 first while code-point order puts it second.
        self.assertLess(
            data.find("Ａ".encode("utf-8")), data.find("\U0001F600".encode("utf-8"))
        )

    def test_round_trips_through_the_stdlib_reader(self):
        data = compile_po(
            'msgid ""\nmsgstr "Content-Type: text/plain; charset=UTF-8\\n"\n'
            '\nmsgid "Play"\nmsgstr "Jouer"\n'
        )
        self.assertEqual(catalog_of(data)["Play"], "Jouer")


if __name__ == "__main__":
    unittest.main()
