"""Searching the real settings tables.

`tests/test_shell_settings.py` covers the screen -- that the box appears,
that results are editable, that typing repaints. This covers the corpus:
whether the queries a real user types actually find the setting they are
looking for, against the labels and notes this app really ships.

That distinction matters because the failure mode of a search box is not an
exception, it is a query returning nothing while the setting sits two tabs
away. Nothing about that is visible from the code, so the cases are written
down as cases.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import config                # noqa: E402


def found(query):
    """Every key ``query`` matches, flattened out of the groups."""
    return {key for _tab, _title, keys in config.search(query) for key in keys}


class VocabularyTest(unittest.TestCase):
    """The words people type, and the settings they mean by them.

    Every case here is a word that is **not** in the setting's label, which
    is the whole argument for searching the notes as well: a label is two or
    three words chosen before anyone knew what users would call the thing.
    """

    #: Words that appear in the setting's **note** and nowhere in its label.
    NOTE_CASES = [
        # what the user types            what they are looking for
        ("gradient",                     "deband"),
        ("anime",                        "deband"),
        ("buffering",                    "network_buffer"),
        ("judder",                       "motion_interpolation"),
        ("interlaced",                   "deinterlace_auto"),
        # "Ends at" is not in the label either, and it is what somebody
        # looking for the player-controls half of this setting has actually
        # seen on screen.
        ("ends at",                      "clock_12h"),
    ]

    #: Words the label already carries. Kept as cases anyway because they
    #: are what people type, and listed apart so the guard below stays
    #: meaningful -- "banding" reaching `deband` proves nothing about
    #: searching notes, since it is a substring of "Debanding".
    LABEL_CASES = [
        ("banding",                      "deband"),
        ("controller",                   "input_gamepad"),
        ("tone mapping",                 "tone_mapping"),
        ("12 hour",                      "clock_12h"),
    ]

    def test_the_words_people_type_find_the_setting(self):
        for query, key in self.NOTE_CASES + self.LABEL_CASES:
            with self.subTest(query=query):
                self.assertIn(key, found(query),
                              "%r does not find %s" % (query, key))

    def test_the_note_cases_really_do_depend_on_the_note(self):
        """Guards the test above. If one of those words migrates elsewhere in
        the haystack, that case stops testing the notes and starts testing
        something already covered -- passing for a reason it was not written
        for, which is how a suite quietly loses coverage.

        Asked of the haystack with the note removed rather than of the label
        and key alone: the corpus also folds in the group title, the enum
        labels and the search-only aliases, and a case that started matching
        via any of those would otherwise slip past this guard.
        """
        titles = {k: title for _tab, title, keys in
                  [(t, ti, ks) for t in config.FORM_TABS
                   for ti, ks in config.sections(t)]
                  for k in keys}
        for query, key in self.NOTE_CASES:
            with self.subTest(query=query):
                without = config.search_haystack(
                    key, titles.get(key, ""), include_note=False)
                self.assertNotIn(
                    query, without,
                    "%r now matches %s without its note; move this case to "
                    "LABEL_CASES" % (query, key))

    def test_an_alias_is_search_only_and_not_shown_to_the_user(self):
        """`SEARCH_ALIASES` carries the words people type that the prose does
        not say -- "bitrate" for a setting labelled kbps. They must not leak
        into the note, whose job is to explain the setting to somebody
        already looking at it."""
        for key, alias in config.SEARCH_ALIASES.items():
            with self.subTest(setting=key):
                note = (config.NOTES.get(key) or "").lower()
                for word in alias.split():
                    self.assertNotIn(word, note,
                                     "%r is in %s's note, so the alias is "
                                     "redundant" % (word, key))

    def test_every_alias_actually_finds_its_setting(self):
        """An alias that does not reach its own setting is dead weight that
        reads as coverage.

        Only for keys the form is currently showing. Search is built on
        `sections()`, so a hidden control is legitimately unfindable -- and
        which of the tray pair is hidden (and therefore whether
        `start_minimized` is offered at all) depends on whether this machine
        has a system tray. Asserting on the hidden ones would make this pass
        or fail on the desktop rather than on the code.
        """
        shown = {k for tab in config.FORM_TABS
                 for _title, keys in config.sections(tab) for k in keys}
        checked = 0
        for key, alias in config.SEARCH_ALIASES.items():
            if key not in shown:
                continue
            for word in alias.split():
                with self.subTest(setting=key, word=word):
                    self.assertIn(key, found(word),
                                  "%r does not find %s" % (word, key))
                    checked += 1
        self.assertTrue(checked, "no alias was checked on this machine")

    def test_matching_is_substring_and_therefore_directional(self):
        """Worth pinning because it is the one surprising thing about the
        corpus, and the reason a note is sometimes worded around a search.
        A query word has to appear IN the haystack: "buffer" finds a note
        saying "buffering", and "buffering" does NOT find one saying only
        "buffer". That is why `network_buffer`'s note says "buffering" in as
        many words rather than leaving it to the reader.
        """
        self.assertIn("network_buffer", found("buffer"))
        self.assertIn("network_buffer", found("buffering"))
        self.assertIn("buffering", (config.NOTES["network_buffer"] or "").lower())


class MatchingTest(unittest.TestCase):
    def test_a_key_is_findable_by_its_own_name(self):
        """The docs, the issue tracker and conf.json all name settings this
        way, so somebody arriving from any of them types the key."""
        self.assertIn("auto_download_lookahead", found("auto_download_lookahead"))

    def test_underscores_are_not_required(self):
        self.assertIn("auto_download_lookahead", found("download lookahead"))

    def test_matching_is_case_insensitive(self):
        self.assertEqual(found("Debanding"), found("debanding"))

    def test_every_word_must_match(self):
        """AND, not OR. Notes here run to eighty words, so OR returns most
        of the form for any two common words."""
        self.assertIn("deband", found("deband gradient"))
        self.assertFalse(found("deband kumquat"))

    def test_an_empty_query_finds_nothing_rather_than_everything(self):
        """The renderer only calls this with a non-empty query, but "no
        query" resolving to "every setting" is one refactor away from
        rendering the entire form as a search result."""
        for query in ("", "   ", None):
            with self.subTest(query=query):
                self.assertEqual(config.search(query), [])

    def test_results_carry_the_tab_and_group_they_came_from(self):
        for tab, title, keys in config.search("deband"):
            self.assertIn(tab, config.FORM_TABS)
            self.assertTrue(title)
            self.assertTrue(keys)


class CoverageTest(unittest.TestCase):
    def test_every_setting_the_form_offers_can_be_found(self):
        """Searching a setting's own label must return it. A key whose label
        does not match itself is unreachable by search however it is
        spelled, and there is no way to notice that by looking at it.

        Runs over `sections()` rather than the schema, so a control the form
        is currently hiding is not counted -- see the next test.
        """
        for tab in config.FORM_TABS:
            for title, keys in config.sections(tab):
                for key in keys:
                    with self.subTest(setting=key):
                        self.assertIn(key, found(config.label_for(key)))

    def test_a_hidden_control_is_not_findable(self):
        """Search is built on `sections()`, not on the schema, so a setting
        the form refuses to draw cannot be found either. Finding one would
        be a result that leads nowhere: `close_to_tray` on a machine with no
        tray, or the passthrough toggles the selected audio mode cannot
        carry.

        Asserted against whichever of the tray pair is currently hidden, so
        this is a real check on both kinds of machine rather than one that
        only means something on the developer's.
        """
        shown = {k for tab in config.FORM_TABS
                 for _title, keys in config.sections(tab) for k in keys}
        hidden = [k for k in config.TRAY_DEPENDENT if k not in shown]
        self.assertTrue(hidden, "neither tray setting is hidden; this "
                                "machine cannot exercise the rule")
        for key in hidden:
            with self.subTest(setting=key):
                self.assertNotIn(key, found(config.label_for(key)))


if __name__ == "__main__":
    unittest.main()
