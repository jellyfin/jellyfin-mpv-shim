"""Playback behavior for remote sources (.strm shortcuts and live streams).

Covers where the runtime for a remote source actually lives, and what happens
when its origin stops delivering without ever signalling end-of-file.
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
