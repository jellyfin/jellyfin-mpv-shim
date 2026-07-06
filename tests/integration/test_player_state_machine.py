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


if __name__ == "__main__":
    unittest.main()
