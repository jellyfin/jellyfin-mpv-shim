"""The queue advances, and the server agrees that it did.

This is the suite's proof-of-concept and its most valuable single test. The
integration suite's `test_realmpv_smoke` already plays a real clip through a
real mpv and watches a genuine EOF auto-advance — but it plays a local file
and the Jellyfin *session* is a recording fake, deliberately (see
`tests/integration/README.md`). So the shim's half of the loop is asserted and
the round trip is not.

Here the episode comes from a real library, over a real HTTP stream URL the
server resolved, through a real `Media` built from real DTOs, and the
watched-marking is read back off the server rather than off a fake we wrote.
Auto-advance and watched-marking are one of the two largest open-bug clusters
in the tracker (#157, #323, #458, #541); every one of those reproduced only
against a server.

stdjflib's episodes are 10 seconds, which is what makes playing one to its end
a reasonable thing for a test to do.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

SHOW = "The Standard Show"
SERVER_HOST = (_e2e.SERVER.split("//")[-1].split("/")[0]
               if _e2e.SERVER else "")


@_e2e.require_server_and_mpv
class QueueAdvanceTest(_e2e.E2ETestCase):

    def setUp(self):
        super().setUp()
        eps = self.session.episodes(SHOW, season=1)
        self.assertGreaterEqual(
            len(eps), 2, "need two episodes to test an advance")
        self.first, self.second = eps[0], eps[1]
        ids = (self.first["Id"], self.second["Id"])
        # Watched state persists on the server, so a second run would start
        # dirty. Clear it both ways round.
        self.session.reset_played(*ids)
        self.addCleanup(self.session.reset_played, *ids)

    def test_an_episode_plays_out_and_the_next_one_starts(self):
        media = _e2e.build_media(self.session, [self.first["Id"],
                                                self.second["Id"]])
        video = media.video
        self.assertIsNotNone(video, "Media built no video for a live server")

        # The stream URL is the server's answer, not ours. A direct-play URL
        # here is also the assertion that this library needs no transcode —
        # if that ever changes the timings below stop being reliable and we
        # want to know from this line rather than from a flaky EOF.
        url = video.get_playback_url()
        self.assertIn(SERVER_HOST, url,
                      "playback URL does not point at the server under test")
        self.assertFalse(video.is_transcode,
                         "expected direct play for a generated H.264 episode")

        self.pm.play(video, is_initial_play=True)
        self.assertIs(self.pm._video, video)
        self.assertTrue(
            self.pm._player.duration and self.pm._player.duration > 0,
            "real mpv never reported a duration for the stream")

        # 1) The server is told we started, and what we started.
        self.pm.send_timeline()
        playing = _e2e.wait_for(
            lambda: (self.session.my_session() or {}).get("NowPlayingItem"))
        self.assertTrue(playing, "the server never saw this device playing")
        self.assertEqual(playing["Id"], self.first["Id"],
                         "the server thinks we are playing something else")

        # 2) A genuine end-of-file advances the queue. Ten seconds of media,
        #    so the timeout is slack rather than a guess.
        advanced = self.pump_until(
            lambda: self.pm._video is not None
            and self.pm._video.item_id == self.second["Id"],
            timeout=45)
        self.assertTrue(advanced, "the queue did not advance on EOF")

        # 3) The finished episode is watched *on the server*. This is the
        #    assertion the fake session cannot make, and the one that has
        #    regressed repeatedly.
        self.pm.send_timeline()
        played = _e2e.wait_for(
            lambda: self.session.user_data(self.first["Id"]).get("Played"))
        self.assertTrue(played,
                        "the finished episode was not marked watched on the "
                        "server")

        # Not asserted: that the episode advanced INTO is still unwatched.
        # The server marks an item under MinResumeDurationSeconds (300 s)
        # played from its own progress reports once past MinResumePct, so on
        # these 10 s episodes that is a race with the next report, not a
        # property. What the app reports on a step: test_playback_lifecycle.


if __name__ == "__main__":
    unittest.main()
