"""Audiobook playback through the shim: a book plays, the position it
reports is the one the server stores, and playing to the end finishes it.
The resume rule itself, and why the long fixtures, is in test_audiobooks.

Split from test_audiobooks, which is contract tier: run there, this class
built a real player outside xvfb and put a window on the developer's
desktop. tests/test_e2e_registry.py keeps real players out of that tier.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402
from test_audiobooks import LIBRARY, LONG_M4B, TICKS  # noqa: E402


@_e2e.require_server_and_mpv
class AudiobookPlaybackTest(_e2e.E2ETestCase):
    """The reporting loop, with a real mpv and a real stream in it.

    Everything above drives the server directly. This is the half that
    proves the *shim* takes part: that an audiobook plays, that the position
    it reports is the one the server stores, and that playing to the end
    finishes the book. None of it can be seen from a fake — the fake agrees
    by construction — and none of it could be tested at all until the
    library grew a book longer than the server's resume window.
    """

    LIBRARY = LIBRARY

    def setUp(self):
        super().setUp()
        self.item = self.session.find(LONG_M4B, library=self.LIBRARY)
        self.session.reset_played(self.item["Id"])
        self.addCleanup(self.session.reset_played, self.item["Id"])

    def _play(self):
        media = _e2e.build_media(self.session, [self.item["Id"]])
        video = media.video
        self.assertIsNotNone(video, "Media built no video for an audiobook")
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(self.pm._player.duration,
                        "real mpv never reported a duration for the book")
        return video

    def test_an_audiobook_plays_at_all(self):
        video = self._play()
        self.assertFalse(video.is_transcode,
                         "an audiobook needed a transcode; the timings below "
                         "stop being meaningful if so")
        self.pm.send_timeline()
        playing = _e2e.wait_for(
            lambda: (self.session.my_session() or {}).get("NowPlayingItem"))
        self.assertTrue(playing, "the server never saw the book playing")
        self.assertEqual(playing["Id"], self.item["Id"])

    def test_mpv_knows_the_chapters(self):
        """The audio bar's chapter controls read mpv's list, not the item's:
        a `.m4b`'s markers live in the FILE, so mpv is the only thing that
        can enumerate them during playback."""
        self._play()
        chapters = self.pm._player.chapter_list or []
        self.assertGreaterEqual(
            len(chapters), 2,
            "mpv sees no chapters in a book the server says has six")

    def test_stopping_midway_leaves_a_resume_position(self):
        """Six minutes in: past `MinAudiobookResume`, and with eighteen
        left, well clear of the finish rule."""
        self._play()
        target = 6 * 60.0
        self.pm.seek(target, absolute=True, exact=True)
        arrived = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) >= target - 5,
            timeout=30)
        self.assertTrue(arrived, "mpv never reached the seek target")
        self.pm.send_timeline()
        self.pm.stop()

        ticks = _e2e.wait_for(
            lambda: self.session.user_data(self.item["Id"])
            .get("PlaybackPositionTicks"))
        self.assertTrue(ticks, "no resume position was recorded for the book")
        seconds = ticks / TICKS
        self.assertGreater(seconds, target - 60)
        self.assertLess(seconds, target + 60)
        self.assertFalse(self.session.user_data(self.item["Id"]).get("Played"),
                         "a book stopped a quarter of the way in was finished")

    def test_stopping_near_the_end_finishes_the_book(self):
        """The other side of the same rule, through the player: under five
        minutes remaining and the server marks it read and drops the
        position. This is the only way an audiobook gets finished by
        listening to it."""
        self._play()
        target = (self.item["RunTimeTicks"] / TICKS) - 90.0
        self.pm.seek(target, absolute=True, exact=True)
        arrived = self.pump_until(
            lambda: (self.pm._player.playback_time or 0) >= target - 5,
            timeout=30)
        self.assertTrue(arrived, "mpv never reached the seek target")
        self.pm.send_timeline()
        self.pm.stop()

        played = _e2e.wait_for(
            lambda: self.session.user_data(self.item["Id"]).get("Played"))
        self.assertTrue(played, "listening to the end did not finish the book")
        self.assertFalse(
            self.session.user_data(self.item["Id"])
            .get("PlaybackPositionTicks"),
            "a finished book kept a resume position")


if __name__ == "__main__":
    unittest.main()
