"""Translations that raise when formatted are kept out of the build.

Four locales ship one today -- `Páxina %(page) de %(total)` and friends -- and
neither compiler notices: `msgfmt` exits 0 and the entry lands in the `.mo`,
where gettext hands it back to be formatted and raise. In three languages that
one string is the reader's bottom bar, so it raised on every repaint.

**These test the filter, not the catalogs.** A test asserting the shipped `.po`
files are clean fails the day a volunteer types a bad placeholder, which is
both inevitable and not a defect in this repo -- and it would fail on a branch
that touched nothing. So the crafted catalog below carries one of every shape,
and the only thing asked of the real ones is that the filter's own output is
clean, which is a property of the filter.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import glob
import os
import sys
import tempfile
import unittest

sys.argv = ["test"]

from tools import msgfmt, po_lint  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

HEADER = '''msgid ""
msgstr ""
"MIME-Version: 1.0\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Content-Transfer-Encoding: 8bit\\n"
'''

#: One of every shape that matters, so a change to the checker has to say
#: which of them it meant to move.
SAMPLE = HEADER + '''
#: a.py
#, python-format
msgid "Page %(page)d of %(total)d"
msgstr "Páxina %(page) de %(total)"

#: a.py
#, python-format
msgid "Page %d of %d"
msgstr "Sida %d av %d"

#: a.py
#, python-format
msgid "%s joined"
msgstr "{0} sa pripojil"

#: a.py
#, python-brace-format
msgid "Quality: {0:0.1f} Mbps"
msgstr "Качество: {0: 0.1f} Мбит/с"

#: a.py
#, no-python-format
msgid "which is 100% on X11"
msgstr "que és 100% a X11, és a dir 100%"

#: a.py
msgid "Plain words"
msgstr "Paraules planes"

#: a.py
#, python-format, fuzzy
msgid "Already flagged %(x)s"
msgstr "Xa marcado %(x)"

#~ msgid "Old and gone %(y)s"
#~ msgstr "Vello %(y)"
'''


def _write(text):
    fd, path = tempfile.mkstemp(suffix=".po")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


class Detection(unittest.TestCase):
    def setUp(self):
        self.path = _write(SAMPLE)
        self.addCleanup(os.unlink, self.path)
        self.lines, self.bad = po_lint.offenders(self.path)
        self.found = {e.msgid: exc for e, exc in self.bad}

    def test_a_dropped_placeholder_is_caught(self):
        """`%(page)` with no conversion character: ValueError at runtime,
        and the actual bug in gl, ms and ar."""
        self.assertIn("Page %(page)d of %(total)d", self.found)

    def test_brace_syntax_in_a_percent_string_is_caught(self):
        """sk's shape. It does not raise on the braces -- it raises because
        nothing consumed the argument."""
        self.assertIn("%s joined", self.found)

    def test_a_correct_translation_is_left_alone(self):
        self.assertNotIn("Page %d of %d", self.found)
        self.assertNotIn("Plain words", self.found)

    def test_a_format_spec_that_only_looks_wrong_is_left_alone(self):
        """The `ru` control, and the reason this formats rather than diffing
        placeholder sets: `{0: 0.1f}` reads like a typo and is valid -- a
        space is a format-spec flag. A placeholder diff drops it."""
        self.assertNotIn("Quality: {0:0.1f} Mbps", self.found)

    def test_a_literal_percent_is_not_a_placeholder(self):
        """`no-python-format` is exactly what xgettext writes for "100% on
        X11", and the app never formats those. Without honouring the flag
        this reported four real locales for a string that cannot raise."""
        self.assertNotIn("which is 100% on X11", self.found)

    def test_what_cannot_reach_a_mo_is_not_reported(self):
        """An entry that is already fuzzy, and an obsolete one. Both are
        excluded by the compilers, so neither can crash anything -- and
        re-reporting them every run would bury the ones that can."""
        self.assertNotIn("Already flagged %(x)s", self.found)
        self.assertNotIn("Old and gone %(y)s", self.found)

    def test_nothing_else_was_reported(self):
        self.assertEqual(sorted(self.found),
                         ["%s joined", "Page %(page)d of %(total)d"])


class Repair(unittest.TestCase):
    """What the compilers see afterwards, which is the point of it."""

    def setUp(self):
        self.path = _write(SAMPLE)
        self.addCleanup(os.unlink, self.path)
        lines, bad = po_lint.offenders(self.path)
        self.catalog = msgfmt.parse("\n".join(po_lint.rewrite(lines, bad)))

    def test_the_crashing_entries_are_out(self):
        """Out of the catalog entirely, so gettext answers with the msgid
        and that one string falls back to English."""
        self.assertNotIn("Page %(page)d of %(total)d", self.catalog)
        self.assertNotIn("%s joined", self.catalog)

    def test_and_everything_else_survives(self):
        """The rest of the locale is untouched -- this is a filter, not a
        gate: failing the build instead would hand Weblate volunteers a way
        to break a release."""
        self.assertEqual(self.catalog["Page %d of %d"], "Sida %d av %d")
        self.assertEqual(self.catalog["Plain words"], "Paraules planes")
        self.assertEqual(self.catalog["which is 100% on X11"],
                         "que és 100% a X11, és a dir 100%")
        self.assertIn("Quality: {0:0.1f} Mbps", self.catalog)

    def test_an_existing_flag_line_keeps_its_flags(self):
        """"#, python-format" becomes "#, python-format, fuzzy" rather than
        being replaced -- a lost `python-format` flag would turn this check
        off for that entry on the next run."""
        lines, bad = po_lint.offenders(self.path)
        out = po_lint.rewrite(lines, bad)
        self.assertIn("#, python-format, fuzzy", out)

    def test_rewriting_twice_changes_nothing_more(self):
        """The build filters every time it compiles. If a second pass added
        a second flag the file would drift with each build."""
        lines, bad = po_lint.offenders(self.path)
        once = po_lint.rewrite(lines, bad)
        path = _write("\n".join(once) + "\n")
        self.addCleanup(os.unlink, path)
        lines2, bad2 = po_lint.offenders(path)
        self.assertEqual(bad2, [])
        self.assertEqual(po_lint.rewrite(lines2, bad2), lines2)


class TheShippedCatalogs(unittest.TestCase):
    def test_the_filters_own_output_is_clean(self):
        """A fixpoint, not a cleanliness assertion: this stays green when a
        volunteer lands a broken string tomorrow, and fails only if the
        filter cannot actually remove what it found."""
        paths = sorted(glob.glob(os.path.join(
            ROOT, "jellyfin_mpv_shim", "messages", "*", "LC_MESSAGES",
            "base.po")))
        self.assertTrue(paths, "no catalogs found")
        for path in paths:
            lines, bad = po_lint.offenders(path)
            if not bad:
                continue
            cleaned = _write("\n".join(po_lint.rewrite(lines, bad)) + "\n")
            self.addCleanup(os.unlink, cleaned)
            _lines, still = po_lint.offenders(cleaned)
            self.assertEqual(
                [e.msgid for e, _x in still], [],
                "%s still raises after filtering" % os.path.basename(
                    os.path.dirname(os.path.dirname(path))))


if __name__ == "__main__":
    unittest.main()
