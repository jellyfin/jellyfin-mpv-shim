"""Finding the mpv window on Windows, independent of its title.

raise_mpv located the window by looking for " - mpv" in the title. Renaming
the window to "<media> - Jellyfin MPV Shim" broke that match silently: no
error, raise-on-play simply stopped working, and on a platform CI never
exercises. It now matches on the window class, which does not move when the
title does.

win32gui only exists on Windows, so it is stubbed here — the logic under
test is pure and does not touch it.
"""

import sys
import types
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

# Must precede the win_utils import; the module imports win32gui at top level.
sys.modules.setdefault("win32gui", types.SimpleNamespace(
    GetWindowText=lambda hwnd: "",
    GetClassName=lambda hwnd: "",
    EnumWindows=lambda cb, arg: None,
    GetForegroundWindow=lambda: 0,
    ShowWindow=lambda hwnd, cmd: None,
))

from jellyfin_mpv_shim import win_utils  # noqa: E402
from jellyfin_mpv_shim.constants import USER_APP_NAME  # noqa: E402


def entry(title, cls=""):
    return (1234, title, cls)


class ClassMatchTest(unittest.TestCase):

    def test_the_mpv_class_matches_whatever_the_title(self):
        self.assertTrue(win_utils.is_mpv_window(
            entry("literally anything", win_utils.MPV_WINDOW_CLASS)))

    def test_another_apps_window_does_not_match(self):
        self.assertFalse(win_utils.is_mpv_window(
            entry("Some Other App", "SomeOtherClass")))


class TitleFallbackTest(unittest.TestCase):
    """For builds whose class differs from the expected one."""

    def test_the_current_title_matches(self):
        self.assertTrue(win_utils.is_mpv_window(
            entry("Rear Window - %s" % USER_APP_NAME)))

    def test_the_idle_title_matches(self):
        self.assertTrue(win_utils.is_mpv_window(entry(USER_APP_NAME)))

    def test_mpvs_stock_title_still_matches(self):
        """An external-mpv setup that never received our --title."""
        self.assertTrue(win_utils.is_mpv_window(entry("No file - mpv")))
        self.assertTrue(win_utils.is_mpv_window(entry("Rear Window - mpv")))

    def test_the_mirror_window_is_never_matched(self):
        """It carries the app name too, so a title match would raise it
        instead of the player."""
        self.assertFalse(win_utils.is_mpv_window(
            entry(win_utils.MIRROR_WINDOW_NAME)))
        self.assertFalse(win_utils.is_mpv_window(
            entry("%s - something" % win_utils.MIRROR_WINDOW_NAME)))

    def test_an_unrelated_window_does_not_match(self):
        self.assertFalse(win_utils.is_mpv_window(entry("Firefox")))
        self.assertFalse(win_utils.is_mpv_window(entry("")))

    def test_a_missing_title_is_tolerated(self):
        self.assertFalse(win_utils.is_mpv_window((1, None, "")))


class TitleAgreementTest(unittest.TestCase):
    """The fallback has to agree with the title player.py actually sets."""

    def test_the_configured_title_would_be_matched(self):
        import os
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "jellyfin_mpv_shim", "player.py"),
                  encoding="utf-8") as fh:
            line = next(l for l in fh if 'mpv_options["title"]' in l)
        # The template ends with the app name; that suffix is what the
        # fallback keys on.
        self.assertIn("USER_APP_NAME", line)
        rendered = "Rear Window - %s" % USER_APP_NAME
        self.assertTrue(win_utils.is_mpv_window(entry(rendered)))


if __name__ == "__main__":
    unittest.main()
