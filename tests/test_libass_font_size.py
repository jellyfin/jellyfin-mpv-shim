"""What size libass actually draws, which is not the size we asked for.

libass reads ``\\fs`` as the font's **ascender+descender height**, not its em
(VSFilter compatibility). That box is font-relative, so one ``\\fs`` renders a
different em per face. Measured at a 128px em: DejaVu Sans' box is 150 and
Segoe UI's is 172, and those are the faces libass draws on Linux and on
Windows. Every string in the browser therefore came out 13% smaller on
Windows, with every box around it identical -- the width model carried the
same per-face factor, so it stayed self-consistent and predicted the smaller
text correctly.

**Linux is the reference.** Every face is asked for the size DejaVu Sans
would have drawn at that nominal size, so the number means one physical size
everywhere. `tests/lua/test_renderer.lua` holds the renderer's half.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk import metrics  # noqa: E402

#: Ascent+descent at `metrics._MEASURE_SIZE`, both measured with Pillow.
DEJAVU = 150      # what libass draws on Linux -- the reference
SEGOE = 172       # what it draws on Windows


class Face:
    """A font with a chosen ascender+descender box and flat advances."""

    def __init__(self, ascent, descent, advance=64.0):
        self._metrics = (ascent, descent)
        self._advance = advance
        self.path = os.path.join(tempfile.gettempdir(), "fake-face.ttf")

    def getmetrics(self):
        return self._metrics

    def getlength(self, text):
        return self._advance * len(text)

    def getname(self):
        return ("Fake Face", "Regular")


def measured(face):
    """`measure_font()` for a face, with the disk cache pointed at a
    scratch file -- the real one is keyed on a real font path."""
    with tempfile.TemporaryDirectory() as tmp:
        with mock.patch.object(metrics, "_load_font", lambda: face), \
                mock.patch.object(
                    metrics, "_cache_path",
                    lambda: os.path.join(tmp, "metrics.json")):
            return metrics.measure_font()


def rendered_em(nominal, data, face):
    """The em libass ends up drawing, in px.

    The second factor is libass, not us: it scales ``\\fs`` so the face's
    ascender+descender box comes out that tall, which is the whole bug.
    """
    ascent, descent = face.getmetrics()
    return (nominal * data["fs"]) * (metrics._MEASURE_SIZE
                                     / float(ascent + descent))


class RenderedSize(unittest.TestCase):
    def test_one_nominal_size_is_one_physical_size(self):
        """The property the whole change exists for. Before it, the same
        17 drew a 14.5px em on Linux and a 12.7px em on Windows."""
        dejavu, segoe = Face(120, 30), Face(138, 34)
        on_linux = rendered_em(17, measured(dejavu), dejavu)
        on_windows = rendered_em(17, measured(segoe), segoe)
        # 4 places, not exact: `fs` is rounded on its way through JSON.
        self.assertAlmostEqual(on_linux, on_windows, places=4)

    def test_and_it_is_the_size_linux_already_drew(self):
        """Conforming Windows up to Linux, not both to the nominal number:
        the reference face is the one whose rendering nobody complained
        about, and matching the nominal number instead would have grown
        every string on every platform."""
        dejavu = Face(120, 30)
        self.assertAlmostEqual(
            rendered_em(17, measured(dejavu), dejavu),
            17 * metrics._MEASURE_SIZE / float(DEJAVU), places=4)

    def test_the_reference_face_is_not_stretched_at_all(self):
        self.assertEqual(measured(Face(120, 30))["fs"], 1.0)

    def test_a_taller_box_is_stretched_to_compensate(self):
        self.assertAlmostEqual(measured(Face(138, 34))["fs"],
                               SEGOE / float(DEJAVU), places=4)


class WidthTable(unittest.TestCase):
    """Widths are a fraction of the NOMINAL size, so they cannot carry the
    face's own box any more -- the renderer's `\\fs` stretch does that now,
    and folding it in twice would predict a width nothing draws."""

    def test_the_table_does_not_move_with_the_face(self):
        wide, narrow = measured(Face(120, 30)), measured(Face(138, 34))
        self.assertEqual(wide["widths"]["M"], narrow["widths"]["M"])

    def test_and_it_still_predicts_what_libass_draws(self):
        """A width is `nominal * widths[c]`; libass draws `em * advance_em`.
        They have to be the same number or clicks land on the wrong letter.
        2 places: the width table is stored to 4 decimals, which is a
        thousandth of a pixel at these sizes and the tolerance this can
        have without asserting on the rounding instead.
        """
        for face in (Face(120, 30), Face(138, 34)):
            with self.subTest(box=sum(face.getmetrics())):
                data = measured(face)
                predicted = 17 * data["widths"]["M"]
                drawn = rendered_em(17, data, face) * (
                    face.getlength("M") / metrics._MEASURE_SIZE)
                self.assertAlmostEqual(predicted, drawn, places=2)


class OnePlace(unittest.TestCase):
    """The per-face box was computed at three sites and folded into four
    tables. That is the shape that made this survive: a rule spelled out
    in one place and applied in all of them, with nothing tying them
    together."""

    def test_the_face_box_is_read_in_exactly_one_place(self):
        src = open(metrics.__file__, encoding="utf-8").read()
        hits = re.findall(r"ascent \+ descent|ascent\+descent", src)
        self.assertEqual(
            len(hits), 1,
            "the face's ascender+descender box is read %d times; it belongs "
            "in _fs_multiplier alone, or the next face divides them again"
            % len(hits))

    def test_a_cache_written_before_this_is_not_reused(self):
        """The stored table carries the old per-face factor and no `fs`
        key, so a warm launch would keep drawing small forever."""
        face = Face(138, 34)
        with mock.patch.object(metrics, "_METRICS_VERSION", 1):
            old = metrics._cache_key(face)
        with mock.patch.object(metrics, "_METRICS_VERSION", 2):
            new = metrics._cache_key(face)
        self.assertNotEqual(old, new)


if __name__ == "__main__":
    unittest.main()
