"""The unplayed-count chip must be as wide as the number it carries.

The chip was a fixed 26 logical px, which is narrower than three digits
draw at the default badge size -- so a series with 100+ unwatched episodes
showed its count hanging out of both ends of its own chip. The size is a
property of the text, not of the two digits somebody had in mind.

Asserted on the composited pixels rather than on the arithmetic, because
the arithmetic is what was wrong: the old code computed a width too, it
just did not compute it from the text.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest

from jellyfin_mpv_shim.mpvtk_browser import theme
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile


class _CapturingStore:
    """MemoryStore stand-in that hands back the image it was given."""

    def __init__(self):
        self.last = None

    def add(self, img):
        self.last = img
        return "&1", img.width, img.height

    def remove(self, src):
        pass


class CountBadgeTest(unittest.TestCase):
    def setUp(self):
        self.mem = _CapturingStore()
        self.store = StripStore(mem_store=self.mem)
        self.accent = theme.rgb(theme.ACCENT, 255)[:3]

    def _bands(self, badge):
        """(accent span, white span) across the chip's centre row."""
        self.store.clear()
        self.store.strip([Tile(key="k", title="T", badge=badge)])
        img = self.mem.last.convert("RGB")
        # The chip's vertical centre; see _paint_decorations.
        y = int(round(15 * (img.height / self.store.geom.strip_h)))
        accent, white = [], []
        for x in range(img.width):
            p = img.getpixel((x, y))
            if max(abs(a - b) for a, b in zip(p, self.accent)) <= 6:
                accent.append(x)
            elif min(p) >= 240:
                white.append(x)
        return accent, white

    def test_the_number_stays_inside_its_chip(self):
        # One digit is the case that always worked; four is past anything
        # a real library shows and is here because the fix is a measurement
        # and a measurement should not have a ceiling.
        for badge in (7, 42, 123, 1984):
            with self.subTest(badge=badge):
                accent, white = self._bands(badge)
                self.assertTrue(accent, "no chip was drawn")
                self.assertTrue(white, "no digits were drawn")
                # Padding, not mere containment. At exactly three digits
                # the old chip was two px narrower than the ink, and the
                # ink's own antialiased edge is dim enough that a strict
                # inside/outside test reads it as contained. What a chip
                # has to be is a chip: some of it on each side.
                self.assertGreaterEqual(
                    min(white) - min(accent), 3,
                    "digits crowd the left of the chip: %d" % badge)
                self.assertGreaterEqual(
                    max(accent) - max(white), 3,
                    "digits crowd the right of the chip: %d" % badge)

    def test_the_chip_grows_with_the_count(self):
        # Not just "wide enough" -- wide enough for each of them, which is
        # what a fixed width can also be for one value by luck.
        widths = []
        for badge in (7, 42, 123, 1984):
            accent, _ = self._bands(badge)
            widths.append(max(accent) - min(accent))
        self.assertEqual(widths, sorted(widths))
        self.assertLess(widths[0], widths[-1])

    def test_the_chip_keeps_its_corner(self):
        # It grows leftwards. If it grew from the centre a wide count would
        # walk off the right-hand edge of the card.
        rights = [max(self._bands(b)[0]) for b in (7, 42, 123, 1984)]
        self.assertEqual(len(set(rights)), 1, "chip's right edge moved")

    def test_a_single_digit_is_unchanged(self):
        # The fix is a floor plus growth, not a redesign: the common case
        # keeps the size it has always had (27px inclusive, as before).
        accent, _ = self._bands(7)
        self.assertEqual(max(accent) - min(accent) + 1, 27)


if __name__ == "__main__":
    unittest.main()
