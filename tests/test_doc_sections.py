"""Every doc SECTION cited in the tree points at a section that exists.

`test_doc_pointers` checks that the cited file exists; a section number
inside it is unchecked, and section numbers are what actually move. A doc
gets a new section in the middle, everything below renumbers, and roughly a
hundred citations across the package now name the wrong place -- while still
pointing at a file that is very much there, so nothing complains.

Two citation forms are in use and both are read: ``docs/foo.md section 3.2``
and ``docs/foo.md §3.2``.

**A sub-number is only checked when the doc actually has subsection
headings under that top-level section.** `mpvtk/GUIDE.md` §6 has none, and
"§6.3" there means item 3 of the numbered list inside section 6 -- a real
reference to a real place, in a doc that never promised `### 6.3` exists.
Checking it as a heading would fail a citation that is correct.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: A doc path (as `test_doc_pointers` matches one) followed by a section
#: number. Written with a character class rather than the literal section
#: sign twice so the pattern stays legible next to the `section` spelling.
CITE = re.compile(
    r'(?<![\w./-])((?:[A-Za-z_][\w-]*/)+[A-Za-z_][\w.-]*\.md)'
    r'[`\'"\s,]{0,3}(?:§|sections?\s+|sec\.\s+)([0-9]+(?:\.[0-9]+)*)'
)

HEADING = re.compile(r'^#{1,6}\s+([0-9]+(?:\.[0-9]+)*)[.)]?\s', re.M)

SKIP_DIRS = (".git/",)


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out
            if f.endswith((".py", ".md", ".sh", ".lua"))
            and not f.startswith(SKIP_DIRS)]


def _resolve(cited, citing_rel):
    """The cited doc, looked up the way `test_doc_pointers` looks it up."""
    here = os.path.join(ROOT, os.path.dirname(citing_rel))
    bases = [ROOT]
    while here.startswith(ROOT):
        bases.append(here)
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    for base in bases:
        path = os.path.join(base, cited)
        if os.path.exists(path):
            return path
    return None


class DocSectionsTest(unittest.TestCase):
    def setUp(self):
        self._headings = {}

    def _headings_of(self, path):
        if path not in self._headings:
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                self._headings[path] = None
            else:
                self._headings[path] = set(HEADING.findall(text))
        return self._headings[path]

    def test_every_cited_section_exists(self):
        dead = []
        checked = 0
        for rel in _tracked():
            try:
                with open(os.path.join(ROOT, rel), encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for cited, section in set(CITE.findall(text)):
                path = _resolve(cited, rel)
                if path is None:
                    continue        # test_doc_pointers owns that failure
                headings = self._headings_of(path)
                if not headings:
                    continue        # a doc that does not number its headings
                checked += 1
                if section in headings:
                    continue
                top = section.split(".")[0]
                if top not in headings:
                    dead.append((rel, cited, section, "no section " + top))
                elif "." in section and any(
                        h.startswith(top + ".") for h in headings):
                    dead.append((rel, cited, section,
                                 "section %s is subdivided, and not that far"
                                 % top))
        self.assertFalse(dead, "citations naming a section that is not there:\n"
                         + "\n".join("  %s -> %s section %s (%s)" % d
                                     for d in sorted(dead)))
        # A guard that resolves nothing passes for the wrong reason.
        self.assertGreater(checked, 100)


if __name__ == "__main__":
    unittest.main()
