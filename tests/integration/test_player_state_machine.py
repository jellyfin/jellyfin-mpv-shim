"""PlayerManager state-machine / race tests, backed by :class:`FakeMPV`.

No real mpv, server, or window: the fake backend lets us fire the eof / abort /
shutdown callbacks that ``PlayerManager`` reacts to, on arbitrary threads and in
adversarial orders, and drive the queued-task drain (normally pumped by the
action thread) by hand.

Each test names the audit-era race it guards against. The central invariants
under test are the *playback epoch* (`_play_epoch`) and the *finished dedup
lock* (`_finished_lock`) that together decide whether an end-of-playback
callback is allowed to advance the queue.
"""

import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, __import__("os").path.dirname(__file__))
import _harness as h  # noqa: E402


player_module = h.import_player_with_fake_mpv()


class FakeParent:
    def __init__(self, has_next=False, next_video=None):
        self.has_next = has_next
        self.has_prev = False
        self._next_video = next_video
        self.is_local = False
        self.queue = []

    def get_next(self):
        return FakeMediaItem(self._next_video)


class FakeMediaItem:
    def __init__(self, video):
        self.video = video


class FakeVideo:
    def __init__(self, item_id="v", duration=100, has_next=False, next_video=None,
                 client="client"):
        self.item_id = item_id
        self._duration = duration
        self.parent = FakeParent(has_next, next_video)
        self.client = client
        self.is_transcode = False
        self.aid = None
        self.sid = None
        self.media_source = {"Id": "ms"}
        self.playback_info = {"PlaySessionId": "ps"}
        self.played = []          # records set_played calls
        self.terminated = False

    def get_duration(self):
        return self._duration

    def get_playlist_id(self):
        return "pl-%s" % self.item_id

    def set_played(self, value=True):
        self.played.append(value)

    def terminate_transcode(self):
        self.terminated = True


class RecordingClient:
    """Jellyfin client stand-in that records the stop payload, so tests can
    assert *what position* an item was reported stopped at (mid-file vs. full
    duration)."""

    def __init__(self):
        self.jellyfin = self
        self.stops = []

    def session_stop(self, options):
        self.stops.append(options)

    def session_progress(self, options):
        pass

    def session_playing(self, options):
        pass


class RaisingMPV(h.FakeMPV):
    """FakeMPV that raises a chosen 'mpv is gone' error when a named property is
    *read*. The base FakeMPV can only raise from command(); this models an mpv
    that died under a property access (the #458/#503 close-crash path), which is
    what send_timeline / the observers actually hit."""

    def __init__(self, raise_prop=None, raise_exc=None, **kw):
        object.__setattr__(self, "_raise_prop", raise_prop)
        object.__setattr__(self, "_raise_exc", raise_exc)
        super().__init__(**kw)

    def __getattribute__(self, name):
        d = object.__getattribute__(self, "__dict__")
        if d.get("_raise_prop") == name:
            raise d.get("_raise_exc")
        return object.__getattribute__(self, name)


def _stub_advance(pm):
    """Replace the collaborators finished_callback fans out to with recorders so
    the *decision* (advance vs stop vs skip) is observable without dragging in
    real play() / timeline plumbing."""
    calls = {"play": [], "stopped": []}
    pm.play = lambda video, *a, **k: calls["play"].append(video)
    pm.send_timeline_stopped = lambda *a, **k: calls["stopped"].append((a, k))
    return calls


class FinishedCallbackTest(unittest.TestCase):
    def _player(self, **video_kw):
        pm = h.build_player(player_module)
        pm._video = FakeVideo(**video_kw)
        return pm

    def test_eof_auto_advances_to_next(self):
        # Genuine EOF with a next item + auto_play → advance to the next video.
        nxt = FakeVideo(item_id="next")
        pm = self._player(has_next=True, next_video=nxt)
        pm._reached_eof = True
        calls = _stub_advance(pm)

        with mock.patch.object(player_module.settings, "auto_play", True), \
                mock.patch.object(player_module.settings, "force_set_played", True):
            pm._queue_finished()
            pm.update()  # action-thread drain

        self.assertEqual(calls["play"], [nxt])
        # The finished item was at a real EOF, so force_set_played marks it.
        self.assertEqual(pm._video.played if pm._video is nxt else None, None)

    def test_cast_landing_at_eof_does_not_skip_the_cast_item(self):
        # AUDIT RACE (cast-at-eof epoch): an EOF fires and queues a finished
        # callback; in the same instant a cast/play swaps in a new item and
        # bumps the epoch. The stale callback must NOT run — otherwise it marks
        # the just-cast item played and auto-advances past it.
        pm = self._player(has_next=True, next_video=FakeVideo(item_id="next"))
        pm._reached_eof = True
        calls = _stub_advance(pm)

        with mock.patch.object(player_module.settings, "auto_play", True), \
                mock.patch.object(player_module.settings, "force_set_played", True):
            pm._queue_finished()  # queued under epoch 0

            # A new item is cast: replicate the epoch bump + lock release that
            # _play_media performs when a fresh file becomes current.
            cast_video = FakeVideo(item_id="cast")
            pm._play_epoch += 1
            if pm._finished_lock.locked():
                pm._finished_lock.release()
            pm._video = cast_video
            pm._reached_eof = False

            pm.update()  # drain the now-stale callback

        self.assertEqual(calls["play"], [], "stale callback advanced the queue")
        self.assertEqual(cast_video.played, [], "just-cast item wrongly marked played")

    def test_abort_far_from_end_not_marked_watched(self):
        # AUDIT RACE (abort vs eof): playback-abort fires on decode/network
        # failure too, not just a clean finish. An abort far from the end must
        # not be recorded as watched.
        pm = self._player(has_next=False, duration=100)
        pm._reached_eof = False
        pm._last_playback_position = 12  # nowhere near the 100s end
        calls = _stub_advance(pm)

        with mock.patch.object(player_module.settings, "force_set_played", True), \
                mock.patch.object(player_module.settings, "auto_play", True):
            pm.finished_callback(True, pm._play_epoch)

        self.assertEqual(pm._video.played, [])
        self.assertEqual(calls["play"], [])

    def test_eof_at_end_is_marked_watched(self):
        # Positive control for the above: a genuine EOF is recorded watched.
        pm = self._player(has_next=False, duration=100)
        pm._reached_eof = True
        _stub_advance(pm)
        with mock.patch.object(player_module.settings, "force_set_played", True):
            pm.finished_callback(True, pm._play_epoch)
        self.assertEqual(pm._video.played, [True])

    def test_video_nulled_mid_callback_is_survived(self):
        # AUDIT RACE (_video snapshot): another thread (stop / mpv disconnect)
        # can null self._video even while we hold _lock. finished_callback must
        # snapshot and bail, not crash.
        pm = self._player()
        pm._video = None
        pm.pause_ignore = True
        # Must not raise, and must clear pause_ignore.
        pm.finished_callback(True, pm._play_epoch)
        self.assertFalse(pm.pause_ignore)

    def test_only_one_of_racing_finish_observers_advances(self):
        # AUDIT RACE (eof + abort both fire): the eof and abort observers can
        # both call _queue_finished. Only the one that wins the non-blocking
        # _finished_lock (has_lock=True) may advance; the loser no-ops.
        pm = self._player(has_next=True, next_video=FakeVideo(item_id="next"))
        pm._reached_eof = True
        calls = _stub_advance(pm)

        barrier = h.spin_barrier(2)

        def observer():
            barrier.wait()
            pm._queue_finished()

        with mock.patch.object(player_module.settings, "auto_play", True), \
                mock.patch.object(player_module.settings, "force_set_played", False):
            h.run_concurrently(observer, 2)
            # Two tasks are queued (one with the lock, one without). Drain both.
            pm.update()

        # Exactly one advance, regardless of which observer won the lock.
        self.assertEqual(len(calls["play"]), 1)


class UpdateDrainTest(unittest.TestCase):
    def test_update_runs_all_queued_tasks_and_survives_exceptions(self):
        # INVARIANT (action-thread survival): update() pumps the task queue and
        # a single failing task must not abort the drain or kill the pump —
        # otherwise every later stop/next/menu action is silently dropped.
        pm = h.build_player(player_module)
        ran = []

        def ok(tag):
            ran.append(tag)

        def boom():
            raise RuntimeError("task blew up")

        pm.put_task(ok, "a")
        pm.put_task(boom)
        pm.put_task(ok, "b")
        pm.update()
        self.assertEqual(ran, ["a", "b"])
        self.assertTrue(pm.evt_queue.empty())

    def test_mpv_disconnect_error_in_task_is_handled_not_raised(self):
        # A task hitting a dead mpv raises a _mpv_errors member; update() must
        # route it to _handle_mpv_disconnect, not let it escape the drain.
        pm = h.build_player(player_module)
        handled = []
        pm._handle_mpv_disconnect = lambda: handled.append(True)

        def dead():
            raise BrokenPipeError()

        pm.put_task(dead)
        pm.update()
        self.assertEqual(handled, [True])


class ShutdownTeardownTest(unittest.TestCase):
    def test_shutdown_task_nulls_video_and_runs_stop_cmd(self):
        # The mpv "shutdown" event only flips a flag + queues _handle_mpv_shutdown
        # onto the action thread; the queued task does the _video swap (under
        # _lock, serialized against stop/play) and the offline stop report.
        pm = h.build_player(player_module)
        pm._video = FakeVideo(item_id="v", client=None)
        reports = []
        pm._report_stopped_offline = lambda video: reports.append(video)
        stop_cmds = []

        with mock.patch.object(player_module.PlayerManager, "exec_stop_cmd",
                               staticmethod(lambda: stop_cmds.append(True))):
            # Run the queued teardown task as the action thread would.
            pm.put_task(pm._handle_mpv_shutdown)
            pm.update()

        self.assertIsNone(pm._video)
        # The offline stop report is spawned on a daemon thread; give it a beat.
        for _ in range(50):
            if reports:
                break
            threading.Event().wait(0.01)
        self.assertEqual(len(reports), 1)
        self.assertEqual(stop_cmds, [True])

    def test_shutdown_task_drains_even_when_mpv_already_dead(self):
        # INVARIANT: the shutdown teardown must run even after _mpv_alive is
        # False — update() drains the queue before it ever touches the player.
        pm = h.build_player(player_module)
        pm._mpv_alive = False
        pm._video = FakeVideo(item_id="v", client=None)
        pm._report_stopped_offline = lambda video: None
        ran = []
        pm.put_task(lambda: ran.append(True))
        pm.update()
        self.assertEqual(ran, [True])


class BackendMatrixTest(unittest.TestCase):
    """Assertions that must hold identically on both mpv backends. Run once per
    backend by run_integration.py (JMS_TEST_BACKEND); the divergence is in which
    exception type means 'mpv is gone'."""

    def test_mpv_errors_tuple_matches_active_backend(self):
        errs = player_module._mpv_errors
        self.assertIn(BrokenPipeError, errs)   # shared by both backends
        if h.BACKEND == "libmpv":
            self.assertFalse(player_module.is_using_ext_mpv)
            self.assertIn(player_module.mpv.ShutdownError, errs)
        else:
            self.assertTrue(player_module.is_using_ext_mpv)
            self.assertIn(TimeoutError, errs)

    def test_backend_specific_disconnect_error_in_task_is_handled(self):
        # The disconnect guard must catch the backend's *divergent* error member
        # (ShutdownError on libmpv, TimeoutError on jsonipc), not only the shared
        # BrokenPipeError — a guard that caught one but not the other was an
        # audit-era, backend-specific bug.
        pm = h.build_player(player_module)
        handled = []
        pm._handle_mpv_disconnect = lambda: handled.append(True)
        err = h.backend_disconnect_error(player_module)

        def dead():
            raise err()

        pm.put_task(dead)
        pm.update()
        self.assertEqual(handled, [True],
                         "%s not caught by _mpv_errors on backend %s"
                         % (err.__name__, h.BACKEND))


class CloseCrashHangTest(unittest.TestCase):
    """Issue #458: closing mpv mid-playback crashed / hung the worker. A
    disconnect (ShutdownError on libmpv, BrokenPipeError on both) firing during
    the action-thread ``update()`` drain *or* during a timeline send must be
    caught, the worker must survive and keep draining, teardown must run, and
    the item torn down mid-file must NOT be reported at full duration or marked
    watched."""

    def _wait_for(self, predicate, timeout=1.0):
        # Bounded, deterministic wait: _handle_mpv_disconnect reports the stop
        # on a daemon thread. Poll rather than sleep-a-fixed-amount, so this
        # returns the instant the report lands (and fails fast if it never does).
        tick = threading.Event()
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if predicate():
                return True
            tick.wait(0.005)
        return predicate()

    def _mid_file_video(self):
        client = RecordingClient()
        video = FakeVideo(item_id="v", duration=100, client=client)
        return video, client

    def test_close_mid_update_survives_and_reports_mid_file(self):
        # A queued task hits a dead mpv (BrokenPipeError) mid-drain. update()
        # must route it to the real _handle_mpv_disconnect (not crash), keep
        # draining later tasks, null the video, and report the stop at the last
        # known position — never at full duration, never marked watched.
        video, client = self._mid_file_video()
        pm = h.build_player(player_module, video=video)
        pm.last_seek = 12.0        # mid-file
        pm.start_time = 1.0
        ran_after = []

        def dead():
            raise BrokenPipeError()

        pm.put_task(dead)
        pm.put_task(lambda: ran_after.append(True))  # proves the drain continued
        pm.update()

        self.assertEqual(ran_after, [True], "drain aborted after the disconnect")
        self.assertIsNone(pm._video, "video not cleared on disconnect")
        self.assertFalse(pm._mpv_alive)
        self.assertTrue(self._wait_for(lambda: bool(client.stops)),
                        "stop was never reported after mid-file close")
        reported = client.stops[-1]["PositionTicks"]
        self.assertEqual(reported, int(12.0 * 10000000),
                         "mid-file close reported at the wrong position")
        self.assertNotEqual(reported, int(100 * 10000000),
                            "mid-file close wrongly reported at full duration")
        self.assertEqual(video.played, [], "mid-file close wrongly marked watched")

    def test_close_mid_update_backend_divergent_error_survives(self):
        # The same, but with the backend's *divergent* disconnect member
        # (ShutdownError on libmpv, TimeoutError on jsonipc) — the #458 crash
        # class was backend-specific, so both must be caught mid-drain.
        video, client = self._mid_file_video()
        pm = h.build_player(player_module, video=video)
        pm.last_seek = 5.0
        pm.start_time = 1.0
        err = h.backend_disconnect_error(player_module)
        ran_after = []

        def dead():
            raise err()

        pm.put_task(dead)
        pm.put_task(lambda: ran_after.append(True))
        pm.update()

        self.assertEqual(ran_after, [True],
                         "%s aborted the drain on backend %s"
                         % (err.__name__, h.BACKEND))
        self.assertIsNone(pm._video)
        self.assertTrue(self._wait_for(lambda: bool(client.stops)))
        self.assertEqual(video.played, [])

    def test_close_mid_send_timeline_survives_and_not_watched(self):
        # A property read dies mid send_timeline() (the timeline-thread path).
        # _mpv_errors must catch it, the worker survives, the video is torn
        # down, and it is reported at its mid-file position, not marked watched.
        video, client = self._mid_file_video()
        pm = h.build_player(player_module, video=video)
        pm._player = RaisingMPV(raise_prop="playback_abort",
                                raise_exc=BrokenPipeError())
        pm.should_send_timeline = True
        pm.last_seek = 33.0
        pm.start_time = 1.0

        # run in a worker so run_concurrently re-raises anything that escapes —
        # a clean return proves send_timeline swallowed the disconnect itself.
        h.run_concurrently(pm.send_timeline, 1)

        self.assertIsNone(pm._video)
        self.assertFalse(pm._mpv_alive)
        self.assertTrue(self._wait_for(lambda: bool(client.stops)))
        self.assertEqual(client.stops[-1]["PositionTicks"], int(33.0 * 10000000))
        self.assertEqual(video.played, [])


@unittest.skipUnless(h.BACKEND == "jsonipc",
                     "#503 is the external-mpv broken-pipe path")
class ExternalBrokenPipeTest(unittest.TestCase):
    """Issue #503: external mpv drops its IPC pipe (BrokenPipeError) on a
    property read during send_timeline. _mpv_errors must catch it and the
    timeline worker must keep running rather than dying on the escape."""

    def test_broken_pipe_in_send_timeline_keeps_worker_running(self):
        client = RecordingClient()
        video = FakeVideo(item_id="v", duration=100, client=client)
        pm = h.build_player(player_module, video=video)
        pm._player = RaisingMPV(raise_prop="playback_abort",
                                raise_exc=BrokenPipeError())
        pm.should_send_timeline = True
        pm.last_seek = 7.0
        pm.start_time = 1.0

        # First send hits the broken pipe; must not raise out of the worker.
        h.run_concurrently(pm.send_timeline, 1)
        self.assertFalse(pm._mpv_alive)

        # The worker is still alive: a subsequent tick runs cleanly (video is
        # gone / should_send_timeline cleared, so it early-returns) rather than
        # throwing a second time.
        h.run_concurrently(pm.send_timeline, 1)


class ResumeAtEofTest(unittest.TestCase):
    """Issues #157 / #323: mpv resuming a file at (or past) its end must not be
    mistaken for a genuine finish (which produced endless episode skipping,
    especially with auto shader profiles reloading the file). #323 was
    external-only, so running this on both backends is the parity check."""

    def _player(self, **video_kw):
        pm = h.build_player(player_module)
        pm._video = FakeVideo(**video_kw)
        return pm

    def test_builtin_resume_playback_disabled(self):
        # a5731db (#323): mpv's own watch-later resume is turned off at init so
        # it can't seek a fresh file to a saved end position under us.
        self.assertIs(player_module.playerManager._player.resume_playback, False)

    def test_resume_at_end_not_marked_watched(self):
        # The live player sits at the very end (a resume position), but no
        # genuine eof was observed and no timeline tick recorded a position.
        # _finished_at_eof keys off _reached_eof / _last_playback_position, not
        # the live player time, so this is NOT counted as watched.
        pm = self._player(has_next=False, duration=100)
        pm._reached_eof = False
        pm._last_playback_position = 0
        pm._player.playback_time = 100      # resume-at-EOF
        pm._player.duration = 100
        calls = _stub_advance(pm)

        with mock.patch.object(player_module.settings, "force_set_played", True), \
                mock.patch.object(player_module.settings, "auto_play", True):
            pm.finished_callback(True, pm._play_epoch)

        self.assertEqual(pm._video.played, [],
                         "resume-at-end wrongly marked the item watched")
        self.assertEqual(calls["play"], [],
                         "resume-at-end wrongly auto-advanced")

    def test_stale_finished_from_reload_discarded_by_epoch(self):
        # An auto-profile / shader reload re-loads the same file: the eof from
        # the prior load queued a finished callback under the old epoch, then
        # the reload bumps the epoch and resets _reached_eof (as _play_media
        # does). The stale callback must be discarded — not advance, not mark
        # watched — even though the reloaded file may momentarily read at end.
        nxt = FakeVideo(item_id="next")
        pm = self._player(has_next=True, next_video=nxt, duration=100)
        pm._reached_eof = True
        calls = _stub_advance(pm)

        with mock.patch.object(player_module.settings, "auto_play", True), \
                mock.patch.object(player_module.settings, "force_set_played", True):
            pm._queue_finished()  # queued under epoch 0 (the pre-reload eof)

            # The reload makes the same file current again: epoch bump + eof
            # reset + lock release, exactly as _play_media does.
            pm._play_epoch += 1
            pm._reached_eof = False
            pm._last_playback_position = 0
            if pm._finished_lock.locked():
                pm._finished_lock.release()

            pm.update()  # drain the now-stale callback

        self.assertEqual(calls["play"], [],
                         "stale post-reload callback auto-advanced")
        self.assertEqual(pm._video.played, [],
                         "stale post-reload callback marked the item watched")


if __name__ == "__main__":
    unittest.main()
