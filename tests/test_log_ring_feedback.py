"""The in-app log viewer must not be able to see its own output.

The Logs tab polls the ring buffer and re-renders when the content changed.
Rendering emits a per-frame timing line. So without an exclusion: the line
lands in the ring, the next poll sees new content, redraws, and emits
another one -- one render and one line per poll, forever, and the "only when
something changed" guard cannot help because **the render IS the change**.

It bites exactly when someone has debug logging on, which is exactly when
they are reading the log.

This is the only feedback loop of its kind in the process: nothing else
reads the log it writes. The rule is therefore about the RING, not about
logging in general -- `log.txt` still gets every line.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import logging
import sys
import unittest

sys.argv = [sys.argv[0]]


def _record(name, message):
    return logging.LogRecord(name, logging.DEBUG, "f.py", 1, message, (), None)


class RingExcludesItsOwnCauseTest(unittest.TestCase):

    def _ring(self):
        from jellyfin_mpv_shim.log_utils import RingLogHandler

        handler = RingLogHandler(capacity=10)
        handler.setFormatter(logging.Formatter("%(name)s: %(message)s"))
        return handler

    def test_the_render_line_never_reaches_the_viewer(self):
        handler = self._ring()
        handler.emit(_record("mpvtk.render", "render: build 1.2ms"))
        self.assertEqual(
            [], list(handler.lines),
            "the log viewer can see the line that drawing it produces, so "
            "polling the tab renders forever")

    def test_ordinary_lines_still_reach_it(self):
        """The control. A ring that dropped everything would make the Logs
        tab pass this file's rule by showing nothing at all."""
        handler = self._ring()
        handler.emit(_record("player", "Playing something"))
        handler.emit(_record("mpvtk", "theme: Default"))
        self.assertEqual(2, len(handler.lines))

    def test_the_excluded_name_is_the_one_actually_used(self):
        """The two halves are in different modules, so nothing but this
        stops a rename on one side silently un-excluding the line."""
        from jellyfin_mpv_shim import log_utils
        from jellyfin_mpv_shim.mpvtk import app as mpvtk_app

        self.assertIn(mpvtk_app.render_log.name, log_utils.RING_EXCLUDED)

    def test_the_render_line_still_goes_to_the_log_file(self):
        """Excluded from the RING only. Someone who turned debug logging on
        to find a slow frame must still find the timings in log.txt."""
        from jellyfin_mpv_shim.mpvtk import app as mpvtk_app

        self.assertTrue(mpvtk_app.render_log.propagate,
                        "the render logger no longer reaches the root "
                        "handlers, so log.txt lost the timings entirely")


if __name__ == "__main__":
    unittest.main()
