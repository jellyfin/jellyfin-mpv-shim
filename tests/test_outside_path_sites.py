"""Every path handed to another program goes through hostinfo.outside_path.

In a packaged (Store) install a fresh config lives in the package's
LocalCache, and only this process sees it at the %APPDATA% path; a file
manager or reader given that path finds nothing. hostinfo's own tests cover
the mapping. These cover the sites, one test each, because the defect this
guards against is a correct rule applied at all but one of them.
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
import sys
import tempfile
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import hostinfo, system_open  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.gateway.diagnostics import (  # noqa: E402
    DiagnosticsMixin,
)


def outside(path):
    return "OUTSIDE:" + path


class Sites(unittest.TestCase):
    def test_open_config_folder_reveals_the_outside_path(self):
        opened = []
        with mock.patch.object(hostinfo, "outside_path", outside), \
                mock.patch.object(sys, "platform", "darwin"), \
                mock.patch("subprocess.Popen",
                           lambda argv, **kw: opened.append(argv)):
            DiagnosticsMixin.open_config_folder(object())
        self.assertEqual(len(opened), 1)
        self.assertTrue(opened[0][1].startswith("OUTSIDE:"), opened)

    def test_open_path_hands_over_the_outside_path(self):
        fh = tempfile.NamedTemporaryFile(suffix=".epub", delete=False)
        fh.close()
        self.addCleanup(os.unlink, fh.name)
        called = []
        with mock.patch.object(system_open, "outside_path", outside), \
                mock.patch.object(system_open.os, "name", "nt"), \
                mock.patch.object(system_open.os, "startfile",
                                  called.append, create=True):
            self.assertEqual(system_open.open_path(fh.name),
                             (True, "startfile"))
        self.assertEqual(called, [outside(fh.name)])

    def test_the_clipboard_fallback_names_the_outside_path(self):
        """The path is shown to the user, who will go looking for it."""
        gw = mock.Mock(config_dir=lambda: "/cfg")
        with mock.patch.object(hostinfo, "outside_path", outside), \
                mock.patch("jellyfin_mpv_shim.clipboard.copy_or_save",
                           lambda text, path, player=None:
                           (True, "file", path)):
            ok, method, path = DiagnosticsMixin.copy_text(gw, "x")
        self.assertEqual(path, outside(os.path.join("/cfg",
                                                    "copied-logs.txt")))


if __name__ == "__main__":
    unittest.main()
