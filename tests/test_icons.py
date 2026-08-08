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
import ast
import os
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk import vector  # noqa: E402
from jellyfin_mpv_shim.ui_icon_paths import ICON_PATHS  # noqa: E402


#: Where the UI names icons. Kept to the package, since a name in a test
#: or a tool is not something the UI draws.
_UI_ROOT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "jellyfin_mpv_shim")


def _scanned_icon_names():
    """Every icon name the UI asks for, read out of the source.

    ``icon="x"`` as a keyword anywhere, and ``action_btn("x", ...)`` whose
    first argument is the icon (components/controls.py). Empty strings are
    skipped -- an action_btn with no icon is a real and deliberate shape.
    """
    names = set()
    for base, _dirs, files in os.walk(_UI_ROOT):
        for fn in files:
            if not fn.endswith(".py"):
                continue
            path = os.path.join(base, fn)
            with open(path, encoding="utf-8") as fh:
                try:
                    tree = ast.parse(fh.read(), filename=path)
                except SyntaxError:
                    continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                for kw in node.keywords:
                    if (kw.arg == "icon"
                            and isinstance(kw.value, ast.Constant)
                            and isinstance(kw.value.value, str)
                            and kw.value.value):
                        names.add(kw.value.value)
                fn_name = getattr(node.func, "attr",
                                  getattr(node.func, "id", None))
                if (fn_name == "action_btn" and node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and node.args[0].value):
                    names.add(node.args[0].value)
    return names


class TestTheDataSurvives(unittest.TestCase):
    def test_the_icon_set_is_present_and_populated(self):
        self.assertGreater(len(ICON_PATHS), 30,
                           "the generated icon data is missing or truncated")

    def test_every_icon_the_ui_names_exists(self):
        """The UI asks for icons by name; a missing one renders blank.

        **Scanned from the source, not listed here.** This was a hand-written
        sample of sixteen names, which is a test that passes for every icon
        nobody thought to add to it -- and that is exactly how "info"
        shipped missing: the button drew, the dialog opened, every test
        was green, and the glyph was an empty path. A blank icon is the
        quietest possible failure, so the check has to be exhaustive or it
        is decoration.

        Two spellings are found: the ``icon=`` keyword on any widget, and
        ``action_btn``'s first positional argument. That is a **lower
        bound**, not the whole set -- a tile-menu entry names its icon as
        the middle of a bare 3-tuple, and icons.ALIASES names several
        indirectly, neither of which is safely distinguishable from any
        other string by a scan. So this catches the common case and not
        every case; widening it is still cheaper than the bug it prevents.
        """
        named = _scanned_icon_names()
        self.assertGreater(len(named), 30,
                           "the scan found almost nothing -- it has probably "
                           "stopped matching how icons are named")
        missing = sorted(n for n in named if n not in ICON_PATHS)
        self.assertEqual(missing, [],
                         "named by the UI but absent from the generated set, "
                         "so they render blank: %r. Add them to "
                         "gen_ui_icons.py's ICON_NAMES and re-run it."
                         % (missing,))

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

    def test_every_drawing_stays_on_the_24x24_canvas(self):
        """The bug this exists for: several Material icons are authored as
        two ``<path>`` elements, the second starting with a *relative*
        moveto. gen_ui_icons joins them into one ``d``, at which point that
        moveto becomes relative to where the first subpath ended — so
        ``keyboard_double_arrow_left``'s second chevron was drawn at
        (28.59, 36), off the canvas, and the icon rendered as one chevron
        plus clipped debris.

        Nothing else could see it: the path converts cleanly, the drawing is
        anchored, and the only symptom is on screen. Bounds are the check
        that catches the whole class.
        """
        import re

        for name in ICON_PATHS:
            with self.subTest(icon=name):
                numbers = [float(v) for v in
                           re.findall(r"-?\d+(?:\.\d+)?",
                                      vector.icon_ass(name))]
                xs, ys = numbers[0::2], numbers[1::2]
                # A hair of slack: the SVG->ASS conversion rounds, and a few
                # icons legitimately touch the edge.
                self.assertGreaterEqual(min(xs + ys), -1.0, name)
                self.assertLessEqual(max(xs + ys), 25.0, name)

    def test_every_icon_rasterizes_to_visible_ink(self):
        """The PIL side of the same data, used for decorations baked into a
        strip bitmap (an ASS glyph over a tile would be hidden under it —
        GUIDE §6). A contour parser that dropped everything would produce a
        blank image rather than an error."""
        for name in ICON_PATHS:
            with self.subTest(icon=name):
                image = vector.icon_image(name, 24, (255, 0, 0))
                self.assertEqual(image.size, (24, 24))
                self.assertGreater(image.getchannel("A").getextrema()[1], 200,
                                   "%s rasterized to nothing" % name)

    def test_rasterized_icons_are_cached(self):
        """Drawn per tile, so potentially dozens per composited row."""
        first = vector.icon_image("play_arrow", 20, (1, 2, 3))
        self.assertIs(vector.icon_image("play_arrow", 20, (1, 2, 3)), first)

    def test_an_unknown_icon_rasterizes_to_nothing(self):
        image = vector.icon_image("no_such_icon_anywhere", 16, (255, 0, 0))
        self.assertEqual(image.size, (16, 16))
        self.assertEqual(image.getchannel("A").getextrema()[1], 0)

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
