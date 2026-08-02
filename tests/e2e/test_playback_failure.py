"""Media that cannot play must fail, not hang — and must not count as watched.

stdjflib builds three hostile files for this and says what each is for: a
truncated one ("playback should fail cleanly and report an error, not hang"),
a zero-byte one ("rejected at scan, and if it is not, it must fail gracefully
at playback") and a single-frame one ("duration rounds to zero in a lot of
arithmetic, and progress bars divide by it").

The failure mode these guard against is the one `qa-notranscode` exists to
find in the account list — "anything that cannot direct play must fail with a
clear message rather than spinning". A spinner is not an exception, so no unit
test sees it; what a test can see is that the call returns, that no video is
left behind, and that the server was not told you watched something you could
not play.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

LIBRARY = "Test Media"


@_e2e.require_server_and_mpv
class BrokenMediaTest(_e2e.E2ETestCase):

    def _item(self, name):
        item = self.session.find(name, library=LIBRARY)
        self.session.reset_played(item["Id"])
        self.addCleanup(self.session.reset_played, item["Id"])
        return item

    def _play(self, item):
        """Play, and bound how long that is allowed to take.

        `play()` is synchronous and gates on the file loading, so a hang here
        is the reported symptom rather than something we have to infer.
        """
        video = _e2e.build_media(self.session, [item["Id"]]).video
        self.assertIsNotNone(video, "Media built no video")
        started = time.time()
        self.pm.play(video, is_initial_play=True)
        return time.time() - started

    def test_a_truncated_file_fails_and_is_not_marked_watched(self):
        item = self._item("Truncated file")
        elapsed = self._play(item)
        self.assertLess(elapsed, 90, "play() hung on a truncated file")

        # It aborts partway. What matters is that it stops on its own and
        # leaves nothing behind claiming to be playing.
        ended = self.pump_until(lambda: self.pm._video is None, timeout=60)
        self.assertTrue(ended, "a truncated file never ended playback")

        _e2e.wait_for(lambda: self.session.user_data(item["Id"])
                      .get("PlayCount"), timeout=10)
        self.assertFalse(
            self.session.user_data(item["Id"]).get("Played"),
            "a file that failed mid-stream was recorded as watched")

    def test_a_zero_byte_file_is_refused_cleanly(self):
        item = self._item("Zero-byte file")
        elapsed = self._play(item)
        self.assertLess(elapsed, 90, "play() hung on a zero-byte file")

        # Nothing to decode: the load fails and no session should survive it.
        self.assertIsNone(
            self.pm._video,
            "a zero-byte file left a video attached to the player")
        time.sleep(1.0)
        self.assertFalse(
            self.session.user_data(item["Id"]).get("Played"),
            "an unplayable file was recorded as watched")

    def test_a_single_frame_file_does_not_break_the_progress_arithmetic(self):
        """One frame, ~1s. The point is that nothing divides by zero.

        Its runtime is short enough that finishing it legitimately marks it
        watched — that is correct and is not what this asserts. What it
        asserts is that playing it start to finish raises nothing and leaves
        the player in a clean state.
        """
        item = self._item("One frame")
        elapsed = self._play(item)
        self.assertLess(elapsed, 90, "play() hung on a single-frame file")

        ended = self.pump_until(lambda: self.pm._video is None, timeout=60)
        self.assertTrue(ended, "a single-frame file never ended playback")
        # Still usable afterwards: a divide-by-zero deep in the reporting path
        # would have taken the action pump down rather than raised here.
        self.pm.update()


if __name__ == "__main__":
    unittest.main()
