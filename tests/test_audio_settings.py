"""Audio output modes: what we hand mpv, and the one combination that must
never happen.

The load-bearing fact behind all of this is that ``audio-spdif`` and a PCM
audio filter are mutually exclusive *per track*, and mpv does not arbitrate
between them. Ask for both and the filter chain fails to build
("unsupported conversion: spdif-ac3 -> floatp"); mpv then plays the file
with no audio output at all -- no error, no fallback to PCM, the clock just
runs in silence. jellyfin-media-player shipped exactly that combination for
S/PDIF + AC3 passthrough.

So the tests that matter here are the negative ones: night mode must clear
passthrough, and the AC3 encoder must never be attached to a track that is
being passed through.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import (  # noqa: E402
    AUDIO_MODE_CHANNELS,
    AUDIO_PASSTHROUGH_CODECS,
    audio_spdif_codecs,
    audio_wants_ac3_encode,
)

ALL_ON = lambda _codec: True        # noqa: E731
ALL_OFF = lambda _codec: False      # noqa: E731

MODES = ["auto", "stereo", "optical", "hdmi"]


class SpdifCodecsTest(unittest.TestCase):
    def test_auto_and_stereo_pass_nothing_through(self):
        # "Default" means we touch nothing; stereo is a downmix, which is the
        # opposite of handing the receiver an undecoded stream.
        for mode in ("auto", "stereo"):
            self.assertEqual(audio_spdif_codecs(mode, False, ALL_ON), [])

    def test_optical_is_limited_to_what_the_cable_carries(self):
        # S/PDIF has ~1.5 Mbps: AC3 and DTS core fit, nothing else does.
        self.assertEqual(audio_spdif_codecs("optical", False, ALL_ON),
                         ["ac3", "dts"])

    def test_hdmi_offers_the_high_bitrate_formats_too(self):
        codecs = audio_spdif_codecs("hdmi", False, ALL_ON)
        self.assertIn("truehd", codecs)
        self.assertIn("eac3", codecs)
        self.assertIn("dts-hd", codecs)

    def test_unticked_codecs_are_dropped(self):
        only_ac3 = audio_spdif_codecs("hdmi", False, lambda c: c == "ac3")
        self.assertEqual(only_ac3, ["ac3"])

    def test_everything_unticked_is_the_same_as_no_passthrough(self):
        for mode in MODES:
            self.assertEqual(audio_spdif_codecs(mode, False, ALL_OFF), [])

    def test_dts_hd_supersedes_dts(self):
        # mpv: "If both dts and dts-hd are specified, it behaves equivalent to
        # specifying dts-hd only." Drop the redundant entry so the value reads
        # the way it behaves.
        codecs = audio_spdif_codecs("hdmi", False, ALL_ON)
        self.assertIn("dts-hd", codecs)
        self.assertNotIn("dts", codecs)

    def test_dts_survives_when_dts_hd_is_unticked(self):
        codecs = audio_spdif_codecs("hdmi", False, lambda c: c != "dts-hd")
        self.assertIn("dts", codecs)

    def test_night_mode_clears_passthrough_in_every_mode(self):
        # The whole point: a compressor cannot run on a stream we never
        # decoded. If this regresses, night mode silences passthrough content.
        for mode in MODES:
            self.assertEqual(
                audio_spdif_codecs(mode, True, ALL_ON), [],
                "%s still passes through with night mode on" % mode)

    def test_unknown_mode_is_inert(self):
        # A config written by a newer version must not turn passthrough on.
        self.assertEqual(audio_spdif_codecs("quadraphonic", False, ALL_ON), [])


class Ac3EncodeTest(unittest.TestCase):
    """lavcac3enc exists to get surround across an optical cable when the
    track is not already something the cable can carry."""

    def test_never_outside_optical(self):
        # HDMI carries multichannel PCM natively; re-encoding would throw away
        # quality for nothing. auto/stereo have no business encoding either.
        for mode in ("auto", "stereo", "hdmi"):
            self.assertFalse(audio_wants_ac3_encode(mode, "aac", ["ac3"]))

    def test_encodes_a_track_the_cable_cannot_carry(self):
        self.assertTrue(
            audio_wants_ac3_encode("optical", "aac", ["ac3", "dts"]))

    def test_leaves_a_passed_through_track_alone(self):
        # THE regression test. Both at once = silence.
        self.assertFalse(
            audio_wants_ac3_encode("optical", "ac3", ["ac3", "dts"]))
        self.assertFalse(
            audio_wants_ac3_encode("optical", "dts", ["ac3", "dts"]))

    def test_codec_match_is_case_insensitive(self):
        # mpv reports lowercase, but nothing guarantees it.
        self.assertFalse(
            audio_wants_ac3_encode("optical", "AC3", ["ac3", "dts"]))

    def test_encodes_ac3_when_the_user_unticked_ac3_passthrough(self):
        # Unticking AC3 means "my receiver can't decode it" -- so encode it
        # rather than passing it through anyway.
        self.assertTrue(audio_wants_ac3_encode("optical", "ac3", ["dts"]))

    def test_encode_others_off_declines_the_encoder(self):
        # Opt-out for receivers where the AC3 encoder adds audible delay.
        # Those tracks go out as stereo PCM: S/PDIF cannot carry multichannel
        # PCM either, so there is no third option.
        self.assertFalse(audio_wants_ac3_encode(
            "optical", "aac", ["ac3", "dts"], False))
        self.assertFalse(audio_wants_ac3_encode(
            "optical", None, ["ac3", "dts"], False))

    def test_encode_others_off_leaves_passthrough_working(self):
        # Declining the encoder must not disturb the tracks the cable can
        # carry directly -- that is the whole point of still being in optical.
        codecs = audio_spdif_codecs("optical", False, ALL_ON)
        self.assertEqual(codecs, ["ac3", "dts"])
        self.assertFalse(audio_wants_ac3_encode("optical", "ac3", codecs, False))

    def test_encode_others_defaults_to_on(self):
        # Surround over optical is the reason the mode exists.
        self.assertTrue(audio_wants_ac3_encode("optical", "aac", ["ac3"]))

    def test_unknown_codec_gets_encoded(self):
        # No track info yet: encoding is the safe direction. It degrades to a
        # needless re-encode, where the other direction degrades to silence.
        self.assertTrue(audio_wants_ac3_encode("optical", None, ["ac3"]))

    def test_never_both_passthrough_and_encode_for_the_same_track(self):
        """The invariant, stated directly over every combination."""
        codecs = ["ac3", "dts", "eac3", "dts-hd", "truehd", "aac", "flac"]
        for mode in MODES:
            for night in (False, True):
                for encode in (False, True):
                    spdif = audio_spdif_codecs(mode, night, ALL_ON)
                    for codec in codecs:
                        passed = codec in spdif
                        encoded = audio_wants_ac3_encode(
                            mode, codec, spdif, encode)
                        self.assertFalse(
                            passed and encoded,
                            "%s/%s (night=%s, encode=%s) would both pass "
                            "through and encode"
                            % (mode, codec, night, encode))


class ModeTablesTest(unittest.TestCase):
    def test_auto_sets_no_channel_layout(self):
        # "Default (auto)" is defined by touching nothing.
        self.assertNotIn("auto", AUDIO_MODE_CHANNELS)

    def test_stereo_forces_two_channels(self):
        self.assertEqual(AUDIO_MODE_CHANNELS["stereo"], "2.0")

    def test_surround_modes_fall_back_to_stereo(self):
        # Each layout list must end at 2.0 so a stereo-only sink still works.
        for mode in ("optical", "hdmi"):
            self.assertTrue(AUDIO_MODE_CHANNELS[mode].endswith("2.0"))

    def test_optical_is_a_subset_of_hdmi(self):
        self.assertTrue(set(AUDIO_PASSTHROUGH_CODECS["optical"])
                        <= set(AUDIO_PASSTHROUGH_CODECS["hdmi"]))

    def test_every_codec_has_a_setting_behind_it(self):
        from jellyfin_mpv_shim.conf import Settings

        for codecs in AUDIO_PASSTHROUGH_CODECS.values():
            for codec in codecs:
                key = "audio_passthrough_" + codec.replace("-", "_")
                self.assertIn(key, Settings.__annotations__,
                              "%s has no config key" % codec)


class FakePlayer:
    """Records what apply_audio_settings pushes at mpv."""

    def __init__(self):
        self.props = {}
        self.commands = []

    def __setattr__(self, name, value):
        if name in ("props", "commands"):
            return object.__setattr__(self, name, value)
        self.props[name] = value

    def command(self, *args):
        self.commands.append(args)

    @property
    def filters(self):
        """Labels currently attached, in the order they were added."""
        out = []
        for cmd in self.commands:
            if cmd[:2] == ("af", "remove"):
                label = cmd[2].lstrip("@")
                out = [f for f in out if f != label]
            elif cmd[:2] == ("af", "add"):
                out.append(cmd[2].split(":", 1)[0].lstrip("@"))
        return out


class ApplyAudioSettingsTest(unittest.TestCase):
    """The apply path itself, against a fake mpv."""

    def setUp(self):
        from jellyfin_mpv_shim import player
        from jellyfin_mpv_shim.conf import settings

        self.settings = settings
        self._saved = (settings.audio_mode, settings.audio_night_mode)
        self.pm = player.PlayerManager.__new__(player.PlayerManager)
        self.pm._audio_configured = False
        self.pm._player = FakePlayer()

    def tearDown(self):
        self.settings.audio_mode, self.settings.audio_night_mode = self._saved

    def apply(self, mode, night=False):
        self.settings.audio_mode = mode
        self.settings.audio_night_mode = night
        self.pm.apply_audio_settings()
        return self.pm._player

    def test_auto_touches_nothing_at_all(self):
        # The contract of "Default": a user who configured audio in their own
        # mpv.conf must not have it overwritten.
        p = self.apply("auto")
        self.assertEqual(p.props, {})
        self.assertEqual(p.commands, [])

    def test_stereo_forces_the_layout_and_normalizes(self):
        p = self.apply("stereo")
        self.assertEqual(p.props["audio_channels"], "2.0")
        self.assertTrue(p.props["audio_normalize_downmix"])
        self.assertEqual(p.props["audio_spdif"], "")

    def test_hdmi_sets_passthrough(self):
        p = self.apply("hdmi")
        self.assertIn("truehd", p.props["audio_spdif"])
        self.assertFalse(p.props["audio_normalize_downmix"])

    def test_night_mode_attaches_the_filter(self):
        p = self.apply("hdmi", night=True)
        self.assertIn("jfnight", p.filters)
        # ...and drops passthrough with it.
        self.assertEqual(p.props["audio_spdif"], "")

    def test_switching_night_mode_off_removes_the_filter(self):
        self.apply("hdmi", night=True)
        p = self.apply("hdmi", night=False)
        self.assertNotIn("jfnight", p.filters)
        self.assertIn("truehd", p.props["audio_spdif"])

    def test_returning_to_auto_undoes_what_we_applied(self):
        """The fast path must not strand settings from a previous mode.

        Skipping the work in auto mode unconditionally would leave a forced
        channel layout, passthrough and the night-mode filter attached with
        no way to switch them off short of a restart.
        """
        self.apply("hdmi", night=True)
        p = self.apply("auto", night=False)
        self.assertEqual(p.props["audio_spdif"], "")
        self.assertEqual(p.props["audio_channels"], "auto-safe")
        self.assertFalse(p.props["audio_normalize_downmix"])
        self.assertEqual(p.filters, [])

    def test_leaving_optical_drops_the_ac3_encoder(self):
        self.settings.audio_mode = "optical"
        self.settings.audio_night_mode = False
        self.pm._player.commands.append(("af", "add", "@jfac3:lavcac3enc"))
        p = self.apply("hdmi")
        self.assertNotIn("jfac3", p.filters)


class SettingsFormTest(unittest.TestCase):
    """The passthrough toggles are shown per mode; a hidden one must not
    reappear somewhere else in the form."""

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        from jellyfin_mpv_shim.mpvtk_browser import config

        self.cfg = config
        self.settings = settings
        self._saved = settings.audio_mode

    def tearDown(self):
        self.settings.audio_mode = self._saved

    def _keys(self):
        return {k for _t, keys in self.cfg.sections() for k in keys}

    def test_no_passthrough_toggles_in_auto_or_stereo(self):
        for mode in ("auto", "stereo"):
            self.settings.audio_mode = mode
            self.assertEqual(self.cfg.visible_passthrough_keys(), [])

    def test_optical_offers_only_ac3_and_dts(self):
        self.settings.audio_mode = "optical"
        self.assertEqual(self.cfg.visible_passthrough_keys(),
                         ["audio_passthrough_ac3", "audio_passthrough_dts"])

    def test_hdmi_offers_all_five(self):
        self.settings.audio_mode = "hdmi"
        self.assertEqual(len(self.cfg.visible_passthrough_keys()), 5)

    def test_hidden_toggles_do_not_leak_into_advanced(self):
        # sections() puts anything uncurated under "Advanced"; a key filtered
        # out of the Audio group must not resurface there.
        for mode in MODES:
            self.settings.audio_mode = mode
            shown = set(self.cfg.visible_passthrough_keys())
            for _codec, key in self.cfg.AUDIO_PASSTHROUGH_KEYS:
                if key not in shown:
                    self.assertNotIn(key, self._keys(),
                                     "%s leaked in %s mode" % (key, mode))

    def test_encode_toggle_is_offered_in_optical_only(self):
        for mode in MODES:
            self.settings.audio_mode = mode
            offered = "audio_optical_encode_ac3" in self._keys()
            self.assertEqual(offered, mode == "optical",
                             "encode toggle visibility wrong in %s" % mode)

    def test_mode_only_keys_do_not_leak_into_advanced(self):
        for mode in MODES:
            if mode == "optical":
                continue
            self.settings.audio_mode = mode
            self.assertNotIn("audio_optical_encode_ac3", self._keys())

    def test_mode_and_night_mode_are_always_offered(self):
        for mode in MODES:
            self.settings.audio_mode = mode
            keys = self._keys()
            self.assertIn("audio_mode", keys)
            self.assertIn("audio_night_mode", keys)

    def test_retired_audio_output_key_is_gone(self):
        from jellyfin_mpv_shim.conf import Settings

        self.assertNotIn("audio_output", Settings.__annotations__)


if __name__ == "__main__":
    unittest.main()
