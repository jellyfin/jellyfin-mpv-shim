"""Every doc path cited in the tree points at a file that exists.

A comment saying "see docs/X.md" costs nothing to write and silently
stops being true the moment X moves. `docs/archive/` collected three
files and left **eighteen** citations of the old paths behind, thirteen
of them in shipped package code and two inside assertion messages, so a
failing test told you to read a file that was not there.

Cheap to check and impossible to notice by hand, which is the whole
argument for doing it mechanically. Deliberately narrow: it matches paths
that look like repo documents, not prose, not URLs, and not the
deleted-doc citations that exist precisely to say the doc is gone.
"""

import os
import re
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: `word/word.md` or `word.md` with at least one directory-ish prefix.
#: Anchored to a path separator so bare prose like "the MIGRATION.md
#: write-up" is not a citation -- those are handled by NAMED_AS_GONE.
CITE = re.compile(r'(?<![\w./-])((?:[A-Za-z_][\w-]*/)+[A-Za-z_][\w.-]*\.md)')

#: Documents that are GONE and are cited to say so. A pointer that
#: explains its own absence is not a dead pointer.
NAMED_AS_GONE = {"MIGRATION.md", "PARITY.md", "HEADLESS.md"}

#: Documents belonging to ANOTHER repository, named with the repo they
#: live in. Not ours to keep in step, and their absence here is not rot.
EXTERNAL = {"COLLECTION_XML_BUGS.md": "stdjflib"}

SKIP_DIRS = (".git/", "docs/archive/")


def _tracked():
    out = subprocess.run(["git", "ls-files"], cwd=ROOT,
                         capture_output=True, text=True).stdout.split()
    return [f for f in out
            if f.endswith((".py", ".md", ".sh", ".lua"))
            and not f.startswith(SKIP_DIRS)]


class DocPointersTest(unittest.TestCase):
    def test_every_cited_document_exists(self):
        dead = []
        for rel in _tracked():
            path = os.path.join(ROOT, rel)
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
            except (OSError, UnicodeDecodeError):
                continue
            for cited in set(CITE.findall(text)):
                if os.path.basename(cited) in NAMED_AS_GONE:
                    continue
                if os.path.basename(cited) in EXTERNAL:
                    continue
                # Resolved against the repo root, against the citing
                # file's directory, and against every directory ABOVE it
                # -- `mpvtk/GUIDE.md` cited from
                # `jellyfin_mpv_shim/mpvtk_browser/app.py` means the
                # sibling package, which is how the tree already writes
                # it and is perfectly legible to a reader.
                here = os.path.dirname(path)
                bases = [ROOT]
                while here.startswith(ROOT):
                    bases.append(here)
                    parent = os.path.dirname(here)
                    if parent == here:
                        break
                    here = parent
                if any(os.path.exists(os.path.join(b, cited)) for b in bases):
                    continue
                dead.append("%s -> %s" % (rel, cited))
        self.assertEqual(
            sorted(dead), [],
            "citations of documents that do not exist. Fix the path, or "
            "add the basename to NAMED_AS_GONE if the point IS that it is "
            "gone.")

    def test_the_matcher_finds_a_citation_at_all(self):
        """Guard on the guard: a regex that matched nothing would make
        the test above pass over any tree."""
        found = CITE.findall("see docs/development.md for the rest")
        self.assertEqual(found, ["docs/development.md"])
        self.assertEqual(CITE.findall("https://example.com/a/b.md"), [])
