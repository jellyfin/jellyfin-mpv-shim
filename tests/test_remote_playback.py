"""Playback behavior for remote sources (.strm shortcuts and live streams).

Covers the three places a remote source diverges from a local file: where the
runtime actually lives, how a live stream is released, and what happens when
the origin stops delivering without ever signalling end-of-file.
"""

import sys
import threading
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing player reaches args.get_args()

from jellyfin_mpv_shim.media import Video  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


def make_video(item=None, media_source=None, is_transcode=False,
               playback_info=None, item_id="item-1"):
    """Build a Video without __init__ (which would hit the network).

    `item_id` matters to `get_duration`: a media source standing for the
    item's own file carries the item's id, and that is what decides whether
    the Item's runtime describes the source being played.
    """
    video = Video.__new__(Video)
    video.item = item if item is not None else {}
    video.item_id = item_id
    video.media_source = media_source
    video.is_transcode = is_transcode
    video.playback_info = playback_info
    video.client = mock.MagicMock()
    return video


class GetDurationTest(unittest.TestCase):
    def test_prefers_media_source_over_item(self):
        # The .strm case: a library scan never probes a shortcut, so the Item
        # has no runtime; the server's playback-time probe puts it on the
        # MediaSource instead.
        video = make_video(item={}, media_source={"RunTimeTicks": 60 * 10000000})
        self.assertEqual(video.get_duration(), 60)

    def test_falls_back_to_item_when_source_has_none(self):
        video = make_video(item={"RunTimeTicks": 42 * 10000000},
                           media_source={})
        self.assertEqual(video.get_duration(), 42)

    def test_falls_back_to_item_when_source_is_none(self):
        # media_source is None until get_playback_url runs.
        video = make_video(item={"RunTimeTicks": 42 * 10000000},
                           media_source=None)
        self.assertEqual(video.get_duration(), 42)

    def test_none_when_neither_has_a_runtime(self):
        # Live TV, and a .strm whose remote probe failed.
        video = make_video(item={}, media_source={})
        self.assertIsNone(video.get_duration())

    def test_zero_ticks_is_not_a_duration(self):
        video = make_video(item={"RunTimeTicks": 0}, media_source={"RunTimeTicks": 0})
        self.assertIsNone(video.get_duration())

    def test_an_alternate_version_never_borrows_the_item_s_runtime(self):
        """A .strm grouped as a version beside real media.

        Measured against 12.0: the server probes only the source it is about
        to play, and declines to probe an alternate even when PlaybackInfo
        names it — so the remote version arrives with no runtime while the
        Item still carries the local file's. Taking that number gave a
        ten-minute stream the local version's twelve seconds, which called
        the file finished twelve seconds in and wiped the resume position.
        """
        video = make_video(item={"RunTimeTicks": 12 * 10000000},
                           media_source={"Id": "other-version"},
                           item_id="item-1")
        self.assertIsNone(video.get_duration())

    def test_the_item_s_own_source_still_uses_the_item_s_runtime(self):
        # The source standing for the item's own file carries the item's id,
        # so the fallback that .strm needs is untouched for everything else.
        video = make_video(item={"RunTimeTicks": 42 * 10000000},
                           media_source={"Id": "item-1"}, item_id="item-1")
        self.assertEqual(video.get_duration(), 42)

    def test_an_unidentified_source_keeps_the_fallback(self):
        # Withheld only where the source is *known* to be an alternate. A
        # source that names no id says nothing either way, and guessing
        # "alternate" there would drop the duration for callers that never
        # had a version set at all.
        video = make_video(item={"RunTimeTicks": 42 * 10000000},
                           media_source={}, item_id="item-1")
        self.assertEqual(video.get_duration(), 42)


class MapStreamsTest(unittest.TestCase):
    """The stream maps are built for a remote source too.

    They used to be built only for `Protocol=File`, which was written before
    stream files existed and reads as "this is not a real file, so it has no
    real tracks". A `.strm` that direct plays is exactly that: a real
    container, with the tracks the server probed off it, reaching mpv either
    from the origin or proxied through the server. Both maps came back empty
    while the browser and the server were still handing out its actual stream
    indices, and selecting one was a KeyError rather than a wrong track.
    """

    #: What a probed `.strm` comes back as: h264 + aac over HTTP.
    REMOTE = {
        "Protocol": "Http",
        "MediaStreams": [
            {"Index": 0, "Type": "Video", "Codec": "h264"},
            {"Index": 1, "Type": "Audio", "Codec": "aac"},
        ],
    }

    @staticmethod
    def mapped(source, explicit_tracks=True):
        """`make_video` plus the track state map_streams reads and writes.

        `explicit_tracks` defaults to True so the mapping is measured on its
        own; the defaulting half has its own test.
        """
        video = make_video(media_source=source)
        video.aid = video.sid = None
        video.explicit_tracks = explicit_tracks
        video.map_streams()
        return video

    def test_a_remote_source_still_maps_its_audio(self):
        video = self.mapped(dict(self.REMOTE))
        # Index 1 in Jellyfin's numbering (0 is the video) is mpv's aid 1.
        self.assertEqual(video.audio_seq, {1: 1})
        self.assertEqual(video.audio_uid, {1: 1})

    def test_a_remote_source_maps_its_subtitles(self):
        source = dict(self.REMOTE)
        source["MediaStreams"] = self.REMOTE["MediaStreams"] + [
            {"Index": 2, "Type": "Subtitle", "DeliveryMethod": "Embed"},
        ]
        self.assertEqual(self.mapped(source).subtitle_seq, {2: 1})

    def test_a_local_file_is_unchanged(self):
        video = self.mapped(dict(self.REMOTE, Protocol="File"))
        self.assertEqual(video.audio_seq, {1: 1})

    def test_a_remote_source_takes_the_server_s_default_track(self):
        """The other half of the early return, and the reason the crash was
        reachable at all.

        `map_streams` also applies `PlaybackInfo`'s `DefaultAudioStreamIndex`,
        so returning early skipped *that* too: played from anywhere that sends
        no track (the search results' play button), a remote source ended up
        with `aid=None` and whatever mpv opened with. The browser's detail page
        resolves the same default itself, for its pickers, and sends it -- so
        the two disagreed, and only the explicit one reached the lookup that
        crashed.
        """
        source = dict(self.REMOTE, DefaultAudioStreamIndex=1)
        video = self.mapped(source, explicit_tracks=False)
        self.assertEqual(video.aid, 1)
        self.assertEqual(video.audio_seq[video.aid], 1)

    def test_language_config_outranks_the_server_default(self):
        """...and a rule beats it, on a remote source like any other.

        The early return skipped the language_config lookup as well, so the
        one setting whose entire job is to override the server's choice was
        silently inert for every stream file and live stream.
        """
        from jellyfin_mpv_shim.conf import settings
        from jellyfin_mpv_shim.language_config import parse_language_config

        source = dict(self.REMOTE, DefaultAudioStreamIndex=1)
        source["MediaStreams"] = [
            {"Index": 0, "Type": "Video", "Codec": "h264"},
            {"Index": 1, "Type": "Audio", "Codec": "aac", "Language": "eng"},
            {"Index": 2, "Type": "Audio", "Codec": "aac", "Language": "jpn"},
        ]
        rules = parse_language_config([{"alang": "jpn"}])
        with mock.patch.object(settings, "language_config", rules):
            video = self.mapped(source, explicit_tracks=False)
        self.assertEqual(video.aid, 2,
                         "the server's default won over an explicit rule")
        self.assertEqual(video.audio_seq[video.aid], 2)

    def test_a_silent_source_selects_no_audio(self):
        """A film with no audio at all -- silent, or a video-only render.

        Nothing may invent a track for it: the map stays empty and `aid` stays
        None, which is what makes the player leave mpv's (absent) audio track
        alone rather than asking for one.
        """
        source = {"Protocol": "Http", "MediaStreams": [
            {"Index": 0, "Type": "Video", "Codec": "h264"},
        ]}
        video = self.mapped(source, explicit_tracks=False)
        self.assertEqual(video.audio_seq, {})
        self.assertIsNone(video.aid)

    def test_a_source_with_no_streams_maps_nothing(self):
        # An unprobed remote (an RTSP channel, a refused shortcut) reports no
        # streams and may not report a Protocol either. Empty maps, no raise.
        video = self.mapped({})
        self.assertEqual(video.audio_seq, {})
        self.assertEqual(video.subtitle_seq, {})

    def test_no_source_maps_nothing(self):
        self.assertEqual(self.mapped(None).audio_seq, {})


class ConfigureStreamsTest(unittest.TestCase):
    """Selecting a track mpv cannot be told about must not abort the start.

    `configure_streams` runs from the middle of `_play_media`, so anything it
    raises leaves playback half-started. An aid that is not in the map is a
    normal state of the world -- carried over from the previous item, or from
    a version that has since been swapped -- and mpv's own default track is a
    fine answer.
    """

    def setUp(self):
        self.player = PlayerManager.__new__(PlayerManager)
        self.player._lock = threading.RLock()
        self.player._player = mock.MagicMock()

    def configure(self, video):
        self.player._video = video
        self.player.configure_streams()

    def test_selects_the_mapped_track(self):
        video = make_video(media_source={})
        video.aid, video.sid = 1, None
        video.audio_seq = {1: 1}
        video.subtitle_seq = {}
        video.subtitle_url = {}
        self.configure(video)
        self.assertEqual(self.player._player.audio, 1)

    def test_an_unmapped_audio_index_leaves_mpv_alone(self):
        video = make_video(media_source={})
        video.aid, video.sid = 1, None
        video.audio_seq = {}          # unprobed source, or a stale index
        video.subtitle_seq = {}
        video.subtitle_url = {}
        self.configure(video)
        # Never assigned: whatever mpv opened with stays selected.
        self.assertNotIn("audio", self.player._player.__dict__)

    def test_a_transcode_never_applies_the_map(self):
        """This, not the media source's protocol, is what keeps a transcode
        right.

        `map_streams` builds its map from the *source's* streams, and a
        transcode is a single re-encoded track: mpv's ids are the
        transcoder's, so applying the map would ask for a track number that
        does not exist. The track is chosen server-side instead -- the index
        goes out with `PlaybackInfo` and comes back in the `TranscodingUrl`.
        (For a local file this map has always been built, protocol check or
        no, so this gate is the only thing that has ever stood here.)
        """
        video = make_video(media_source={}, is_transcode=True)
        video.aid, video.sid = 6, None
        video.audio_seq = {1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 6: 6}
        video.subtitle_seq = {}
        video.subtitle_url = {}
        self.configure(video)
        self.assertNotIn("audio", self.player._player.__dict__)


class TerminateTranscodeTest(unittest.TestCase):
    def test_closes_live_stream_even_when_direct_streaming(self):
        # The regression this guards: a live source that direct-streams (the
        # usual HDHomeRun path) is not a transcode, so an is_transcode gate
        # skipped the close entirely and leaked the tuner.
        video = make_video(media_source={"LiveStreamId": "live-1"},
                           is_transcode=False)
        video.terminate_transcode()
        video.client.jellyfin.close_live_stream.assert_called_once_with("live-1")

    def test_closing_live_stream_skips_the_transcode_call(self):
        # Closing the live stream tears down its transcode as a side effect.
        video = make_video(media_source={"LiveStreamId": "live-3"},
                           is_transcode=True,
                           playback_info={"PlaySessionId": "sess"})
        video.terminate_transcode()
        video.client.jellyfin.close_transcode.assert_not_called()

    def test_falls_back_to_transcode_close_when_live_close_fails(self):
        video = make_video(media_source={"LiveStreamId": "live-4"},
                           is_transcode=True,
                           playback_info={"PlaySessionId": "sess"})
        video.client.jellyfin.close_live_stream.side_effect = RuntimeError("boom")
        video.client.config.data = {"app.device_id": "dev"}
        video.terminate_transcode()
        video.client.jellyfin.close_transcode.assert_called_once_with("dev", "sess")

    def test_plain_direct_play_closes_nothing(self):
        video = make_video(media_source={}, is_transcode=False)
        video.terminate_transcode()
        video.client.jellyfin.close_live_stream.assert_not_called()
        video.client.jellyfin.close_transcode.assert_not_called()

    def test_transcode_without_live_stream_still_closes_transcode(self):
        video = make_video(media_source={}, is_transcode=True,
                           playback_info={"PlaySessionId": "sess"})
        video.client.config.data = {"app.device_id": "dev"}
        video.terminate_transcode()
        video.client.jellyfin.close_transcode.assert_called_once_with("dev", "sess")


class BestMediaSourceTest(unittest.TestCase):
    """Source selection must never come back empty-handed.

    Callers dereference the result immediately (get_playback_url does
    media_source["Id"]), and the retry loop only covers a playback_info
    carrying more than one source — which a live channel never has.
    """

    @staticmethod
    def video_with(sources):
        video = make_video()
        video.playback_info = {"MediaSources": sources}
        return video

    def test_live_source_with_no_bitrate_is_still_selected(self):
        # A live TV source reports neither SupportsDirectPlay nor a Bitrate,
        # so it weighs 0 and could never beat the starting weight of 0.
        live = {"Id": "live", "SupportsDirectStream": True}
        video = self.video_with([live])
        self.assertIs(video.get_best_media_source(), live)

    def test_prefers_direct_play_over_higher_bitrate(self):
        direct = {"Id": "a", "SupportsDirectPlay": True, "Bitrate": 1000}
        fat = {"Id": "b", "SupportsDirectPlay": False, "Bitrate": 40000000}
        video = self.video_with([fat, direct])
        self.assertIs(video.get_best_media_source(), direct)

    def test_prefers_higher_bitrate_among_equals(self):
        low = {"Id": "a", "SupportsDirectPlay": True, "Bitrate": 1000}
        high = {"Id": "b", "SupportsDirectPlay": True, "Bitrate": 9000}
        video = self.video_with([low, high])
        self.assertIs(video.get_best_media_source(), high)

    def test_explicit_preference_wins(self):
        wanted = {"Id": "want", "Bitrate": 1}
        better = {"Id": "other", "SupportsDirectPlay": True, "Bitrate": 9000}
        video = self.video_with([better, wanted])
        self.assertIs(video.get_best_media_source("want"), wanted)

    def test_first_source_wins_when_all_weigh_nothing(self):
        first = {"Id": "a"}
        second = {"Id": "b"}
        video = self.video_with([first, second])
        self.assertIs(video.get_best_media_source(), first)


class StalledFinishTest(unittest.TestCase):
    """The watchdog for an end-of-file mpv never reports.

    A remote origin that stops delivering without closing the connection
    leaves the demuxer blocked in read: no end-file event, eof-reached False,
    playback-abort False. keep_open then holds the last frame indefinitely.
    """

    def setUp(self):
        self.player = PlayerManager.__new__(PlayerManager)
        self.player._player = mock.MagicMock()
        self.player._player.pause = False
        self.player._reached_eof = False
        self.player._last_playback_position = 0
        self.player._stall_position = None
        self.player._stall_since = 0.0

    def stall_at(self, position, video, elapsed):
        """Report `position` twice, `elapsed` seconds apart."""
        self.player._player.playback_time = position
        self.player._check_stalled_finish(video)          # first sighting
        self.player._stall_since = time.time() - elapsed  # age it
        return self.player._check_stalled_finish(video)

    def test_fires_when_stalled_at_the_end(self):
        video = make_video(item={"RunTimeTicks": 100 * 10000000}, media_source={})
        self.assertTrue(self.stall_at(99.0, video, elapsed=30))
        # Marked as a genuine finish so the item records as watched.
        self.assertTrue(self.player._reached_eof)

    def test_does_not_fire_before_the_threshold(self):
        video = make_video(item={"RunTimeTicks": 100 * 10000000}, media_source={})
        self.assertFalse(self.stall_at(99.0, video, elapsed=5))

    def test_does_not_fire_mid_file(self):
        # A stall in the middle is rebuffering on a slow origin. Advancing
        # would silently skip the rest of the episode.
        video = make_video(item={"RunTimeTicks": 100 * 10000000}, media_source={})
        self.assertFalse(self.stall_at(30.0, video, elapsed=300))

    def test_does_not_fire_without_a_known_duration(self):
        # Nothing to place the position against; guessing risks skipping.
        video = make_video(item={}, media_source={})
        self.assertFalse(self.stall_at(99.0, video, elapsed=300))

    def test_ignores_infinite_streams(self):
        # A live channel has no end to arrive at; a stall is an outage, and
        # "finishing" it would advance past a channel still being watched.
        video = make_video(item={"RunTimeTicks": 100 * 10000000},
                           media_source={"IsInfiniteStream": True})
        self.assertFalse(self.stall_at(99.0, video, elapsed=300))

    def test_ignores_paused_playback(self):
        video = make_video(item={"RunTimeTicks": 100 * 10000000}, media_source={})
        self.player._player.pause = True
        self.assertFalse(self.stall_at(99.0, video, elapsed=300))

    def test_progress_resets_the_stall_window(self):
        video = make_video(item={"RunTimeTicks": 100 * 10000000}, media_source={})
        self.player._player.playback_time = 98.0
        self.player._check_stalled_finish(video)
        self.player._stall_since = time.time() - 300
        # Position moved: playback is alive, so the window must restart.
        self.player._player.playback_time = 99.0
        self.assertFalse(self.player._check_stalled_finish(video))
        self.assertEqual(self.player._stall_position, 99.0)

    def test_unreadable_position_is_not_a_stall(self):
        video = make_video(item={"RunTimeTicks": 100 * 10000000}, media_source={})
        self.player._player.playback_time = None
        self.assertFalse(self.player._check_stalled_finish(video))


if __name__ == "__main__":
    unittest.main()
