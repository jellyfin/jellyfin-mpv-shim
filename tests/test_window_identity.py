"""Window title, desktop-icon identity, and remembered geometry.

mpv's own defaults name the wrong application ("No file - mpv") and open a
fixed 960x540 window however big the display is. These pin the overrides,
plus the one cross-file agreement that fails silently: the desktop-entry id
in constants.py has to match the installed .desktop file's basename AND its
StartupWMClass, because that match is the entire mechanism by which a Linux
desktop finds the window's icon. Break it and nothing errors — you just get
mpv's icon back.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.constants import DESKTOP_ID, USER_APP_NAME  # noqa: E402
from jellyfin_mpv_shim.conf import Settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INTEGRATION = os.path.join(ROOT, "jellyfin_mpv_shim", "integration")


class DesktopIdentityTest(unittest.TestCase):
    """constants.DESKTOP_ID vs the shipped .desktop file."""

    def _desktop_path(self):
        return os.path.join(INTEGRATION, DESKTOP_ID + ".desktop")

    def _entries(self):
        out = {}
        with open(self._desktop_path(), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line and not line.startswith(("#", "[")) and "=" in line:
                    k, v = line.split("=", 1)
                    out[k] = v
        return out

    def test_the_desktop_file_is_named_after_the_id(self):
        """DESKTOP_ID is what mpv reports as the window class; if no
        installed .desktop has that basename, the desktop cannot match it."""
        self.assertTrue(os.path.exists(self._desktop_path()),
                        "no .desktop named %s.desktop" % DESKTOP_ID)

    def test_startup_wm_class_matches_the_id(self):
        self.assertEqual(self._entries().get("StartupWMClass"), DESKTOP_ID)

    def test_the_icon_is_declared(self):
        self.assertEqual(self._entries().get("Icon"), DESKTOP_ID)

    def test_the_icon_files_are_shipped(self):
        """The .desktop names an icon by id; the PNGs are what installs it."""
        pngs = [f for f in os.listdir(INTEGRATION) if f.endswith(".png")]
        self.assertTrue(pngs, "no icon PNGs to install")


class WindowSettingsTest(unittest.TestCase):

    def test_the_default_is_larger_than_mpvs(self):
        """mpv opens 960x540 whatever the display size, which is cramped for
        a browsable UI."""
        self.assertGreater(Settings.window_width, 960)
        self.assertGreater(Settings.window_height, 540)

    def test_the_defaults_are_a_sane_aspect(self):
        ratio = Settings.window_width / Settings.window_height
        self.assertAlmostEqual(ratio, 16 / 9, places=2)

    def test_remembering_is_on_by_default(self):
        self.assertTrue(Settings.remember_window_size)

    def test_the_types_are_coercible_by_the_settings_loader(self):
        """settings_base only understands the object_types table; an
        annotation outside it KeyErrors at load time."""
        ann = Settings.__annotations__
        self.assertIs(ann["window_width"], int)
        self.assertIs(ann["window_height"], int)
        self.assertIs(ann["window_maximized"], bool)
        self.assertIs(ann["remember_window_size"], bool)


class SettingsFormTest(unittest.TestCase):
    """Remembered state must not appear as an editable setting."""

    def test_remembered_geometry_is_hidden(self):
        schema = cfg.settings_schema()
        for key in ("window_width", "window_height", "window_maximized"):
            self.assertNotIn(key, schema,
                             "%s is rewritten on every exit; showing it as "
                             "editable is a setting that appears broken" % key)

    def test_the_preference_itself_is_visible(self):
        self.assertIn("remember_window_size", cfg.settings_schema())

    def test_the_visible_preference_is_labelled(self):
        self.assertNotEqual(cfg.label_for("remember_window_size"),
                            "remember_window_size")

    def test_the_visible_preference_is_in_a_curated_section(self):
        curated = {k for _title, keys in cfg.SECTIONS for k in keys}
        self.assertIn("remember_window_size", curated)


class TitleTest(unittest.TestCase):
    """The title is built in player.py; assert on its shape without
    importing player (which pulls in libmpv)."""

    def _title_line(self):
        path = os.path.join(ROOT, "jellyfin_mpv_shim", "player.py")
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                if 'mpv_options["title"]' in line:
                    return line
        self.fail("player.py no longer sets a window title")

    def test_the_app_name_is_used(self):
        self.assertIn("USER_APP_NAME", self._title_line())

    def test_the_media_title_is_expanded_by_mpv(self):
        """mpv evaluates this live, so playback updates the title without
        us pushing anything."""
        self.assertIn("${?media-title:${media-title} - }", self._title_line())

    def test_the_app_name_is_not_mpv(self):
        self.assertNotEqual(USER_APP_NAME, "mpv")


if __name__ == "__main__":
    unittest.main()
