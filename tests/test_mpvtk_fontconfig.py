"""#786: fonts Pillow cannot find by path, found through fontconfig.

On NixOS the fonts are in /nix/store and only fontconfig's configuration lists
them, so every curated candidate failed and the library's titles fell to
Pillow's built-in face -- at 10px, whatever size was asked for.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ctypes.util
import os
import shutil
import sys
import unittest
from unittest import mock

from jellyfin_mpv_shim.mpvtk import fontconfig, pilfont

LINUX = sys.platform not in ("win32", "darwin")


def real_latin_face(test):
    from PIL import ImageFont

    for name in pilfont._CANDIDATES["latin"]:
        try:
            return str(ImageFont.truetype(name, 20).path)
        except (OSError, IOError):
            continue
    test.skipTest("no Latin face installed to stand in for a store font")


class _Fresh(unittest.TestCase):
    def setUp(self):
        fontconfig.reset()
        self.addCleanup(fontconfig.reset)

    def child(self, code):
        """Run ``code`` as the child script in place of the real one."""
        import tempfile

        fd, path = tempfile.mkstemp(suffix=".py")
        with os.fdopen(fd, "w") as fh:
            fh.write(code)
        self.addCleanup(os.unlink, path)
        return mock.patch.object(fontconfig, "_CHILD_PATH", path)


@unittest.skipUnless(LINUX, "fontconfig is only asked on Linux")
class LookupTest(_Fresh):

    def test_a_child_that_segfaults_is_a_failed_lookup_not_a_crash(self):
        """The reason the query is a child process at all."""
        with self.child("import os; os.kill(os.getpid(), 11)"), \
                mock.patch.object(fontconfig, "_from_fc_list",
                                  lambda: None):
            got = fontconfig.lookup(["sans-serif"])
        self.assertEqual(got, {"files": {}, "match": {}})

    def test_a_child_that_answers_nonsense_is_a_failed_lookup(self):
        with self.child("print('[1, 2]')"), \
                mock.patch.object(fontconfig, "_from_fc_list",
                                  lambda: None):
            got = fontconfig.lookup(["sans-serif"])
        self.assertEqual(got, {"files": {}, "match": {}})

    @unittest.skipUnless(shutil.which("fc-list"), "no fc-list here")
    def test_fc_list_stands_in_for_a_child_that_failed(self):
        with self.child("import sys; sys.exit(2)"):
            got = fontconfig.lookup(["sans-serif"])
        self.assertTrue(got["files"], "fc-list's file list was not used")
        self.assertEqual(got["match"], {})

    def test_the_child_script_ships_beside_the_module(self):
        """Run by path, never imported: a missing file would make every
        lookup fall back to fc-list without anything saying why."""
        self.assertTrue(os.path.isfile(fontconfig._CHILD_PATH))

    def test_it_asks_once_per_process(self):
        calls = []

        def child(queries):
            calls.append(list(queries))
            return {"files": ["/x/A.ttf", "/x/fonts.dir", "/x/B.pcf.gz"],
                    "match": {}, "via": "test"}

        with mock.patch.object(fontconfig, "_from_child", child):
            for _ in range(3):
                got = fontconfig.lookup(["sans-serif"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(got["files"], {"a.ttf": "/x/A.ttf"},
                         "a file Pillow cannot open as TrueType was indexed")

    @unittest.skipUnless(ctypes.util.find_library("fontconfig"),
                         "no libfontconfig here")
    def test_the_real_library_answers_with_files_that_open(self):
        """Against this host's own fontconfig, through the ctypes child --
        the half nothing else here exercises."""
        from PIL import ImageFont

        got = fontconfig.lookup(["sans-serif", "sans-serif:weight=bold"])
        self.assertTrue(got["files"])
        path = got["match"].get("sans-serif")
        self.assertTrue(path and os.path.exists(path), got["match"])
        ImageFont.truetype(path, 20)


class PilfontThroughFontconfigTest(_Fresh):
    """The pilfont half, with fontconfig's answer stubbed so the test says
    the same thing on every host."""

    def setUp(self):
        super().setUp()
        self.real = real_latin_face(self)
        for table in (pilfont._CANDIDATES, pilfont._BOLD):
            for script, names in list(table.items()):
                self.addCleanup(table.__setitem__, script, list(names))
        self.addCleanup(setattr, pilfont, "_HOST_FONT_DIRS",
                        pilfont._HOST_FONT_DIRS)
        self.addCleanup(pilfont.clear_cache)
        pilfont._HOST_FONT_DIRS = ("/nonexistent/run/host/fonts",)
        self.asked = []

    def _nixos(self, answer, candidates):
        """A host where ``candidates`` are the only Latin names and none of
        them opens where it says, and fontconfig answers ``answer``."""
        pilfont._CANDIDATES["latin"] = candidates
        pilfont._BOLD["latin"] = []
        pilfont.clear_cache()

        def lookup(queries=()):
            self.asked.append(queries)
            return answer

        patcher = mock.patch.object(fontconfig, "lookup", lookup)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_candidate_is_found_by_basename_where_fontconfig_has_it(self):
        self._nixos({"files": {"jmsstoreonly-regular.ttf": self.real},
                     "match": {}},
                    ["/nix/store/abc-font/share/JmsStoreOnly-Regular.ttf"])
        got = pilfont.font_for("Blade Runner 2049", 20)
        self.assertEqual(str(getattr(got, "path", "")), self.real)

    def test_with_no_known_name_fontconfig_s_own_choice_is_used(self):
        self._nixos({"files": {}, "match": {"sans-serif": self.real}},
                    ["/nix/store/abc-font/share/NotInstalled.ttf"])
        got = pilfont.font_for("Blade Runner 2049", 20)
        self.assertEqual(str(getattr(got, "path", "")), self.real)

    def test_a_curated_candidate_still_outranks_fontconfig(self):
        """Its matches are appended, not preferred -- and it is not even
        asked while a candidate opens."""
        self._nixos({"files": {}, "match": {"sans-serif": "/elsewhere.ttf"}},
                    [self.real])
        got = pilfont.font_for("Blade Runner 2049", 20)
        self.assertEqual(str(getattr(got, "path", "")), self.real)
        self.assertEqual(self.asked, [], "fontconfig was asked needlessly")

    def test_with_nothing_found_anywhere_the_face_is_the_size_asked(self):
        """The tiny titles themselves: Pillow's default is 10px unless it
        is given a size."""
        from PIL import ImageFont

        self._nixos({"files": {}, "match": {}},
                    ["/nix/store/abc-font/share/NotInstalled.ttf"])
        if not hasattr(ImageFont.load_default(size=20), "size"):
            self.skipTest("Pillow < 10.1 has only the bitmap default")
        for size in (14, 20, 28):
            got = pilfont.font_for("Blade Runner 2049", size)
            self.assertEqual(got.size, size)


if __name__ == "__main__":
    unittest.main()
