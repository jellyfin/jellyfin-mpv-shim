"""Profiles a machine cannot run are not offered on it.

Most profiles are shaders, and shaders run anywhere MPV does. A few are
not: RTX Video Super Resolution is a Direct3D 11 video filter, so on Linux
or macOS the entry can only ever fail. A pack declares those with
``"platforms": ["windows"]``; everything else stays unrestricted, so packs
written before the key exists behave exactly as they did.

The menu row arithmetic is tested with it, because filtering is what
breaks it: the highlighted row has to be the row the profile landed on,
not its index in the unfiltered pack.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import video_profile  # noqa: E402
from jellyfin_mpv_shim.shader_overrides import ShaderOverrides  # noqa: E402
from jellyfin_mpv_shim.video_profile import VideoProfileManager  # noqa: E402

PROFILES = {
    "generic": {"displayname": "FSRCNNX"},
    "rtx-vsr": {"displayname": "RTX Video Super Resolution",
                "platforms": ["windows"],
                "setting-groups": ["hw-d3d11va-rtxvsr"]},
    "nvscaler": {"displayname": "Nvidia Image Scaler"},
}


class FakeMenu:
    def __init__(self):
        self.title = None
        self.entries = []
        self.selected = None

    def put_menu(self, title, entries=None, selected=0):
        self.title, self.entries, self.selected = title, entries or [], selected


class FakePlayerManager:
    """Enough player for the menu to ask what is playing.

    Modelled rather than left off: ``_menu_item`` reaches through this to
    the item, and a manager without one does not leave the scope menu
    untested -- it makes it raise where nothing is looking."""

    def __init__(self, item=None):
        self.item = item

    def get_video(self):
        if self.item is None:
            return None
        return type("V", (), {"item": self.item, "client": None})()


def make_manager(current=None, item=None):
    mgr = VideoProfileManager.__new__(VideoProfileManager)
    mgr.profiles = PROFILES
    mgr.menu = FakeMenu()
    mgr.current_profile = current
    mgr.groups = {}
    mgr.default_groups = []
    mgr.defaults = {}
    mgr.revert_ignore = set()
    mgr.used_settings = set()
    mgr.playerManager = FakePlayerManager(item)
    # path=None: no file, no writes. The store is real otherwise, because
    # what these tests are about is which rows the menu draws.
    mgr.overrides = ShaderOverrides(None)
    mgr.active_scope = "default"
    mgr._library_ids = {}
    mgr._menu_scope = "default"
    # The default scope's value IS the loaded one here: these tests
    # have no overrides, so the two cannot differ.
    mgr.session_default = current
    mgr.suppressed = False
    return mgr


def rows(mgr):
    return [row[2] for row in mgr.menu.entries]


class PlatformGatingTests(unittest.TestCase):
    def test_unrestricted_profile_runs_anywhere(self):
        for platform in ("windows", "macos", "linux"):
            with self.subTest(platform=platform), \
                    mock.patch.object(video_profile, "PLATFORM", platform):
                self.assertTrue(
                    VideoProfileManager.profile_is_available(PROFILES["generic"])
                )

    def test_gated_profile_only_on_its_platform(self):
        with mock.patch.object(video_profile, "PLATFORM", "windows"):
            self.assertTrue(
                VideoProfileManager.profile_is_available(PROFILES["rtx-vsr"])
            )
        for platform in ("macos", "linux"):
            with self.subTest(platform=platform), \
                    mock.patch.object(video_profile, "PLATFORM", platform):
                self.assertFalse(
                    VideoProfileManager.profile_is_available(PROFILES["rtx-vsr"])
                )

    def test_menu_hides_what_this_machine_cannot_run(self):
        mgr = make_manager()
        with mock.patch.object(video_profile, "PLATFORM", "linux"), \
                mock.patch.object(video_profile.settings,
                                  "shader_pack_subtype", "lq"):
            mgr.profile_menu("default")
        self.assertEqual(rows(mgr), [None, "generic", "nvscaler"])

        mgr = make_manager()
        with mock.patch.object(video_profile, "PLATFORM", "windows"), \
                mock.patch.object(video_profile.settings,
                                  "shader_pack_subtype", "lq"):
            mgr.profile_menu("default")
        self.assertEqual(rows(mgr), [None, "generic", "rtx-vsr", "nvscaler"])

    def test_selected_row_survives_filtering(self):
        # nvscaler is the third profile in the pack but the second row on a
        # machine where rtx-vsr is filtered out -- and row 0 is "None".
        mgr = make_manager(current="nvscaler")
        with mock.patch.object(video_profile, "PLATFORM", "linux"), \
                mock.patch.object(video_profile.settings,
                                  "shader_pack_subtype", "lq"):
            mgr.profile_menu("default")
        self.assertEqual(mgr.menu.selected, 2)
        self.assertEqual(mgr.menu.entries[mgr.menu.selected][2], "nvscaler")

    def test_loading_a_gated_profile_elsewhere_fails_cleanly(self):
        mgr = make_manager()
        mgr.player = object()      # touching it at all would be the bug
        with mock.patch.object(video_profile, "PLATFORM", "linux"):
            self.assertIs(mgr.load_profile("rtx-vsr", reset=False), False)


if __name__ == "__main__":
    unittest.main()
