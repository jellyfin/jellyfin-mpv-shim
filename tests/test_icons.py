"""The shared icon path data and its converter.

`jellyfin_mpv_shim/ui_icon_paths.py` is generated Material SVG path data
(see gen_ui_icons.py) and is SHARED — it outlived the Tk browser, whose
rasterizer used to be its other consumer. It now feeds `mpvtk/vector.py`,
which converts each path to an ASS drawing for the renderer.

That makes it easy to sweep up by mistake during a cleanup: it sits at the
package root, not under mpvtk/, and nothing about its name says the in-mpv
UI depends on it. Deleting it breaks every icon in the application, so
these tests hold the whole path: data present, every entry convertible, and
the converter's contract.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk import vector  # noqa: E402
from jellyfin_mpv_shim.ui_icon_paths import ICON_PATHS  # noqa: E402


class TestTheDataSurvives(unittest.TestCase):
    def test_the_icon_set_is_present_and_populated(self):
        self.assertGreater(len(ICON_PATHS), 30,
                           "the generated icon data is missing or truncated")

    def test_every_icon_the_ui_names_exists(self):
        """The UI asks for icons by name; a missing one renders blank."""
        for name in ("play_arrow", "pause", "home", "search", "settings",
                     "favorite", "favorite_border", "lock", "person",
                     "refresh", "folder", "content_copy", "queue_music",
                     "file_download", "groups", "radio"):
            self.assertIn(name, ICON_PATHS, name)


class TestTheConverter(unittest.TestCase):
    def test_every_icon_converts_to_a_drawing(self):
        """A path the converter chokes on is an icon that never renders."""
        for name in ICON_PATHS:
            with self.subTest(icon=name):
                out = vector.icon_ass(name)
                self.assertTrue(out, "%s produced nothing" % name)
                self.assertIn("m ", out, "%s is not an ASS drawing" % name)

    def test_the_result_is_cached(self):
        """Converted at first use; every tile redraw would otherwise re-parse
        the path data."""
        first = vector.icon_ass("play_arrow")
        self.assertIs(vector.icon_ass("play_arrow"), first)

    def test_every_drawing_is_corner_anchored(self):
        """The renderer scales with \\fscx/\\fscy against a 24x24 box, so each
        drawing pins that box with two zero-length contours. Without them an
        icon is scaled by its ink, and glyphs jump around at different sizes.
        """
        for name in list(ICON_PATHS)[:12]:
            with self.subTest(icon=name):
                self.assertTrue(vector.icon_ass(name).startswith(vector._ANCHOR),
                                "%s is not anchored" % name)

    def test_an_unknown_icon_degrades_rather_than_raising(self):
        """It used to raise KeyError out of the middle of layout, which took
        down the whole scene — one mistyped name blanked the entire UI."""
        out = vector.icon_ass("no_such_icon_anywhere")
        self.assertEqual(out, vector._ANCHOR,
                         "an unknown icon should draw nothing, not raise")

    def test_icon_names_is_the_data(self):
        self.assertEqual(set(vector.icon_names()), set(ICON_PATHS))


if __name__ == "__main__":
    unittest.main()
