"""How the stream reached us — the fact behind the playback-info screen.

**Every fixture here is a real measurement**, taken against a live Jellyfin
10.11 server by asking PlaybackInfo for one item with three device profiles
built to force each outcome. That matters more than usual, because two of
the things this code does are the opposite of what the obvious reading of
the API suggests, and both fail silently:

* ``TranscodeReasons`` is **not a MediaSource field**. It rides in the
  transcoding URL's query string as a comma-joined flags string. Reading it
  off the DTO — which is what the schema suggests and what an older server
  seems to promise — yields None every time, and the screen then says the
  file is being transcoded for no reason at all.
* a remuxing URL does **not** say ``VideoCodec=copy``. It names the target
  codec, which for a remux is simply the codec the file already has. So the
  test is a comparison against the source stream, not a keyword.

The measured URLs, source ``mkv / hevc / aac``:

===============================  ===============  ===============  ===========
profile                          VideoCodec       AudioCodec       reasons
===============================  ===============  ===============  ===========
container refused, codecs kept   hevc             aac              Container…
container refused, audio changed hevc             opus             Container…
video codec refused              h264             aac              VideoCodec…
===============================  ===============  ===============  ===========
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import media  # noqa: E402

SOURCE = {"Container": "mkv", "MediaStreams": [
    {"Type": "Video", "Index": 0, "Codec": "hevc"},
    {"Type": "Audio", "Index": 1, "Codec": "aac"},
    {"Type": "Audio", "Index": 2, "Codec": "truehd"},
]}

BASE = ("/videos/6519d549/stream.mkv?&DeviceId=probe"
        "&MediaSourceId=6519d549&VideoCodec=%s&AudioCodec=%s"
        "&AudioStreamIndex=1&VideoBitrate=199808000&AudioBitrate=192000"
        "&PlaySessionId=735f27d3&RequireAvc=false&Tag=d830593"
        "&hevc-level=150&TranscodeReasons=%s")


def url(video, audio, reasons="ContainerNotSupported"):
    return BASE % (video, audio, reasons)


class MeasuredCasesTest(unittest.TestCase):
    """The three real server answers, verbatim."""

    def test_both_codecs_kept_is_a_remux(self):
        method, reasons = media.transcode_play_method(
            SOURCE, url("hevc", "aac"))
        self.assertEqual(method, media.PLAY_REMUX)
        self.assertEqual(reasons, ["ContainerNotSupported"])

    def test_video_kept_audio_re_encoded_is_direct_stream(self):
        method, _r = media.transcode_play_method(SOURCE, url("hevc", "opus"))
        self.assertEqual(method, media.PLAY_DIRECT_STREAM)

    def test_video_re_encoded_is_a_transcode(self):
        method, reasons = media.transcode_play_method(
            SOURCE, url("h264", "aac", "VideoCodecNotSupported"))
        self.assertEqual(method, media.PLAY_TRANSCODE)
        self.assertEqual(reasons, ["VideoCodecNotSupported"])

    def test_the_reasons_come_out_of_the_url_not_the_dto(self):
        # The regression this file exists for: a DTO carrying its own
        # TranscodeReasons must not be preferred, because a real one has
        # none and the URL is the only place they are.
        source = dict(SOURCE, TranscodeReasons=["SomethingFromTheDto"])
        _m, reasons = media.transcode_play_method(
            source, url("h264", "aac", "VideoCodecNotSupported"))
        self.assertEqual(reasons, ["VideoCodecNotSupported"])


class CodecMatchingTest(unittest.TestCase):

    def test_a_target_listing_several_codecs_counts_as_direct(self):
        # A device profile may offer a list and let the server pick; the
        # server then emits the list. Equality would call a remux a
        # transcode for every profile written that way.
        method, _r = media.transcode_play_method(
            SOURCE, url("hevc%2Ch264", "aac"))
        self.assertEqual(method, media.PLAY_REMUX)

    def test_codec_comparison_ignores_case(self):
        method, _r = media.transcode_play_method(SOURCE, url("HEVC", "AAC"))
        self.assertEqual(method, media.PLAY_REMUX)

    def test_the_selected_audio_track_is_the_one_compared(self):
        # Index 2 is truehd; a url that emits aac is re-encoding *that*
        # track even though the file's first audio stream is already aac.
        method, _r = media.transcode_play_method(SOURCE, url("hevc", "aac"),
                                                 aid=2)
        self.assertEqual(method, media.PLAY_DIRECT_STREAM)

    def test_without_a_chosen_track_the_first_audio_stream_is_used(self):
        method, _r = media.transcode_play_method(SOURCE, url("hevc", "aac"))
        self.assertEqual(method, media.PLAY_REMUX)

    def test_an_unknown_audio_index_falls_back_rather_than_crashing(self):
        method, _r = media.transcode_play_method(SOURCE, url("hevc", "aac"),
                                                 aid=99)
        self.assertEqual(method, media.PLAY_REMUX)


class DegenerateSourceTest(unittest.TestCase):

    def test_a_file_with_no_audio_is_not_reported_as_re_encoding_it(self):
        silent = {"MediaStreams": [{"Type": "Video", "Index": 0,
                                    "Codec": "hevc"}]}
        method, _r = media.transcode_play_method(silent, url("hevc", "aac"))
        self.assertEqual(method, media.PLAY_REMUX)

    def test_a_target_naming_no_codec_means_untouched(self):
        method, _r = media.transcode_play_method(SOURCE, url("", ""))
        self.assertEqual(method, media.PLAY_REMUX)

    def test_no_url_at_all(self):
        method, reasons = media.transcode_play_method(SOURCE, None)
        self.assertEqual(method, media.PLAY_REMUX)
        self.assertEqual(reasons, [])

    def test_no_streams_at_all(self):
        method, _r = media.transcode_play_method({}, url("h264", "aac"))
        # Nothing to compare against is not evidence of re-encoding.
        self.assertEqual(method, media.PLAY_REMUX)

    def test_several_reasons_arrive_comma_joined(self):
        _m, reasons = media.transcode_play_method(
            SOURCE, url("h264", "opus",
                        "VideoCodecNotSupported%2CAudioCodecNotSupported"))
        self.assertEqual(reasons, ["VideoCodecNotSupported",
                                   "AudioCodecNotSupported"])


class OfflineVideoTest(unittest.TestCase):
    """A downloaded copy is the case no other Jellyfin client has."""

    def _offline_video(self):
        import json
        from unittest import mock
        from jellyfin_mpv_shim.sync import offline_media

        row = {"file_path": "x/y.mkv", "server_uuid": "s",
               "item_json": json.dumps({"Type": "Episode", "Name": "Ep"}),
               "source_json": json.dumps(
                   {"Id": "src", "MediaStreams": [], "Container": "mkv"})}
        parent = mock.Mock(client=None)
        with mock.patch.object(offline_media, "syncManager") as sm:
            sm.db.get.return_value = row
            sm.root = "/downloads"
            return offline_media.OfflineVideo("item", parent)

    def test_it_says_direct_play_before_any_url_is_asked_for(self):
        """Set in __init__ rather than only in get_playback_url: the
        playback-info screen can be asked before a url has been requested,
        and unlike the online Video there is no decision pending here — it is
        a local file whatever anyone's device profile says."""
        video = self._offline_video()
        self.assertEqual(video.play_method, media.PLAY_DIRECT)
        self.assertTrue(video.direct_path)
        self.assertEqual(video.transcode_reasons, [])

    def test_it_is_the_same_constant_the_online_path_uses(self):
        # Two spellings of "direct play" would be two things the screen has
        # to know about, and the second one is always the one nobody mapped.
        from jellyfin_mpv_shim.sync import offline_media
        self.assertIs(offline_media.PLAY_DIRECT, media.PLAY_DIRECT)


if __name__ == "__main__":
    unittest.main()


class SameCodecReEncodeTest(unittest.TestCase):
    """A re-encode that keeps the codec is not a remux.

    The url names the *target* codec, and for a bitrate- or level-limited
    transcode that is the codec the file already has — so the codec
    comparison alone calls it a copy. Measured against a live server with a
    100 kbps ceiling on an h264 file: target `h264`, reasons
    `AudioCodecNotSupported,ContainerBitrateExceedsLimit`.
    """

    def test_a_bitrate_ceiling_is_a_transcode(self):
        method, _r = media.transcode_play_method(
            SOURCE, url("hevc", "aac", "ContainerBitrateExceedsLimit"))
        self.assertEqual(method, media.PLAY_TRANSCODE)

    def test_the_measured_case(self):
        source = {"MediaStreams": [
            {"Type": "Video", "Index": 0, "Codec": "h264"},
            {"Type": "Audio", "Index": 1, "Codec": "aac"}]}
        method, _r = media.transcode_play_method(
            source,
            url("h264", "aac",
                "AudioCodecNotSupported%2CContainerBitrateExceedsLimit"))
        self.assertEqual(method, media.PLAY_TRANSCODE)

    def test_a_video_level_limit_is_a_transcode(self):
        method, _r = media.transcode_play_method(
            SOURCE, url("hevc", "aac", "VideoLevelNotSupported"))
        self.assertEqual(method, media.PLAY_TRANSCODE)

    def test_an_audio_only_reason_still_leaves_the_video_direct(self):
        """The DirectStream case: video passed through, audio re-encoded."""
        method, _r = media.transcode_play_method(
            SOURCE, url("hevc", "aac", "AudioChannelsNotSupported"))
        self.assertEqual(method, media.PLAY_DIRECT_STREAM)

    def test_a_codec_reason_does_not_outrank_the_target_codec(self):
        """`TranscodeReasons` says why *direct play* was refused, not what
        the transcoder does with each stream — so AudioCodecNotSupported
        can sit on a session whose profile then copies the audio anyway.
        Treating it as authoritative reported a remux as a DirectStream,
        which the live-server e2e test caught."""
        method, _r = media.transcode_play_method(
            SOURCE, url("hevc", "aac",
                        "ContainerNotSupported%2CAudioCodecNotSupported"))
        self.assertEqual(method, media.PLAY_REMUX)

    def test_a_container_only_reason_is_still_a_remux(self):
        method, _r = media.transcode_play_method(
            SOURCE, url("hevc", "aac", "ContainerNotSupported"))
        self.assertEqual(method, media.PLAY_REMUX)
