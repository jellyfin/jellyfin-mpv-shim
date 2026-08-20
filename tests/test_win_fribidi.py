"""Loading FriBiDi so Pillow can shape text, and the cache that hides it.

Pillow's wheels ship no FriBiDi on any platform. libraqm and HarfBuzz are
compiled into ``_imagingft``; FriBiDi alone is loaded at runtime by a
vendored shim, and Windows has no such DLL -- so ``ImageFont.truetype``
silently returns a Basic-layout font, right-to-left text renders reversed
and unjoined, and *no* script gets GPOS kerning (#689).

Verified against a real Windows Pillow wheel under wine before this was
written: the stock wheel answers ``RAQM=False``, and dropping one 150KB DLL
beside it answers ``RAQM=True`` and renders the reporter's Arabic correctly.
What is tested here is the parts of that which run on any platform -- where
we look, that it happens once, and that it cannot be skipped by a stale
measurement cache.
"""

import ctypes  # noqa: F401  -- see below
import os
import sys
import unittest
from unittest import mock

# `ctypes` is imported here for its SIDE EFFECT, not for its API.
#
# These tests patch `win_fribidi.os.name` to "nt", and `win_fribidi.os` is
# the one shared `os` module -- so the patch is global while it is in
# effect. `preload()` does `import ctypes` inside the function, and
# `ctypes/__init__.py` branches on `os.name`: reached for the first time
# under the patch, it takes the Windows path and dies on
# `from _ctypes import FormatError`.
#
# It never bit in a full `discover tests` run because something earlier had
# always imported ctypes already, so the import was a cache hit. Running
# this module on its own -- which is what tools/run_tests_parallel.py does
# to every module -- is where it surfaces.

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import win_fribidi  # noqa: E402


class PreloadTest(unittest.TestCase):
    def setUp(self):
        # Module state, so each test starts from "not yet tried".
        self._saved = (win_fribidi._done, win_fribidi.loaded_from)
        win_fribidi._done = False
        win_fribidi.loaded_from = None

    def tearDown(self):
        win_fribidi._done, win_fribidi.loaded_from = self._saved

    def test_it_is_a_no_op_off_windows(self):
        """Every other platform already has FriBiDi: the manylinux wheels
        dlopen the system ``libfribidi.so.0``, which any desktop has because
        pango needs it, and a distro Pillow links raqm outright."""
        with mock.patch.object(win_fribidi.os, "name", "posix"):
            self.assertIsNone(win_fribidi.preload())
        self.assertIsNone(win_fribidi.loaded_from)

    def test_it_runs_once(self):
        """This sits in front of font loading, which is on the render path,
        so a second call must not re-walk the filesystem."""
        calls = []
        with mock.patch.object(win_fribidi, "_candidate_dirs",
                               lambda: calls.append(1) or []), \
                mock.patch.object(win_fribidi.os, "name", "nt"):
            win_fribidi.preload()
            win_fribidi.preload()
            win_fribidi.preload()
        self.assertEqual(len(calls), 1)

    def test_a_missing_dll_is_not_an_error(self):
        """A degraded UI, not a reason to refuse to launch -- the contract
        every optional dependency here has."""
        with mock.patch.object(win_fribidi, "_candidate_dirs", lambda: []), \
                mock.patch.object(win_fribidi.os, "name", "nt"):
            self.assertIsNone(win_fribidi.preload())

    def test_the_bundle_directory_is_searched_first(self):
        """``sys._MEIPASS`` is where ``--add-binary`` puts it, and under
        PyInstaller 6 that is ``_internal`` rather than the folder holding
        the .exe -- which is the whole reason this does not rely on the DLL
        search path."""
        with mock.patch.object(sys, "_MEIPASS", "/bundle", create=True):
            dirs = win_fribidi._candidate_dirs()
        self.assertEqual(dirs[0], "/bundle")

    def test_the_candidate_list_has_no_repeats(self):
        """Several of these point at the same place in a source checkout,
        and each repeat is two more stat calls before giving up."""
        dirs = win_fribidi._candidate_dirs()
        self.assertEqual(len(dirs), len(set(dirs)))

    def test_it_loads_the_first_name_that_exists(self):
        loaded = []
        fake_ctypes = mock.Mock()
        fake_ctypes.WinDLL = lambda path: loaded.append(path)
        with mock.patch.object(win_fribidi.os, "name", "nt"), \
                mock.patch.object(win_fribidi, "_candidate_dirs",
                                  lambda: ["/a", "/b"]), \
                mock.patch.object(win_fribidi.os.path, "exists",
                                  lambda p: p == os.path.join(
                                      "/b", "libfribidi-0.dll")), \
                mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            got = win_fribidi.preload()
        self.assertEqual(loaded, [os.path.join("/b", "libfribidi-0.dll")])
        self.assertEqual(got, os.path.join("/b", "libfribidi-0.dll"))

    def test_a_dll_that_will_not_load_moves_on_rather_than_raising(self):
        """The expected failure is an architecture mismatch, which on
        Windows on Arm is a plausible mistake -- and it must not take the
        launch with it."""
        tried, loaded = [], []

        def win_dll(path):
            tried.append(path)
            if path.endswith("fribidi-0.dll") and "/bad" in path:
                raise OSError("not a valid Win32 application")
            loaded.append(path)

        fake_ctypes = mock.Mock()
        fake_ctypes.WinDLL = win_dll
        with mock.patch.object(win_fribidi.os, "name", "nt"), \
                mock.patch.object(win_fribidi, "_candidate_dirs",
                                  lambda: ["/bad", "/good"]), \
                mock.patch.object(win_fribidi.os.path, "exists",
                                  lambda p: p.endswith("fribidi-0.dll")), \
                mock.patch.dict(sys.modules, {"ctypes": fake_ctypes}):
            got = win_fribidi.preload()
        self.assertTrue(loaded, "gave up after the first bad DLL")
        self.assertEqual(got, loaded[0])
        self.assertIn("/good", got)


class DescribeTest(unittest.TestCase):
    """The startup line.

    It exists because the shipped Windows build is a PyInstaller bundle with
    no interpreter to query, so "does your Pillow have Raqm" has to already
    be in the log a reporter sends.
    """

    def test_it_names_the_answer(self):
        with mock.patch.object(win_fribidi, "raqm_available", lambda: True):
            self.assertIn("raqm=yes", win_fribidi.describe())
        with mock.patch.object(win_fribidi, "raqm_available", lambda: False):
            self.assertIn("raqm=no", win_fribidi.describe())

    def test_it_survives_pillow_being_absent(self):
        """Pillow is optional here, and a startup log line is not the place
        to discover that."""
        with mock.patch.object(win_fribidi, "raqm_available", lambda: None):
            self.assertIsInstance(win_fribidi.describe(), str)


class MetricsCacheKeyTest(unittest.TestCase):
    """The layout engine is part of the measurement cache key.

    This is the half of the fix that would have failed silently. The cache is
    keyed on the font path, its mtime, and Pillow's version -- and *none of
    those move* when FriBiDi appears. A Windows user updating into a build
    that ships it would keep measuring with the old unkerned numbers, and the
    only symptom would be captions still truncating slightly early.

    Raqm applies the font's GPOS kerning and Basic does not: measured on
    DejaVuSans at 20px, "AVATAR" is 81.94px against 75.20px, 9% apart.
    """

    def _font(self):
        from PIL import ImageFont

        for path in ("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                     "/usr/share/fonts/truetype/liberation/"
                     "LiberationSans-Regular.ttf"):
            if os.path.exists(path):
                return ImageFont.truetype(path, 20)
        self.skipTest("no measurable font on this box")

    def test_the_engine_is_in_the_key(self):
        from jellyfin_mpv_shim.mpvtk import metrics

        font = self._font()
        with mock.patch.object(metrics, "_layout_engine", lambda: "raqm"):
            raqm_key = metrics._cache_key(font)
        with mock.patch.object(metrics, "_layout_engine", lambda: "basic"):
            basic_key = metrics._cache_key(font)
        self.assertNotEqual(raqm_key, basic_key)

    def test_everything_else_still_keys_it(self):
        """The engine is added to the key, not substituted for it."""
        from jellyfin_mpv_shim.mpvtk import metrics

        font = self._font()
        key = metrics._cache_key(font)
        self.assertIn(font.path, key)
        self.assertIn(str(metrics._METRICS_VERSION), key)

    def test_the_engine_is_asked_of_pillow(self):
        """Not inferred from whether `preload` found a file: a system
        FriBiDi on the search path counts, and so does a distro Pillow with
        raqm linked in."""
        from jellyfin_mpv_shim.mpvtk import metrics

        self.assertIn(metrics._layout_engine(), ("raqm", "basic", "?"))

    def test_an_unimportable_pillow_does_not_break_the_key(self):
        from jellyfin_mpv_shim.mpvtk import metrics

        with mock.patch.dict(sys.modules, {"PIL.features": None}):
            self.assertIsInstance(metrics._layout_engine(), str)


if __name__ == "__main__":
    unittest.main()
