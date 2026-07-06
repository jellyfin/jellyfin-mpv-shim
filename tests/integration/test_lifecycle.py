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
* ``gui_mgr.UserInterface.on_browser_died`` — detaches the log / sync callbacks
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


class BrowserDeathDetachTest(unittest.TestCase):
    """gui_mgr.on_browser_died must detach the forwarding callbacks and drop the
    dead child's command queue. A real child process is avoided here (fork under
    a test runner is flaky); the process is mocked and on_browser_died is driven
    directly — which is exactly the leak/echo path being pinned."""

    def test_on_browser_died_detaches_callbacks_and_nulls_queue(self):
        import jellyfin_mpv_shim.gui_mgr as gui_mgr

        ui = gui_mgr.UserInterface.__new__(gui_mgr.UserInterface)
        ui._shutting_down = False
        ui.browser_ready = True
        ui.tray_alive = True  # so it minimizes-to-tray rather than quitting
        ui.browser_cmd_queue = mock.Mock()
        ui.browser_process = mock.Mock()
        ui.browser_process.join = mock.Mock()

        # Sentinels the browser launch would have installed.
        sentinel_log = lambda line: None
        sentinel_change = lambda: None
        sentinel_progress = lambda item_id, name, downloaded, total: None
        gui_mgr.guiHandler.callback = sentinel_log
        gui_mgr.syncManager.on_change = sentinel_change
        gui_mgr.syncManager.on_progress = sentinel_progress
        self.addCleanup(setattr, gui_mgr.guiHandler, "callback", None)

        ui.on_browser_died(None)

        # The forwarding callbacks are detached (reset to no-ops, not the
        # sentinels), and the dead child's queue is dropped.
        self.assertIsNot(gui_mgr.guiHandler.callback, sentinel_log)
        self.assertIsNone(gui_mgr.guiHandler.callback)
        self.assertIsNot(gui_mgr.syncManager.on_change, sentinel_change)
        self.assertIsNot(gui_mgr.syncManager.on_progress, sentinel_progress)
        self.assertIsNone(ui.browser_cmd_queue)
        self.assertIsNone(ui.browser_process)
        # A push into the (now detached) sync callbacks must be a safe no-op.
        gui_mgr.syncManager.on_change()
        gui_mgr.syncManager.on_progress("i", "n", 1, 2)


if __name__ == "__main__":
    unittest.main()
