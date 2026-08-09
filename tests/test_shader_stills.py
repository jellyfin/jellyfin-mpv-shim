"""A shader pack must not run over a still image (#13).

`VideoProfileManager.load_profile` is applied once — from the menu, or
restored at startup from ``shader_pack_profile`` — and then left on the mpv
instance. Nothing on the play path touched it, so an anime-upscaling chain
ran over a photograph, and over a comic page at 1600x2400 or larger, where
it is expensive as well as wrong.

The fix is a *suspension*, and that distinction is the whole point: the
obvious `unload_profile` clears ``current_profile``, which the menu's
selection and ``menu_handle``'s persistence both read — so opening a photo
would have silently reset the user's chosen profile.
"""

import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.video_profile import VideoProfileManager  # noqa: E402


def _manager(current="anime4k"):
    """A VideoProfileManager with the file/network parts stubbed.

    __init__ reads a shader pack off disk and probes mpv for every setting
    the pack names, none of which this behaviour depends on.
    """
    m = VideoProfileManager.__new__(VideoProfileManager)
    m.player = mock.Mock()
    m.player.glsl_shaders = ["/packs/shaders/a.glsl"]
    m.used_settings = {"scale", "cscale"}
    m.defaults = {"scale": "bilinear", "cscale": "bilinear"}
    m.current_profile = current
    m._suspended = None
    m.profiles = {"anime4k": {"shaders": ["a.glsl"]}}
    m.default_groups = []
    m.groups = {}
    m.revert_ignore = set()
    m.shader_pack = "/packs"
    return m


class SuspendKeepsTheChoiceTest(unittest.TestCase):

    def test_suspending_clears_the_shaders(self):
        m = _manager()
        m.suspend_for_still()
        self.assertEqual(m.player.glsl_shaders, [])

    def test_suspending_puts_the_settings_back_to_their_defaults(self):
        m = _manager()
        m.suspend_for_still()
        self.assertEqual(m.player.scale, "bilinear")
        self.assertEqual(m.player.cscale, "bilinear")

    def test_but_it_keeps_the_profile_name(self):
        """The regression this is shaped to avoid: unload_profile clears
        current_profile, and the menu selection and the remembered setting
        both read it. Opening a photo would have reset the user's choice."""
        m = _manager()
        m.suspend_for_still()
        self.assertEqual(m.current_profile, "anime4k")

    def test_resuming_reapplies_it(self):
        m = _manager()
        m.suspend_for_still()
        with mock.patch.object(m, "load_profile") as load:
            m.resume_after_still()
        load.assert_called_once_with("anime4k", reset=False)

    def test_resuming_without_a_suspension_does_nothing(self):
        # This runs on every playback start and almost none of them follow
        # a still; re-applying a whole profile each time would be waste.
        m = _manager()
        with mock.patch.object(m, "load_profile") as load:
            m.resume_after_still()
        load.assert_not_called()

    def test_suspending_twice_does_not_lose_the_name(self):
        """Photos arrive in queues and every one runs the play path. A
        second suspension must not park None over the real profile."""
        m = _manager()
        m.suspend_for_still()
        m.suspend_for_still()
        with mock.patch.object(m, "load_profile") as load:
            m.resume_after_still()
        load.assert_called_once_with("anime4k", reset=False)

    def test_with_no_profile_chosen_there_is_nothing_to_do(self):
        m = _manager(current=None)
        m.suspend_for_still()
        self.assertIsNone(m._suspended)
        with mock.patch.object(m, "load_profile") as load:
            m.resume_after_still()
        load.assert_not_called()

    def test_resume_is_not_a_second_unload(self):
        # load_profile(reset=False): unload already ran, and unloading
        # again would write every default a second time for nothing.
        m = _manager()
        m.suspend_for_still()
        with mock.patch.object(m, "unload_profile") as unload:
            with mock.patch.object(m, "load_profile"):
                m.resume_after_still()
        unload.assert_not_called()


class TheStillPathsAskForItTest(unittest.TestCase):
    """Both kinds of still reach the suspension, by different routes: a
    photo is ordinary playback (_play_media) and a comic page is not
    (show_picture). Checked from the source, because constructing either
    path needs a real mpv."""

    @staticmethod
    def _source(name):
        import os
        import jellyfin_mpv_shim
        base = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
        with open(os.path.join(base, name), encoding="utf-8") as fh:
            return fh.read()

    def test_photo_playback_suspends_and_video_resumes(self):
        src = self._source("player.py")
        self.assertIn("suspend_for_still()", src)
        # apply_for_item, not resume_after_still: the play path resolves the
        # profile per item now (series -> library -> the setting), and
        # putting back a suspension is one of the things that does. The
        # property is unchanged -- a video after a still gets its profile
        # back -- so this still names the call that delivers it.
        self.assertIn("apply_for_item(", src)

    def test_the_comic_reader_suspends_and_restores(self):
        src = self._source("player_window.py")
        self.assertIn("_suspend_shaders_for_still()", src)
        self.assertIn("_resume_shaders_after_still()", src)


if __name__ == "__main__":
    unittest.main()


class ThePackDoesNotSetHwdecTest(unittest.TestCase):
    """default-shader-pack pulls a "hwdec-default" group into every
    profile, setting hwdec to auto-copy (d3d11va in pack-next). That was
    fine while hwdec was nobody's setting; it is now a user-facing one that
    defaults off *because* some drivers handle hardware decoding badly, and
    a shader profile switching it back on reintroduces exactly that failure
    in the last place anyone would look for it."""

    def _apply(self, group_settings):
        m = _manager()
        m.groups = {"g": {"settings": group_settings}}
        m.defaults = {"scale": "bilinear", "hwdec": "no"}
        m.used_settings = set()
        m.forced_hwdec = None
        m._sets_vf = False
        m._names_direct_hwdec = False
        applied, shaders = [], []
        m.process_setting_group("g", applied, shaders)
        return m, applied

    def test_the_packs_hwdec_is_dropped(self):
        _m, applied = self._apply([["hwdec", "auto-copy"],
                                   ["scale", "ewa_lanczossharp"]])
        self.assertEqual(applied, [("scale", "ewa_lanczossharp")])

    def test_but_a_named_decoder_is_applied(self):
        """The line is policy vs requirement. `auto-copy` says "use hardware
        decoding if you can", which is an opinion about the machine and not
        the pack's to have. `d3d11va` names the decoder the profile's
        d3d11vpp filter needs to exist at all, and choosing that profile is
        opting in."""
        m, applied = self._apply([["hwdec", "auto-copy"],
                                  ["hwdec", "d3d11va"]])
        self.assertEqual(applied, [("hwdec", "d3d11va")])
        self.assertEqual(m.forced_hwdec, "d3d11va")

    def test_a_profile_that_names_nothing_forces_nothing(self):
        m, _applied = self._apply([["hwdec", "auto-copy"]])
        self.assertIsNone(m.forced_hwdec)

    def test_a_pack_turning_it_off_is_also_a_requirement(self):
        """Nothing in the current pack sets `no`, but if one did it would
        mean "my shaders need software frames" — a statement about the
        profile, not an opinion about the machine [iw]. So it survives,
        where `auto` and `auto-copy` do not."""
        m, applied = self._apply([["hwdec", "no"]])
        self.assertEqual(applied, [("hwdec", "no")])
        self.assertEqual(m.forced_hwdec, "no")

    def test_the_users_config_outranks_the_pack_too(self):
        """A profile applies its settings directly, so without this the
        pack would slip past the mpv.conf pin between one file and the
        next — the per-item write in _play_media is not the only writer."""
        with mock.patch("jellyfin_mpv_shim.video_profile."
                        "hwdec_pinned_by_config", return_value="vaapi"):
            m, applied = self._apply([["hwdec", "d3d11va"]])
        self.assertEqual(applied, [])
        self.assertIsNone(m.forced_hwdec)

    def test_it_never_becomes_a_setting_the_profile_reverts(self):
        """Dropped before used_settings, so unload_profile does not write
        hwdec either — which also keeps the still-image suspension from
        quietly changing the decoder."""
        m, _applied = self._apply([["hwdec", "auto-copy"]])
        self.assertNotIn("hwdec", m.used_settings)

    def test_everything_else_in_the_group_still_applies(self):
        _m, applied = self._apply([["scale", "ewa_lanczossharp"],
                                   ["hwdec", "auto-copy"]])
        self.assertIn(("scale", "ewa_lanczossharp"), applied)


class CopyBackIsAnUpgradeNotASwitchTest(unittest.TestCase):
    """When does a profile actually need frames in system RAM?

    Not when it says ``hwdec: auto-copy`` — every profile in the shipped
    pack says that, from a time when being cautious about it cost nothing
    [iw], and a glsl shader runs inside the GPU renderer on frames that are
    already there. What needs system RAM is a real ``vf``.

    And a profile naming a **direct** hwdec mode beside its filter is
    saying the opposite. In the shipped pack that is exactly ``rtx-vsr``:
    ``format=nv12,d3d11vpp=scale=2:scaling-mode=nvidia`` is a Direct3D
    video-processor filter operating on d3d11 surfaces, so copying back
    would break the only profile in the pack that has a filter at all.
    """

    def _wants_copy(self, group_settings):
        m = _manager()
        m.groups = {"g": {"settings": group_settings}}
        m.defaults = {"hwdec": "no", "vf": "", "gpu_api": "auto",
                      "scale": "bilinear", "profile": ""}
        m.used_settings = set()
        m._sets_vf = False
        m._names_direct_hwdec = False
        m.process_setting_group("g", [], [])
        return m._wants_copy()

    def test_the_blanket_auto_copy_is_not_evidence(self):
        # "hwdec-default", which every profile in the pack pulls in.
        self.assertFalse(self._wants_copy([["hwdec", "auto-copy"],
                                           ["profile", "gpu-hq"]]))

    def test_a_shader_only_profile_needs_nothing(self):
        self.assertFalse(self._wants_copy([["scale", "ewa_lanczossharp"]]))

    def test_a_real_filter_does(self):
        self.assertTrue(self._wants_copy([["vf", "lavfi=[negate]"]]))

    def test_but_not_when_the_profile_wants_gpu_frames(self):
        # The shipped rtx-vsr group, verbatim.
        self.assertFalse(self._wants_copy([
            ["hwdec", "d3d11va"],
            ["gpu_api", "d3d11"],
            ["vf", "format=nv12,d3d11vpp=scale=2:scaling-mode=nvidia"]]))

    def test_unloading_forgets_it(self):
        m = _manager()
        m.wants_copy_hwdec = True
        m.unload_profile()
        self.assertFalse(m.wants_copy_hwdec)

    def test_a_reset_free_load_recomputes_it(self):
        """load_profile(reset=False) skips unload — the still-image resume
        and the startup restore both use it — so a stale True would outlive
        the profile that set it."""
        m = _manager()
        m.wants_copy_hwdec = True
        m.groups = {"g": {"settings": [["scale", "bilinear"]]}}
        m.defaults = {"scale": "bilinear"}
        m.profiles = {"plain": {"setting-groups": ["g"]}}
        m.load_profile("plain", reset=False)
        self.assertFalse(m.wants_copy_hwdec)


class HwdecUpgradeTest(unittest.TestCase):
    """`needs_copy` upgrades, it never enables."""

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        self.settings = settings
        self._saved = settings.hwdec
        self.addCleanup(lambda: setattr(settings, "hwdec", self._saved))

    def _for(self, mode, height=None, needs_copy=False):
        from jellyfin_mpv_shim import mpv_options
        self.settings.hwdec = mode
        return mpv_options.hwdec_for(height, needs_copy)

    def test_off_stays_off_however_much_something_wants_frames(self):
        # Turning hardware decoding off is not a preference about *which*
        # hardware decoding, and a filter is not a reason to overrule it.
        self.assertEqual(self._for("no", 2160, needs_copy=True), "no")

    def test_a_direct_mode_becomes_the_copy_variant(self):
        self.assertEqual(self._for("auto", 2160, needs_copy=True),
                         "auto-copy")

    def test_the_threshold_mode_upgrades_only_where_it_was_on(self):
        self.assertEqual(self._for("over-1080p", 2160, needs_copy=True),
                         "auto-copy")
        self.assertEqual(self._for("over-1080p", 1080, needs_copy=True),
                         "no")

    def test_an_explicit_copy_choice_is_unchanged(self):
        self.assertEqual(self._for("auto-copy", 2160, needs_copy=True),
                         "auto-copy")

    def test_without_a_filter_nothing_is_upgraded(self):
        self.assertEqual(self._for("auto", 2160), "auto")
        self.assertEqual(self._for("over-1080p", 2160), "auto")
