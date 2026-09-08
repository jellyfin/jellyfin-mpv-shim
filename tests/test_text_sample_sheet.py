"""The sample sheet builds, and its sample table stays honest.

`tools/shoot_text_samples.py` exists because no assertion here can see text
drawn in the wrong order, letters that failed to join, or Traditional Chinese
set in Japanese letterforms -- a person looking at a picture is the only
instrument for those. That makes the tool a deliverable rather than a
convenience, and an unrunnable one is worth nothing when it is next needed.

So this pins the two things about it that can go stale silently: that it still
builds an image through the real `pilfont` path, and that every row of its
table still resolves the script it is filed under. It deliberately does **not**
assert anything about the pixels; that is the whole point of the tool.
"""

if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest


class SampleSheetTest(unittest.TestCase):
    def _tool(self):
        import importlib

        try:
            return importlib.import_module("tools.shoot_text_samples")
        except ImportError as exc:            # pragma: no cover
            self.skipTest("tool not importable: %s" % exc)

    def test_the_sheet_builds(self):
        tool = self._tool()
        img = tool.build(size=18, width=900)
        self.assertGreater(img.width, 0)
        self.assertGreater(img.height, 0)
        # Not a blank canvas: something was drawn on it. The cheapest
        # possible check that the pilfont path ran at all -- a sheet that
        # silently drew nothing would still have saved a valid PNG.
        self.assertGreater(len(img.getcolors(maxcolors=1 << 20) or []), 3,
                           "the sheet came out effectively blank")

    def test_every_row_is_filed_under_the_script_it_resolves(self):
        """A row moved to the wrong group, or a sample edited until it no
        longer exercises what its label claims, is invisible in a picture --
        the reader trusts the label. `script_of` is what decides the face,
        so it is what the group has to agree with.
        """
        from jellyfin_mpv_shim.mpvtk import pilfont

        tool = self._tool()
        want = {"Right to left": ("hebrew", "arabic"),
                "CJK -- one bucket, four languages": ("cjk",),
                "Latin and neighbours": ("latin",)}
        for row in tool.SAMPLES:
            group, label, text = row[0], row[1], row[2]
            if group not in want:
                continue
            self.assertIn(
                pilfont.script_of(text), want[group],
                "%r is filed under %r but resolves %r"
                % (label, group, pilfont.script_of(text)))

    def test_the_rtl_mix_rows_really_do_mix(self):
        """The rows that demonstrate RTL outranking are only evidence if
        they contain both scripts. An edit that dropped the CJK would leave
        a row that looks fine and proves nothing."""
        from jellyfin_mpv_shim.mpvtk import pilfont

        tool = self._tool()
        rows = [r for r in tool.SAMPLES if "RTL wins" in r[1]]
        self.assertTrue(rows, "the RTL-precedence rows are gone")
        for row in rows:
            text = row[2]
            self.assertTrue(pilfont.has_rtl(text), "%r has no RTL" % row[1])
            self.assertTrue(
                any(pilfont.script_of_char(ord(c)) == "cjk" for c in text),
                "%r has no CJK left to be outranked" % row[1])

    def test_the_expected_boxes_notes_point_at_something_real(self):
        """A row marked "boxes here are expected" is telling the reader not
        to investigate, so it has to be true. Each one must cite where the
        trade is written down."""
        tool = self._tool()
        marked = [r for r in tool.SAMPLES if len(r) > 3]
        self.assertTrue(marked, "no rows carry an explanation any more")
        for row in marked:
            note = row[3]
            self.assertTrue(
                "GUIDE" in note or "BY DESIGN" in note,
                "%r explains its boxes without citing the decision: %r"
                % (row[1], note))


if __name__ == "__main__":
    unittest.main()
