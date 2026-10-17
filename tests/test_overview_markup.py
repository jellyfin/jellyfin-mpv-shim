"""Overviews arrive with markup in them: metadata providers leave HTML
(`<br>`, `<i>`, `&amp;`) and BBCode (`[i]...[/i]`) in the text, and the
browser draws text, not markup, so it showed the tags."""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import pathlib
import re
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.components import chrome  # noqa: E402

PACKAGE = pathlib.Path(__file__).resolve().parent.parent / "jellyfin_mpv_shim"


class OverviewMarkupTest(unittest.TestCase):
    def clean(self, text):
        return chrome.overview({"Overview": text})

    def test_line_breaks_become_newlines(self):
        self.assertEqual(self.clean("One.<br>Two.<br/>Three.<BR />Four."),
                         "One.\nTwo.\nThree.\nFour.")
        self.assertEqual(self.clean("<p>One.</p><p>Two.</p>"),
                         "One.\nTwo.")

    def test_html_tags_go_and_their_text_stays(self):
        self.assertEqual(
            self.clean('A <i>very</i> <b>bold</b> <a href="x">link</a>.'),
            "A very bold link.")

    def test_bbcode_tags_go_and_their_text_stays(self):
        self.assertEqual(
            self.clean("A [i]quiet[/i] [B]loud[/B] [url=http://x]site[/url]."),
            "A quiet loud site.")

    def test_entities_are_decoded(self):
        self.assertEqual(self.clean("Tom &amp; Jerry &quot;cat&quot;"),
                         'Tom & Jerry "cat"')

    def test_text_that_only_looks_like_markup_survives(self):
        for text in ("x < y and y > z", "3 <4", "[citation needed]",
                     "Season [1]", "a <-> b"):
            with self.subTest(text=text):
                self.assertEqual(self.clean(text), text)

    def test_missing_and_empty(self):
        self.assertEqual(chrome.overview({}), "")
        self.assertEqual(chrome.overview({"Overview": None}), "")
        self.assertEqual(self.clean("  <br>  "), "")


class EveryOverviewIsCleanedTest(unittest.TestCase):
    """The clean-up is only as good as its coverage: a page that reads the
    field itself draws the tags again. Every read goes through
    chrome.overview."""

    READ = re.compile(r"""\[\s*["']Overview["']\s*\]|get\(\s*["']Overview["']""")

    def test_nothing_else_reads_the_field(self):
        allowed = {PACKAGE / "mpvtk_browser" / "components" / "chrome.py"}
        offenders = []
        for path in PACKAGE.rglob("*.py"):
            if path in allowed:
                continue
            for n, line in enumerate(path.read_text(encoding="utf-8")
                                     .splitlines(), 1):
                if self.READ.search(line):
                    offenders.append("%s:%d" % (path.relative_to(PACKAGE), n))
        self.assertEqual(offenders, [],
                         "read Overview through chrome.overview(item)")


if __name__ == "__main__":
    unittest.main()
