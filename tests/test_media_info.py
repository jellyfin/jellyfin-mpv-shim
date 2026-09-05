"""The shared media-info formatter.

These are DTO-shape tests, which is the whole reason
``components/media_info.py`` is pure: the interesting cases here are files,
not screens, and a screen test would need a server to reach any of them.

The cases worth pinning are the ones where the *absence* of a field, or a
field only some files carry, changes the output — because those are the ones
that pass on whatever the author happened to test against.
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

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.components import media_info  # noqa: E402


def _attrs(stream, source=None):
    return dict(media_info.stream_attributes(stream, source))


class StreamOrderTest(unittest.TestCase):
    """jellyfin-web's ``itemHelper.sortTracks``, which is worth copying
    exactly: the order *is* information — it is the order a player considers
    them in — and disagreeing about it makes an external subtitle look like
    a missing one."""

    def test_embedded_before_external(self):
        streams = [{"Type": "Subtitle", "Index": 1, "IsExternal": True},
                   {"Type": "Subtitle", "Index": 9, "IsExternal": False}]
        order = media_info.visible_streams({"MediaStreams": streams})
        self.assertEqual([s["Index"] for s in order], [9, 1])

    def test_forced_and_default_outrank_index(self):
        streams = [{"Type": "Subtitle", "Index": 1},
                   {"Type": "Subtitle", "Index": 2, "IsDefault": True},
                   {"Type": "Subtitle", "Index": 3, "IsForced": True}]
        order = media_info.visible_streams({"MediaStreams": streams})
        self.assertEqual([s["Index"] for s in order], [3, 2, 1])

    def test_index_breaks_ties(self):
        streams = [{"Type": "Audio", "Index": 5}, {"Type": "Audio", "Index": 2}]
        order = media_info.visible_streams({"MediaStreams": streams})
        self.assertEqual([s["Index"] for s in order], [2, 5])

    def test_a_stream_with_no_index_sorts_last_rather_than_crashing(self):
        # A DTO field that is merely usually present. Sorting None against an
        # int is a TypeError in Python where JS would coerce, so this is a
        # real crash and not a tidiness point.
        streams = [{"Type": "Audio"}, {"Type": "Audio", "Index": 3}]
        order = media_info.visible_streams({"MediaStreams": streams})
        self.assertEqual([s.get("Index") for s in order], [3, None])

    def test_data_streams_are_dropped(self):
        streams = [{"Type": "Data", "Index": 1}, {"Type": "Video", "Index": 2}]
        order = media_info.visible_streams({"MediaStreams": streams})
        self.assertEqual([s["Type"] for s in order], ["Video"])

    def test_no_streams_at_all(self):
        self.assertEqual(media_info.visible_streams(None), [])
        self.assertEqual(media_info.visible_streams({}), [])


class SourceAttributeTest(unittest.TestCase):

    def test_the_path_is_shown(self):
        """Deliberate divergence from jellyfin-web, which gates the path on
        IsAdministrator. See docs/UI_FIXES_4.md §11 — it is not a boundary,
        and hiding it from the owner of the machine buys nothing."""
        rows = dict(media_info.source_attributes({"Path": "/media/a.mkv"}))
        self.assertIn("/media/a.mkv", rows.values())

    def test_absent_fields_produce_no_rows(self):
        self.assertEqual(media_info.source_attributes({}), [])
        self.assertEqual(media_info.source_attributes(None), [])

    def test_zero_size_is_not_reported_as_a_size(self):
        # The server sends 0 for a source it could not measure. "0 B" reads
        # as a fact about the file rather than as the absence of one.
        rows = dict(media_info.source_attributes({"Size": 0,
                                                  "Container": "mkv"}))
        self.assertNotIn("Size", rows)


class StreamAttributeTest(unittest.TestCase):

    def test_a_stream_with_nothing_on_it_yields_nothing(self):
        self.assertEqual(media_info.stream_attributes({"Type": "Audio"}), [])

    def test_false_booleans_are_omitted(self):
        """Web prints "External: No". Five such rows per stream, on a file
        with eight subtitle tracks, is forty rows saying nothing — and the
        HUD has no scrollbar to hide them behind."""
        rows = _attrs({"Type": "Subtitle", "Index": 1, "Codec": "subrip",
                       "IsDefault": False, "IsForced": False,
                       "IsExternal": False})
        self.assertEqual(list(rows), ["Codec"])

    def test_true_booleans_are_shown(self):
        rows = _attrs({"Type": "Subtitle", "Index": 1, "IsExternal": True})
        self.assertEqual(rows.get("External"), "Yes")

    def test_is_avc_false_is_kept_because_it_is_not_a_flag(self):
        # Tri-state: absent means "not asked", False distinguishes Annex-B
        # from AVC and is the answer to a real question. Unlike the flags
        # above, its absence would not say it.
        self.assertEqual(_attrs({"Type": "Video", "IsAVC": False})["AVC"],
                         "No")
        self.assertNotIn("AVC", _attrs({"Type": "Video"}))

    def test_video_only_attributes_stay_off_audio(self):
        audio = _attrs({"Type": "Audio", "VideoRange": "HDR",
                        "Rotation": 90, "AspectRatio": "16:9",
                        "ReferenceFrameRate": 24})
        for label in ("Video range", "Rotation", "Aspect ratio", "Framerate"):
            self.assertNotIn(label, audio)

    def test_language_is_not_repeated_on_video(self):
        # It is already inside the server's DisplayTitle there, and reading
        # it twice on one block looks like a rendering fault.
        self.assertNotIn("Language", _attrs({"Type": "Video",
                                             "Language": "eng"}))
        self.assertIn("Language", _attrs({"Type": "Audio",
                                          "Language": "eng"}))

    def test_units(self):
        rows = _attrs({"Type": "Audio", "BitRate": 4_500_000,
                       "SampleRate": 48000, "BitDepth": 24, "Channels": 8})
        self.assertEqual(rows["Bitrate"], "4500 kbps")
        self.assertEqual(rows["Sample rate"], "48000 Hz")
        self.assertEqual(rows["Bit depth"], "24 bit")
        self.assertEqual(rows["Channels"], "8 ch")

    def test_a_half_known_resolution_still_reports(self):
        rows = _attrs({"Type": "Video", "Height": 1080})
        self.assertEqual(rows["Resolution"], "?x1080")

    def test_dolby_vision_block_only_on_a_dolby_vision_stream(self):
        plain = _attrs({"Type": "Video", "DvProfile": 7})
        self.assertNotIn("DV profile", plain)
        dovi = _attrs({"Type": "Video", "VideoDoViTitle": "DV Profile 7",
                       "DvProfile": 7, "DvLevel": 6, "BlPresentFlag": 0})
        self.assertEqual(dovi["DV profile"], "7")
        # 0 is a real flag value, not an absent one.
        #
        # "DV bl preset flag" is jellyfin-web's own spelling, typo and all
        # (MediaInfoBlPresentFlag). Ours has to be character-identical or
        # seed_from_jellyfin_web.py cannot match it -- see the note on
        # _REASONS. Tidying it here would cost the translation, not gain a
        # better label.
        self.assertEqual(dovi["DV bl preset flag"], "0")

    def test_timestamp_comes_from_the_source_and_only_for_video(self):
        source = {"Timestamp": "Zero"}
        self.assertEqual(_attrs({"Type": "Video"}, source)["Timestamp"],
                         "Zero")
        self.assertNotIn("Timestamp", _attrs({"Type": "Audio"}, source))
        self.assertNotIn("Timestamp", _attrs({"Type": "Video"}, {}))


class SummaryTest(unittest.TestCase):

    def test_codec_survives_when_the_server_gives_no_display_title(self):
        # "1080p" alone drops the one thing that decides whether a file will
        # direct-play, which is what this line exists to answer.
        parts = media_info.summary_parts({
            "MediaStreams": [{"Type": "Video", "Index": 0, "Codec": "hevc",
                              "Width": 1920, "Height": 1080}]})
        self.assertIn("HEVC 1920x1080", parts)

    def test_the_servers_display_title_wins_when_there_is_one(self):
        parts = media_info.summary_parts({
            "MediaStreams": [{"Type": "Video", "Index": 0, "Codec": "hevc",
                              "DisplayTitle": "4K HEVC"}]})
        self.assertIn("4K HEVC", parts)

    def test_sdr_is_not_worth_a_chip(self):
        self.assertEqual(media_info.video_range({"VideoRangeType": "SDR"}), "")
        self.assertEqual(media_info.video_range({"VideoRange": "HDR",
                                                 "VideoRangeType": "HDR10"}),
                         "HDR10")

    def test_the_primary_stream_is_the_default_one_not_the_first_listed(self):
        source = {"MediaStreams": [
            {"Type": "Audio", "Index": 1, "Codec": "aac"},
            {"Type": "Audio", "Index": 2, "Codec": "truehd",
             "IsDefault": True}]}
        self.assertEqual(
            media_info.primary_stream(source, "Audio")["Codec"], "truehd")

    def test_an_empty_source_summarises_to_nothing(self):
        self.assertEqual(media_info.summary_parts({}), [])
        self.assertEqual(media_info.summary_parts(None), [])


class PlayMethodTest(unittest.TestCase):

    def test_every_method_has_a_word(self):
        for method in (media_info.DIRECT_PLAY, media_info.DIRECT_STREAM,
                       media_info.REMUX, media_info.TRANSCODE):
            self.assertTrue(media_info.play_method_label(method))

    def test_an_unknown_method_is_blank_rather_than_the_enum_name(self):
        self.assertEqual(media_info.play_method_label("Teleportation"), "")

    def test_reasons_become_sentences(self):
        # No trailing full stop, and the phrasing is not ours to tidy:
        # seed_from_jellyfin_web.py matches our msgid against
        # jellyfin-web's *value*, so any edit here is the difference
        # between 25 strings arriving translated in 86 locales and 25
        # nobody ever translates.
        out = media_info.transcode_reasons(["VideoCodecNotSupported"])
        self.assertEqual(out, ["The video codec is not supported"])

    def test_reasons_arrive_as_a_flags_string_on_some_endpoints(self):
        out = media_info.transcode_reasons(
            "VideoCodecNotSupported, AudioIsExternal")
        self.assertEqual(len(out), 2)
        self.assertNotIn("VideoCodecNotSupported", out)

    def test_an_unknown_reason_passes_through_rather_than_vanishing(self):
        # The enum grows with the server. A name we have no sentence for is
        # still the answer to "why is my server pinned at 100%"; dropping it
        # leaves the screen saying it is transcoding for no reason.
        self.assertEqual(media_info.transcode_reasons(["BrandNewReason"]),
                         ["BrandNewReason"])

    def test_no_reasons(self):
        self.assertEqual(media_info.transcode_reasons(None), [])
        self.assertEqual(media_info.transcode_reasons(""), [])


if __name__ == "__main__":
    unittest.main()
