"""End-of-file edges, against a real server.

The three historically worst of them, each with an issue behind it and each
reproducible only with a server on the other end:

* the **last** item in a queue, which ends via `playback-abort` rather than
  `eof-reached` because there is no next file for `keep_open` to hold on —
  the path that decides whether finishing a series marks the finale watched;
* a **seek to the very end** (#541), where the historic complaint is that the
  end-of-file notification never arrives at all when you skip into it rather
  than play into it;
* **replaying something already finished** (#157, closed #323), which used to
  start at EOF, mark itself watched again and skip straight past.

Each class owns its own series, so nothing here depends on execution order —
and each resets that series' playstate in `setUp` as well as on cleanup, so a
previous run that died halfway cannot change the outcome either.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402


class _ShowCase(_e2e.E2ETestCase):
    """A case that owns one series. Subclasses set `SHOW` (and `SEASON`)."""

    SHOW = None
    SEASON = 1
    NEEDED = 2

    def setUp(self):
        super().setUp()
        self.eps = self.session.episodes(self.SHOW, season=self.SEASON)
        self.assertGreaterEqual(
            len(self.eps), self.NEEDED,
            "%s needs %d episodes" % (self.SHOW, self.NEEDED))
        self.ep_ids = [e["Id"] for e in self.eps]
        # Reset in setUp, not only on cleanup: a run that died halfway would
        # otherwise leave this series watched and change what the next run
        # measures. This is what makes the suite order-independent.
        self.session.reset_played(*self.ep_ids)
        self.addCleanup(self.session.reset_played, *self.ep_ids)

    def play_queue(self, ids, seq=0):
        media = _e2e.build_media(self.session, ids, seq=seq)
        video = media.video
        self.assertIsNotNone(video, "Media built no video")
        self.pm.play(video, is_initial_play=True)
        self.assertTrue(self.pm._player.duration,
                        "real mpv never reported a duration")
        return video

    def assert_played(self, item_id, msg, timeout=20):
        got = _e2e.wait_for(
            lambda: self.session.user_data(item_id).get("Played"),
            timeout=timeout)
        self.assertTrue(got, msg)


@_e2e.require_server_and_mpv
class EndOfQueueTest(_ShowCase):
    """The last item in a queue still gets marked watched.

    It ends differently from every other item: with no next file, mpv reports
    `playback-abort` rather than `eof-reached`, and the 07-25 checklist calls
    this out specifically. Run under both `force_set_played` values because
    they reach the same outcome by different routes — the setting makes the
    shim mark it, and without it the server infers it from a stop report past
    `MaxResumePct` (90%). A user cares that the finale is watched; which of
    the two did it is the implementation detail.
    """

    SHOW = "Absolute Numbering Show"
    NEEDED = 1

    def _play_single_to_end(self):
        last = self.eps[0]
        self.play_queue([last["Id"]])
        finished = self.pump_until(lambda: self.pm._video is None
                                   or (self.pm._player.playback_time or 0) >= 9.0,
                                   timeout=45)
        self.assertTrue(finished, "the single item never reached its end")
        self.pm.send_timeline()
        return last

    def test_the_last_item_is_marked_watched_with_force_set_played(self):
        with mock.patch.object(self.player_module.settings,
                               "force_set_played", True):
            last = self._play_single_to_end()
        self.assert_played(
            last["Id"],
            "the last item in a queue was not marked watched "
            "(force_set_played on)")

    def test_the_last_item_is_marked_watched_without_force_set_played(self):
        with mock.patch.object(self.player_module.settings,
                               "force_set_played", False):
            last = self._play_single_to_end()
        self.assert_played(
            last["Id"],
            "the last item in a queue was not marked watched "
            "(force_set_played off — the server should infer it from the "
            "stop report past MaxResumePct)")


@_e2e.require_server_and_mpv
class SeekToEndTest(_ShowCase):
    """#541 — skipping to the end must fire EOF, not park there.

    The reported symptom is a queue that stops instead of advancing. The
    integration suite covers this against a local clip; the point of doing it
    again here is that the bytes now arrive over HTTP from the server, which
    is where the reporters were.
    """

    SHOW = "Flat Show No Season Folders"

    def test_seeking_to_the_end_advances_the_queue(self):
        self.play_queue(self.ep_ids[:2])
        duration = self.pm._player.duration
        self.assertTrue(duration and duration > 1)

        self.pm.seek(max(duration - 0.3, 0), absolute=True, exact=True)

        advanced = self.pump_until(
            lambda: self.pm._video is not None
            and self.pm._video.item_id == self.ep_ids[1],
            timeout=45)
        self.assertTrue(
            advanced, "a seek to the end did not fire EOF / advance the queue")
        self.assert_played(
            self.ep_ids[0],
            "the episode we seeked to the end of was not marked watched")


@_e2e.require_server_and_mpv
class ReplayFinishedTest(_ShowCase):
    """#157 / #323 — replaying a finished episode starts at the start.

    The failure was that it resumed at EOF, remarked itself watched and
    skipped straight to the next one, so choosing to rewatch something played
    the episode after it. Reported external-mpv-only, which is why this runs
    on both legs.
    """

    SHOW = "Show With Missing Episodes"

    def test_replaying_a_watched_episode_does_not_skip_it(self):
        # 1) Finish it for real, so the replay starts from the state the
        #    reporters were in rather than from a flag we set by hand.
        self.play_queue([self.ep_ids[0]])
        self.pump_until(lambda: self.pm._video is None
                        or (self.pm._player.playback_time or 0) >= 9.0,
                        timeout=45)
        self.pm.send_timeline()
        self.pm.stop()
        self.assert_played(self.ep_ids[0],
                           "the episode never became watched, so the replay "
                           "below would not be testing anything")

        # 2) Play it again, with a next episode queued behind it — the bug
        #    needs somewhere to skip *to* in order to be visible.
        video = self.play_queue(self.ep_ids[:2])
        self.assertEqual(video.item_id, self.ep_ids[0])

        started_near_zero = _e2e.wait_for(
            lambda: (self.pm._player.playback_time or 0) < 3.0, timeout=10)
        self.assertTrue(started_near_zero,
                        "the replay did not start near the beginning")

        # 3) It must still be on the same episode a few seconds later. A skip
        #    was instant, so this does not need the full runtime to catch it.
        self.pump_until(lambda: False, timeout=4)
        self.assertIsNotNone(self.pm._video, "playback stopped during a replay")
        self.assertEqual(
            self.pm._video.item_id, self.ep_ids[0],
            "replaying a watched episode skipped to the next one")
        self.assertLess(
            self.pm._player.playback_time or 0, 9.0,
            "the replay is sitting at the end of the file rather than "
            "playing it")


if __name__ == "__main__":
    unittest.main()
