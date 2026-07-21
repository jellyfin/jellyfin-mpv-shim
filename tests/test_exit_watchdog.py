"""exit_watchdog picks the right threads to complain about.

``finish()`` itself ends the process, so it is exercised in a subprocess;
the selection and reporting logic is pure and tested in-process.
"""

import os
import subprocess
import sys
import threading
import time
import unittest

from jellyfin_mpv_shim import exit_watchdog

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class SurvivorSelectionTest(unittest.TestCase):
    """Only non-daemon threads keep the interpreter alive, so only those
    are worth reporting — a daemon straggler costs nothing at exit and
    would just be noise in a log people are reading to find a real leak.
    """

    def setUp(self):
        self.release = threading.Event()
        self.started = threading.Event()
        self.threads = []

    def tearDown(self):
        self.release.set()
        for t in self.threads:
            t.join(timeout=5)
            self.assertFalse(t.is_alive(), "test thread outlived the test")

    def _spawn(self, name, daemon):
        def body():
            self.started.set()
            self.release.wait(10)

        t = threading.Thread(target=body, name=name, daemon=daemon)
        self.threads.append(t)
        t.start()
        self.assertTrue(self.started.wait(5), "thread never started")
        self.started.clear()
        return t

    def test_a_non_daemon_straggler_is_reported(self):
        t = self._spawn("stuck-worker", daemon=False)
        names = [s.name for s in exit_watchdog._survivors()]
        self.assertIn("stuck-worker", names)

    def test_a_daemon_straggler_is_not_reported(self):
        self._spawn("daemon-worker", daemon=True)
        names = [s.name for s in exit_watchdog._survivors()]
        self.assertNotIn("daemon-worker", names)

    def test_the_main_thread_is_never_reported(self):
        names = [s.name for s in exit_watchdog._survivors()]
        self.assertNotIn(threading.main_thread().name, names)

    def test_the_report_names_the_thread_and_its_stack(self):
        t = self._spawn("stuck-worker", daemon=False)
        text = exit_watchdog._describe([t])
        self.assertIn("stuck-worker", text)
        # the frame it is parked in is the actionable half of the report
        self.assertIn("test_exit_watchdog.py", text)

    def test_the_grace_period_is_a_budget_for_all_stragglers(self):
        # Was a per-thread timeout, so N leaked threads meant N * GRACE
        # seconds of extra hang — the delay grew with exactly the problem
        # it was supposed to bound.
        threads = [self._spawn("straggler-%d" % i, daemon=False)
                   for i in range(4)]
        started = time.monotonic()
        exit_watchdog._await_stragglers(threads)
        elapsed = time.monotonic() - started
        self.assertLess(
            elapsed, exit_watchdog.GRACE_SECONDS * 2,
            "waited %.1fs for %d threads on a %.1fs budget"
            % (elapsed, len(threads), exit_watchdog.GRACE_SECONDS))

    def test_describe_survives_a_thread_that_just_ended(self):
        # _survivors() and _current_frames() are separate snapshots, so a
        # thread can finish between them. Reporting must not raise — that
        # would turn a leak warning into a crash on the way out.
        t = self._spawn("ending-worker", daemon=False)
        self.release.set()
        t.join(timeout=5)
        text = exit_watchdog._describe([t])
        self.assertIn("ending-worker", text)


PROBE = r"""
import sys, threading, time
sys.path.insert(0, %(root)r)
from jellyfin_mpv_shim import exit_watchdog

exit_watchdog.GRACE_SECONDS = 0.2

# A non-daemon thread that will not finish: without the watchdog this
# process would sit here until the sleep ends, long past main returning.
threading.Thread(target=lambda: time.sleep(60), name="wedged").start()
print("EXITING", flush=True)
exit_watchdog.finish(0)
print("UNREACHABLE", flush=True)
"""


DEADLINE_PROBE = r"""
import sys, threading, time
sys.path.insert(0, %(root)r)
from jellyfin_mpv_shim import exit_watchdog

# The failure this guards: a shutdown STEP that never returns, so nothing
# after it in the sequence ever runs.
exit_watchdog.arm(deadline=0.5)
print("SHUTTING DOWN", flush=True)
threading.Event().wait(60)          # a stop() that hangs forever
print("UNREACHABLE", flush=True)
"""


class ShutdownDeadlineTest(unittest.TestCase):
    """The deadline has to fire from *inside* a wedged shutdown — the
    original watchdog sat after the sequence and so never ran at all."""

    def test_a_wedged_shutdown_step_is_dumped_and_the_process_exits(self):
        proc = subprocess.run(
            [sys.executable, "-c", DEADLINE_PROBE % {"root": ROOT}],
            capture_output=True, text=True, timeout=30)
        self.assertIn("SHUTTING DOWN", proc.stdout)
        self.assertNotIn("UNREACHABLE", proc.stdout,
                         "the deadline did not end the process")
        self.assertIn("shutdown did not finish", proc.stderr)
        # the dump must cover every thread, main included: when a stop() is
        # wedged, the main thread's frame is the one naming the step
        self.assertIn("Thread 0x", proc.stderr,
                      "no faulthandler dump: %r" % proc.stderr)

    def test_finish_disarms_the_deadline(self):
        probe = (
            "import sys; sys.path.insert(0, %r)\n"
            "from jellyfin_mpv_shim import exit_watchdog\n"
            "exit_watchdog.arm(deadline=0.3)\n"
            "exit_watchdog.finish(0)\n" % ROOT)
        proc = subprocess.run([sys.executable, "-c", probe],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(proc.returncode, 0)
        self.assertNotIn("shutdown did not finish", proc.stderr,
                         "the deadline fired despite a clean finish")


class ForcedExitTest(unittest.TestCase):
    def test_finish_exits_despite_a_wedged_non_daemon_thread(self):
        proc = subprocess.run(
            [sys.executable, "-c", PROBE % {"root": ROOT}],
            capture_output=True, text=True, timeout=30)
        self.assertIn("EXITING", proc.stdout)
        self.assertNotIn("UNREACHABLE", proc.stdout,
                         "finish() returned instead of ending the process")
        self.assertEqual(proc.returncode, 0)
        self.assertIn("did not stop during shutdown", proc.stderr,
                      "the straggler was not reported: %r" % proc.stderr)
        self.assertIn("wedged", proc.stderr,
                      "the report did not name the thread")


if __name__ == "__main__":
    unittest.main()
