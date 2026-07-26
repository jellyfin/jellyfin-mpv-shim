"""The shader pack's OpenGL pins are advisory, not obeyed.

The pack pairs `gpu_api: opengl` with `fbo_format: rgba16f` — a format name
that only the OpenGL backend has. On Direct3D 11 the same format is
`rgba16hf`, so the pack's value fails, MPV drops to dumb mode, and dumb
mode disables every user shader. Both pins are dropped: the format because
MPV's `auto` asks for the same thing portably, the API because obeying it
costs HDR on Windows, where MPV would otherwise autoprobe d3d11.

`fbo_format` goes unconditionally; `gpu_api` comes back only if the user
names an API in `shader_pack_gpu_api`. Nothing else in the group may be
dropped along with them.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.video_profile import VideoProfileManager  # noqa: E402

# The real group, from default-shader-pack's pack.json.
HWDEC_DEFAULT = [
    ["hwdec", "auto-copy"],
    ["profile", "gpu-hq"],
    ["gpu_api", "opengl"],
    ["fbo_format", "rgba16f"],
]


# A group that means its API, in the shape issue #8 of default-shader-pack
# proposes for RTX Video Super Resolution. d3d11 is not a preference there:
# the video filter does not exist on any other backend.
RTX_VSR = [
    ["hwdec", "d3d11va"],
    ["gpu_api", "d3d11"],
    ["vf", "format=nv12,d3d11vpp=scale=2:scaling-mode=nvidia"],
]


class FakeProfileManager:
    """Just enough of VideoProfileManager to run process_setting_group."""

    def __init__(self, group=None):
        self.groups = {"hwdec-default": {"settings": group or HWDEC_DEFAULT}}
        self.defaults = {"hwdec": "no", "profile": "", "fbo_format": "auto",
                         "vf": ""}
        self.revert_ignore = {"gpu_api", "profile", "hwdec"}
        self.used_settings = set()
        self.shader_pack = "/nonexistent"

    process_setting_group = VideoProfileManager.process_setting_group
    api_setting_override = staticmethod(VideoProfileManager.api_setting_override)

    def applied(self):
        out = []
        self.process_setting_group("hwdec-default", out, [])
        return dict(out)


def applied(group=None):
    return FakeProfileManager(group).applied()


class ShaderGpuApiTests(unittest.TestCase):
    def setUp(self):
        self.original = settings.shader_pack_gpu_api
        self.addCleanup(
            lambda: setattr(settings, "shader_pack_gpu_api", self.original)
        )

    def test_pins_are_ignored_by_default(self):
        settings.shader_pack_gpu_api = "auto"
        applied = FakeProfileManager().applied()
        self.assertNotIn("gpu_api", applied)
        self.assertNotIn("fbo_format", applied)
        # The rest of the group still has to be applied.
        self.assertEqual(applied["hwdec"], "auto-copy")
        self.assertEqual(applied["profile"], "gpu-hq")

    def test_fbo_format_stays_dropped_when_an_api_is_forced(self):
        # rgba16f is valid on OpenGL, but MPV's auto asks for the same
        # thing, and the pack's name is a trap on any other backend.
        settings.shader_pack_gpu_api = "opengl"
        self.assertNotIn("fbo_format", FakeProfileManager().applied())

    def test_unset_value_behaves_as_auto(self):
        settings.shader_pack_gpu_api = None
        self.assertNotIn("gpu_api", FakeProfileManager().applied())

    def test_user_choice_replaces_the_pin(self):
        for choice in ("vulkan", "d3d11", "opengl"):
            with self.subTest(choice=choice):
                settings.shader_pack_gpu_api = choice
                self.assertEqual(
                    FakeProfileManager().applied()["gpu_api"], choice
                )

    def test_choice_is_case_insensitive(self):
        settings.shader_pack_gpu_api = "OpenGL"      # hand-edited config.json
        self.assertEqual(FakeProfileManager().applied()["gpu_api"], "opengl")

    def test_an_api_the_profile_actually_needs_is_honoured(self):
        # Only the legacy opengl pin is refused. A d3d11 filter profile is
        # not expressing a preference — it cannot run anywhere else.
        settings.shader_pack_gpu_api = "auto"
        out = applied(RTX_VSR)
        self.assertEqual(out["gpu_api"], "d3d11")
        self.assertEqual(out["hwdec"], "d3d11va")

    def test_the_user_still_outranks_a_profile_that_names_an_api(self):
        settings.shader_pack_gpu_api = "vulkan"
        self.assertEqual(applied(RTX_VSR)["gpu_api"], "vulkan")


if __name__ == "__main__":
    unittest.main()
