"""App / thread / process lifecycle tests.

Covers the three background daemons and the teardown paths that must stay
deterministic and non-hanging:

* ``ActionThread`` (action_thread.py) and ``TimelineManager`` (timeline.py) —
  each is a "must never die" loop pumped by a trigger Event. We start one, tick
  it, prove it survives an exception thrown by its stubbed collaborator, then
  ``stop()`` it and assert the join returns promptly and the thread is dead.
* ``PlayerManager.terminate()`` — stops playback, terminates the external mpv
  (only on the jsonipc backend), and stops trickplay.
* ``ClientManager.stop()`` — idempotent, and prompt even with an in-flight
  reconnect sleep parked on ``_stop_event`` (reuses the seams from
  test_clients_concurrency).
  and drops the dead child's command queue (the fork-echo / queue-leak fix).

Determinism: threads are driven by their trigger Events and woken by hand;
collaborators signal completion via ``threading.Event`` rather than sleeps.
"""

import os
import sys
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402


# player.py must be imported against the fake mpv *before* action_thread /
# timeline (which do ``from .player import playerManager``).
player = h.import_player_with_fake_mpv()

import jellyfin_mpv_shim.action_thread as action_thread  # noqa: E402
import jellyfin_mpv_shim.timeline as timeline            # noqa: E402


def _join_prompt(thread, stop_call, limit=3.0):
    """Invoke ``stop_call`` (which joins internally) and assert it returns well
    under ``limit`` seconds and the thread is dead."""
    start = time.monotonic()
    stop_call()
    elapsed = time.monotonic() - start
    return elapsed, thread.is_alive()


class ActionThreadLifecycleTest(unittest.TestCase):
    class FakePM:
        def __init__(self):
            self.updates = 0
            self.ticked = threading.Event()
            self.raise_once = False

        def is_active(self):
            return True

        def update(self):
            self.updates += 1
            self.ticked.set()
            if self.raise_once:
                self.raise_once = False
                raise RuntimeError("update blew up")

    def test_ticks_and_stops_promptly(self):
        fake = self.FakePM()
        t = action_thread.ActionThread()
        self.addCleanup(lambda: t.halt or t.stop())
        with mock.patch.object(action_thread, "playerManager", fake):
            t.start()
            # The loop ticks once immediately (is_active True).
            self.assertTrue(fake.ticked.wait(3), "action thread never ticked")
            elapsed, alive = _join_prompt(t, t.stop)
        self.assertLess(elapsed, 2.0, "stop() did not return promptly")
        self.assertFalse(alive, "action thread still alive after stop()")
        self.assertGreaterEqual(fake.updates, 1)

    def test_survives_exception_in_update(self):
        # A stubbed update() that raises must not kill the loop — the next tick
        # still runs. This pins the "must never die" guard.
        fake = self.FakePM()
        t = action_thread.ActionThread()
        self.addCleanup(lambda: t.halt or t.stop())
        with mock.patch.object(action_thread, "playerManager", fake):
            t.start()
            self.assertTrue(fake.ticked.wait(3))
            before = fake.updates
            fake.ticked.clear()
            fake.raise_once = True
            t.trigger.set()  # force another iteration
            self.assertTrue(fake.ticked.wait(3), "thread died on exception")
            self.assertTrue(t.is_alive())
            # And it keeps going after the raise.
            fake.ticked.clear()
            t.trigger.set()
            self.assertTrue(fake.ticked.wait(3))
            self.assertGreater(fake.updates, before + 1)
            elapsed, alive = _join_prompt(t, t.stop)
        self.assertLess(elapsed, 2.0)
        self.assertFalse(alive)

    def test_final_drain_runs_on_stop(self):
        # stop() sets halt and the loop exits, but the final drain must still
        # call update() once (shutdown-queued tasks depend on it).
        fake = self.FakePM()
        t = action_thread.ActionThread()
        self.addCleanup(lambda: t.halt or t.stop())
        with mock.patch.object(action_thread, "playerManager", fake):
            t.start()
            self.assertTrue(fake.ticked.wait(3))
            count_before_stop = fake.updates
            t.stop()
        # The final drain is an extra update() beyond whatever ticked while running.
        self.assertGreater(fake.updates, count_before_stop)
        self.assertFalse(t.is_alive())


class TimelineThreadLifecycleTest(unittest.TestCase):
    class FakePM:
        def __init__(self, raise_in_send=False):
            self.sent = 0
            self.ticked = threading.Event()
            self._raise_in_send = raise_in_send
            self.raise_once = False

        def is_active(self):
            return True

        def is_paused(self):
            return False

        def has_video(self):
            return True

        def send_timeline(self):
            self.sent += 1
            self.ticked.set()
            if self.raise_once:
                self.raise_once = False
                raise RuntimeError("send_timeline blew up")

    def test_ticks_and_stops_promptly(self):
        fake = self.FakePM()
        t = timeline.TimelineManager()
        self.addCleanup(lambda: t.halt or t.stop())
        with mock.patch.object(timeline, "playerManager", fake):
            t.start()
            self.assertTrue(fake.ticked.wait(3), "timeline thread never ticked")
            elapsed, alive = _join_prompt(t, t.stop)
        self.assertLess(elapsed, 2.0, "stop() did not return promptly")
        self.assertFalse(alive)
        self.assertGreaterEqual(fake.sent, 1)

    def test_survives_exception_in_send_timeline(self):
        # send_timeline() raising a bare Exception is caught by the run() guard
        # (the static send_timeline only swallows HTTPException / _mpv_errors),
        # so the thread must keep reporting.
        fake = self.FakePM()
        t = timeline.TimelineManager()
        self.addCleanup(lambda: t.halt or t.stop())
        with mock.patch.object(timeline, "playerManager", fake):
            t.start()
            self.assertTrue(fake.ticked.wait(3))
            before = fake.sent
            fake.ticked.clear()
            fake.raise_once = True
            t.trigger.set()
            self.assertTrue(fake.ticked.wait(3), "timeline thread died on exception")
            self.assertTrue(t.is_alive())
            fake.ticked.clear()
            t.trigger.set()
            self.assertTrue(fake.ticked.wait(3))
            self.assertGreater(fake.sent, before + 1)
            elapsed, alive = _join_prompt(t, t.stop)
        self.assertLess(elapsed, 2.0)
        self.assertFalse(alive)


class PlayerTerminateTest(unittest.TestCase):
    def test_terminate_stops_and_tears_down(self):
        pm = h.build_player(player)
        calls = {"stop": 0}
        # Isolate terminate()'s own contract: it must call stop(), stop trickplay,
        # and (only on external mpv) terminate the player process.
        pm.stop = lambda: calls.__setitem__("stop", calls["stop"] + 1)
        trick_stopped = []
        pm.trickplay = mock.Mock()
        pm.trickplay.stop = lambda: trick_stopped.append(True)

        pm.terminate()

        self.assertEqual(calls["stop"], 1, "terminate() did not call stop()")
        self.assertEqual(trick_stopped, [True], "trickplay was not stopped")
        if player.is_using_ext_mpv:
            self.assertTrue(pm._player.terminated,
                            "external mpv not terminated on jsonipc backend")
        else:
            self.assertFalse(pm._player.terminated,
                             "libmpv player should not be .terminate()d")


class ClientManagerStopTest(unittest.TestCase):
    def _manager(self):
        from jellyfin_mpv_shim import clients as clients_module
        with mock.patch.object(clients_module.settings,
                               "health_check_interval", None):
            cm = clients_module.ClientManager()
        return cm

    def test_stop_is_prompt_with_inflight_reconnect_sleep(self):
        # A reconnect/retry loop parks on _stop_event.wait(long). stop() sets the
        # event, which must wake that sleep immediately instead of blocking
        # shutdown for the full backoff.
        cm = self._manager()

        woke = threading.Event()
        started = threading.Event()

        def fake_reconnect():
            started.set()
            # Would be wait(30) in connect_all's retry loop.
            if cm._stop_event.wait(30):
                woke.set()

        t = threading.Thread(target=fake_reconnect)
        t.start()
        self.assertTrue(started.wait(3))

        start = time.monotonic()
        cm.stop()
        t.join(3)
        elapsed = time.monotonic() - start

        self.assertTrue(woke.is_set(), "stop() did not wake the reconnect sleep")
        self.assertFalse(t.is_alive())
        self.assertLess(elapsed, 2.0, "stop() blocked on the reconnect backoff")

    def test_stop_drains_clients_and_is_idempotent(self):
        cm = self._manager()

        class FakeClient:
            def __init__(self):
                self.stopped = 0

            def stop(self):
                self.stopped += 1

        c = FakeClient()
        cm.clients["s1"] = c

        cm.stop()
        self.assertEqual(cm.clients, {}, "clients not drained by stop()")
        self.assertEqual(c.stopped, 1)
        self.assertTrue(cm.is_stopping)

        # Second stop() must be a harmless no-op (no re-stop, no error).
        cm.stop()
        self.assertEqual(cm.clients, {})
        self.assertEqual(c.stopped, 1, "idempotent stop() re-stopped a client")


if __name__ == "__main__":
    unittest.main()


class EndOfQueueTest(unittest.TestCase):
    """Reaching the end of a queue has to leave the player genuinely idle.

    Leaving _video set kept is_active() true, so once the browser re-loaded
    its background image (clearing playback-abort) the next timeline tick
    reported the finished item as *playing* again and the UI bounced back to
    the player with the ended video paused.
    """

    def _player(self):
        pm = h.build_player(player)
        pm._mpv_alive = True
        pm.should_send_timeline = True
        pm.send_timeline_stopped = lambda *a, **k: None
        pm.pushed = []
        pm.push_playstate = lambda stopped=False: pm.pushed.append(stopped)
        return pm

    def _video(self, has_next=False):
        class Parent:
            queue = []

            def __init__(self):
                self.has_next = has_next

        class Video:
            def __init__(self):
                self.parent = Parent()
                self.item = {"Name": "Ended", "Type": "Movie"}
                self.terminated = False

            def get_duration(self):
                return 100

            def set_played(self):
                pass

            def terminate_transcode(self):
                self.terminated = True

        return Video()

    def test_end_of_queue_clears_the_video(self):
        pm = self._player()
        pm._video = self._video()
        pm.finished_callback(True)
        self.assertIsNone(pm._video)
        self.assertFalse(pm.is_active())

    def test_end_of_queue_unloads_the_file(self):
        pm = self._player()
        video = self._video()
        pm._video = video
        pm.finished_callback(True)
        self.assertIn(("stop",), [tuple(c) for c in pm._player.commands])
        self.assertTrue(video.terminated)

    def test_end_of_queue_reports_stopped_once(self):
        pm = self._player()
        pm._video = self._video()
        pm.finished_callback(True)
        self.assertEqual(pm.pushed, [True])

    def test_mid_queue_does_not_stop(self):
        """With a next item, finished_callback advances instead."""
        pm = self._player()
        pm._video = self._video(has_next=True)
        played = []
        pm.play = lambda v: played.append(v)
        pm._video.parent.get_next = lambda: type("E", (), {"video": "next"})()
        pm.finished_callback(True)
        self.assertEqual(played, ["next"])
        self.assertEqual(pm.pushed, [])
