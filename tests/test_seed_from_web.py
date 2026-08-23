"""seed_from_jellyfin_web's placeholder conversion and flag handling.

gettext keys on the English exactly, so our ``"%d episodes"`` never matched
jellyfin-web's ``"{0} episodes"`` and five strings we had taken from them word
for word could not be seeded in any language. The seeder compares in their
notation and converts their translation back into ours.

The conversion is the part worth testing rather than asserting by eye: it
writes into 86 catalogues at once, and its worst failure -- a positional
argument that comes back in the other order -- produces a string that formats
without error and reads perfectly while naming the wrong thing.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import seed_from_jellyfin_web as seed                           # noqa: E402


class BraceFormTest(unittest.TestCase):
    def test_a_positional_conversion_becomes_an_index(self):
        self.assertEqual(seed.brace_form("%d episodes"),
                         ("{0} episodes", ["%d"]))

    def test_several_are_numbered_in_order(self):
        self.assertEqual(seed.brace_form("%s of %d"),
                         ("{0} of {1}", ["%s", "%d"]))

    def test_a_named_conversion_keeps_its_spec(self):
        self.assertEqual(seed.brace_form("Channels %(from)d-%(to)d"),
                         ("Channels {0}-{1}", ["%(from)d", "%(to)d"]))

    def test_a_literal_percent_is_not_an_argument(self):
        # "%%" is one percent sign once formatted, so their English has one.
        self.assertEqual(seed.brace_form("100%% of %d"),
                         ("100% of {0}", ["%d"]))

    def test_a_plain_string_converts_to_itself(self):
        # Returned rather than None so a caller can look it up either way.
        self.assertEqual(seed.brace_form("Play"), ("Play", []))

    def test_a_mixed_msgid_is_refused(self):
        # The index-to-spec mapping would be a guess, and we have no such
        # string to check the guess against.
        self.assertIsNone(seed.brace_form("%(name)s has %d"))


class PrintfFormTest(unittest.TestCase):
    def test_an_index_comes_back_as_our_conversion(self):
        # %d, not %s: the spec is taken from OUR msgid, so an int stays an int.
        self.assertEqual(seed.printf_form("{0} Folgen", ["%d"]), "%d Folgen")

    def test_a_literal_percent_is_doubled(self):
        # The result is about to be a format string; a bare % would raise
        # ValueError, or worse, silently eat the next character.
        self.assertEqual(seed.printf_form("{0} % fertig", ["%d"]),
                         "%d %% fertig")

    def test_order_is_preserved(self):
        self.assertEqual(seed.printf_form("{0} von {1}", ["%s", "%d"]),
                         "%s von %d")

    def test_a_reordered_positional_pair_is_refused(self):
        # THE reason this file exists. "%s of %s" is filled from a tuple in
        # occurrence order, so accepting this would swap the arguments in a
        # string that still formats and still reads.
        self.assertIsNone(seed.printf_form("{1} van {0}", ["%s", "%d"]))

    def test_a_reordered_named_pair_is_allowed(self):
        # Named conversions carry their own identity, so moving them is
        # exactly what a translator is entitled to do.
        self.assertEqual(
            seed.printf_form("{1}-{0} Kanäle", ["%(from)d", "%(to)d"]),
            "%(to)d-%(from)d Kanäle")

    def test_a_dropped_argument_is_refused(self):
        self.assertIsNone(seed.printf_form("etwas", ["%d"]))

    def test_a_repeated_argument_is_refused(self):
        # Legal printf, but not something to write unreviewed into 86 files.
        self.assertIsNone(seed.printf_form("{0} van {0}", ["%d"]))

    def test_an_index_we_do_not_have_is_refused(self):
        self.assertIsNone(seed.printf_form("{0} of {1}", ["%d"]))


class RoundTripTest(unittest.TestCase):
    def test_our_english_survives_the_round_trip(self):
        """The English is its own translation, so it must come back unchanged.

        Cheap, and it is the case a reordering bug cannot hide in: if the
        pair does not round-trip, nothing downstream of it is trustworthy.
        """
        for msgid in ("%d episodes", "%s of %d", "Channels %(from)d-%(to)d",
                      "100%% of %d", "Play"):
            with self.subTest(msgid):
                brace, specs = seed.brace_form(msgid)
                self.assertEqual(seed.printf_form(brace, specs), msgid)


class PlaceholderGuardStillApplies(unittest.TestCase):
    def test_placeholders_agree_after_conversion(self):
        """The seeder keeps its original guard behind the new conversion.

        Two independent checks of the same property is deliberate: this one
        predates the conversion and covers the exact-match path, which the
        conversion never touches.
        """
        brace, specs = seed.brace_form("%d items")
        got = seed.printf_form("{0} Elemente", specs)
        self.assertEqual(seed.placeholders("%d items"),
                         seed.placeholders(got))


if __name__ == "__main__":
    unittest.main()


def _entry(*lines):
    return seed.Entry(list(lines))


class SeedableTest(unittest.TestCase):
    def test_an_untranslated_entry_is_seedable(self):
        e = _entry('msgid "Play"', 'msgstr ""')
        self.assertTrue(e.seedable())

    def test_a_volunteers_translation_is_not(self):
        e = _entry('msgid "Play"', 'msgstr "Jouer"')
        self.assertFalse(e.seedable())

    def test_a_plain_fuzzy_entry_is_not(self):
        # msgmerge's own guess, with its #| note. Worth more than ours.
        e = _entry("#, fuzzy", '#| msgid "Playlist"',
                   'msgid "Play"', 'msgstr "Liste de lecture"')
        self.assertFalse(e.seedable())

    def test_one_of_our_own_fuzzy_seeds_is(self):
        e = _entry(seed.MARKER + "Play", "#, fuzzy",
                   'msgid "Play"', 'msgstr "Jouer"')
        self.assertEqual(e.seeded_key, "Play")
        self.assertTrue(e.seedable())

    def test_one_of_our_seeds_a_volunteer_has_accepted_is_not(self):
        # No fuzzy flag: somebody signed off on it, so it is theirs now.
        e = _entry(seed.MARKER + "Play", 'msgid "Play"', 'msgstr "Jouer"')
        self.assertFalse(e.seedable())

    def test_an_obsolete_entry_is_never_touched(self):
        e = _entry('#~ msgid "Play"', '#~ msgstr ""')
        self.assertFalse(e.seedable())


class MarkerIsCurrentTest(unittest.TestCase):
    """The guard between a --merge sweep and a seed run.

    msgmerge carries translator comments onto the entry it fuzzy-matches, so
    after a reworded string our MARKER can end up above a msgid it never
    described. Promoting from that note ships the old string's translation
    under the new English.
    """

    EN = {"Play": "Play", "LabelDroppedFrames": "Dropped frames",
          "EpisodeCount": "{0} episodes"}

    def test_a_note_naming_our_own_msgid_is_current(self):
        e = _entry(seed.MARKER + "Play", 'msgid "Play"', 'msgstr "Jouer"')
        self.assertTrue(seed.marker_is_current(e, self.EN))

    def test_a_note_msgmerge_moved_is_not(self):
        # What the sweep produces: 'Dropped frames' was reworded to
        # 'Blend Frames' and its translation came along, fuzzy, with the note.
        e = _entry(seed.MARKER + "LabelDroppedFrames", "#, fuzzy",
                   'msgid "Blend Frames"', 'msgstr "Images perdues"')
        self.assertFalse(seed.marker_is_current(e, self.EN))

    def test_a_key_jellyfin_web_has_retired_is_not(self):
        e = _entry(seed.MARKER + "LabelGone", 'msgid "Play"', 'msgstr "Jouer"')
        self.assertFalse(seed.marker_is_current(e, self.EN))

    def test_their_notation_is_what_the_comparison_uses(self):
        # Our "%d episodes" is their "{0} episodes"; the seed was legitimate
        # and the note still describes this string.
        e = _entry(seed.MARKER + "EpisodeCount", "#, fuzzy",
                   'msgid "%d episodes"', 'msgstr "%d Folgen"')
        self.assertTrue(seed.marker_is_current(e, self.EN))


class SeedFlagTest(unittest.TestCase):
    def test_seeding_clears_the_fuzzy_flag_by_default(self):
        e = _entry(seed.MARKER + "Old", "#, fuzzy",
                   'msgid "Play"', 'msgstr "Jouer"')
        e.seed("Jouer", "Play")
        self.assertNotIn("#, fuzzy", e.lines)
        self.assertIn(seed.MARKER + "Play", e.lines)
        self.assertIn('msgstr "Jouer"', e.lines)

    def test_other_flags_survive_it(self):
        """python-format in particular: msgfmt uses it to CHECK the

        placeholders, so dropping it while writing a format string would
        disable the check on exactly the entries that just changed.
        """
        e = _entry("#, fuzzy, python-format",
                   'msgid "%d items"', 'msgstr ""')
        e.seed("%d Einträge", "ItemCount")
        self.assertIn("#, python-format", e.lines)
        self.assertNotIn("#, fuzzy, python-format", e.lines)

    def test_fuzzy_true_still_marks_it(self):
        e = _entry('msgid "Play"', 'msgstr ""')
        e.seed("Jouer", "Play", fuzzy=True)
        self.assertIn("#, fuzzy", e.lines)

    def test_the_flag_line_stays_above_the_msgid(self):
        # msgfmt rejects a "#," between msgid and msgstr, and has.
        e = _entry('msgid "Play"', 'msgstr ""')
        e.seed("Jouer", "Play", fuzzy=True)
        self.assertLess(e.lines.index("#, fuzzy"),
                        e.lines.index('msgid "Play"'))

    def test_a_previous_runs_note_is_replaced_not_stacked(self):
        e = _entry(seed.MARKER + "Old", 'msgid "Play"', 'msgstr ""')
        e.seed("Jouer", "New")
        self.assertEqual([l for l in e.lines if l.startswith(seed.MARKER)],
                         [seed.MARKER + "New"])
