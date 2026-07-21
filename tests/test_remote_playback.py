"""Playback behavior for remote sources (.strm shortcuts and live streams).

Covers the three places a remote source diverges from a local file: where the
runtime actually lives, how a live stream is released, and what happens when
the origin stops delivering without ever signalling end-of-file.
"""

import sys
import time
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing player reaches args.get_args()

from jellyfin_mpv_shim.media import Video  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


def make_video(item=None, media_source=None, is_transcode=False,
               playback_info=None):
    """Build a Video without __init__ (which would hit the network)."""
    video = Video.__new__(Video)
    video.item = item if item is not None else {}
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


class TerminateTranscodeTest(unittest.TestCase):
    def test_closes_live_stream_even_when_direct_streaming(self):
        # The regression this guards: a live source that direct-streams (the
        # usual HDHomeRun path) is not a transcode, so an is_transcode gate
        # skipped the close entirely and leaked the tuner.
        video = make_video(media_source={"LiveStreamId": "live-1"},
                           is_transcode=False)
        video.terminate_transcode()
        video.client.jellyfin._post.assert_called_once_with(
            "LiveStreams/Close", params={"liveStreamId": "live-1"}
        )

    def test_close_sends_id_as_query_param_not_body(self):
        # The server binds liveStreamId with [FromQuery, Required]; a JSON body
        # fails model validation and the tuner is never released.
        video = make_video(media_source={"LiveStreamId": "live-2"},
                           is_transcode=True)
        video.terminate_transcode()
        _args, kwargs = video.client.jellyfin._post.call_args
        self.assertEqual(kwargs.get("params"), {"liveStreamId": "live-2"})
        self.assertIsNone(kwargs.get("json"))

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
        video.client.jellyfin._post.side_effect = RuntimeError("boom")
        video.client.config.data = {"app.device_id": "dev"}
        video.terminate_transcode()
        video.client.jellyfin.close_transcode.assert_called_once_with("dev", "sess")

    def test_plain_direct_play_closes_nothing(self):
        video = make_video(media_source={}, is_transcode=False)
        video.terminate_transcode()
        video.client.jellyfin._post.assert_not_called()
        video.client.jellyfin.close_transcode.assert_not_called()

    def test_transcode_without_live_stream_still_closes_transcode(self):
        video = make_video(media_source={}, is_transcode=True,
                           playback_info={"PlaySessionId": "sess"})
        video.client.config.data = {"app.device_id": "dev"}
        video.terminate_transcode()
        video.client.jellyfin.close_transcode.assert_called_once_with("dev", "sess")


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
