"""`--reset-shaders`: the escape hatch for a profile that breaks video.

A remembered profile is reapplied by VideoProfileManager at construction,
before there is any menu to turn it off, and a forced graphics API can leave
MPV with no window to press `k` in. Both are cleared here, from the command
line, before a player exists.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import args as args_mod  # noqa: E402
from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.video_profile import (  # noqa: E402
    reset_saved_shader_settings,
)


class ResetSavedShaderSettingsTest(unittest.TestCase):
    def setUp(self):
        for key in ("shader_pack_profile", "shader_pack_gpu_api"):
            self.addCleanup(setattr, settings, key, getattr(settings, key))
        saver = mock.patch.object(settings, "save", mock.Mock())
        self.save = saver.start()
        self.addCleanup(saver.stop)

    def test_clears_both_settings(self):
        settings.shader_pack_profile = "Anime4K x2 Faithful (For HD)"
        settings.shader_pack_gpu_api = "opengl"
        changed = reset_saved_shader_settings()
        self.assertIsNone(settings.shader_pack_profile)
        self.assertEqual(settings.shader_pack_gpu_api, "auto")
        self.assertEqual(dict(changed), {
            "shader_pack_profile": "Anime4K x2 Faithful (For HD)",
            "shader_pack_gpu_api": "opengl",
        })
        self.save.assert_called_once()

    def test_reports_only_what_changed(self):
        settings.shader_pack_profile = None
        settings.shader_pack_gpu_api = "d3d11"
        self.assertEqual([k for k, _v in reset_saved_shader_settings()],
                         ["shader_pack_gpu_api"])

    def test_no_write_when_already_default(self):
        # Otherwise every launch with the flag rewrites the config file and
        # claims to have fixed something.
        settings.shader_pack_profile = None
        settings.shader_pack_gpu_api = "auto"
        self.assertEqual(reset_saved_shader_settings(), [])
        self.save.assert_not_called()

    def test_auto_is_recognised_whatever_its_case(self):
        settings.shader_pack_profile = None
        settings.shader_pack_gpu_api = "AUTO"
        self.assertEqual(reset_saved_shader_settings(), [])

    def test_profile_is_cleared_even_when_remember_is_off(self):
        # The startup reapply does not consult shader_pack_remember, so a
        # stale value keeps breaking video regardless of that setting.
        self.addCleanup(setattr, settings, "shader_pack_remember",
                        settings.shader_pack_remember)
        settings.shader_pack_remember = False
        settings.shader_pack_profile = "Generic (FSRCNNX)"
        settings.shader_pack_gpu_api = "auto"
        reset_saved_shader_settings()
        self.assertIsNone(settings.shader_pack_profile)


class ResetShadersArgTest(unittest.TestCase):
    def _parse(self, argv):
        return args_mod._build_parser().parse_args(argv)

    def test_flag_defaults_off(self):
        self.assertFalse(self._parse([]).reset_shaders)

    def test_flag_parses(self):
        self.assertTrue(self._parse(["--reset-shaders"]).reset_shaders)

    def test_it_is_a_normal_launch_flag(self):
        # Not a subcommand: the point is to start the player with working
        # video, not to fix the config and exit.
        self.assertNotIn("reset-shaders", args_mod.COMMANDS)
        self.assertNotIn("reset_shaders", args_mod.COMMANDS)


if __name__ == "__main__":
    unittest.main()
