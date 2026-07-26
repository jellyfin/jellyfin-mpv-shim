"""The serialized session-report worker.

Two properties matter, and they pull against each other: reports must leave
the playback path (so a track change is not two blocking round trips wide),
but must still arrive in order (the server blanks NowPlayingItem and replaces
PlayState on a stop, so a stop arriving after the following start would leave
every subscribed client showing a dead session).
"""

import threading
import time
import unittest

from jellyfin_mpv_shim.session_reporter import SessionReporter


class OrderingTest(unittest.TestCase):

    def test_reports_run_in_submission_order(self):
        got = []
        r = SessionReporter()
        self.addCleanup(r.stop)
        for i in range(50):
            r.submit(lambda i=i: got.append(i), "n%d" % i)
        self.assertTrue(r.drain(5))
        self.assertEqual(got, list(range(50)))

    def test_a_slow_stop_still_precedes_the_next_start(self):
        # The case this exists for: stop(N) is slow, start(N+1) is queued
        # right behind it. A thread per call would let the start win.
        got = []

        def slow_stop():
            time.sleep(0.2)
            got.append("stop")

        r = SessionReporter()
        self.addCleanup(r.stop)
        r.submit(slow_stop, "session_stop")
        r.submit(lambda: got.append("start"), "session_playing")
        self.assertTrue(r.drain(5))
        self.assertEqual(got, ["stop", "start"])


class NonBlockingTest(unittest.TestCase):

    def test_submit_does_not_wait_for_the_report(self):
        release = threading.Event()
        r = SessionReporter()
        self.addCleanup(lambda: (release.set(), r.stop()))
        r.submit(lambda: release.wait(5), "slow")
        started = time.time()
        r.submit(lambda: None, "second")
        self.assertLess(time.time() - started, 0.5,
                        "submit blocked on the in-flight report")


class ResilienceTest(unittest.TestCase):

    def test_a_failing_report_does_not_kill_the_worker(self):
        # Otherwise one unreachable-server blip silently drops every later
        # report for the rest of the session.
        got = []
        r = SessionReporter()
        self.addCleanup(r.stop)

        def boom():
            raise RuntimeError("server said no")

        r.submit(boom, "session_stop")
        r.submit(lambda: got.append("after"), "session_playing")
        self.assertTrue(r.drain(5))
        self.assertEqual(got, ["after"])

    def test_submit_never_raises_to_the_caller(self):
        r = SessionReporter()
        self.addCleanup(r.stop)
        r.submit(lambda: (_ for _ in ()).throw(RuntimeError()), "boom")
        self.assertTrue(r.drain(5))


class ShutdownTest(unittest.TestCase):

    def test_stop_delivers_what_is_queued(self):
        # The worker is a daemon, so an undrained queue would lose the final
        # stop report and the server would keep the session marked playing.
        got = []
        r = SessionReporter()
        for i in range(10):
            r.submit(lambda i=i: got.append(i), "n%d" % i)
        r.stop()
        self.assertEqual(got, list(range(10)))

    def test_reports_submitted_after_stop_run_inline(self):
        # Dropping them would lose the very report most worth keeping.
        got = []
        r = SessionReporter()
        r.stop()
        r.submit(lambda: got.append("late"), "session_stop")
        self.assertEqual(got, ["late"])

    def test_a_failing_late_report_does_not_raise(self):
        r = SessionReporter()
        r.stop()
        r.submit(lambda: (_ for _ in ()).throw(RuntimeError()), "late")

    def test_drain_reports_failure_rather_than_hanging(self):
        # A wedged server must delay shutdown by the timeout, no more.
        release = threading.Event()
        r = SessionReporter()
        self.addCleanup(lambda: (release.set(), r.stop()))
        r.submit(lambda: release.wait(30), "wedged")
        started = time.time()
        self.assertFalse(r.drain(0.3))
        self.assertLess(time.time() - started, 3)


class LazinessTest(unittest.TestCase):

    def test_no_thread_until_something_is_submitted(self):
        # Constructing a PlayerManager must not spawn threads.
        before = threading.active_count()
        r = SessionReporter()
        self.addCleanup(r.stop)
        self.assertEqual(threading.active_count(), before)

    def test_drain_on_an_unused_reporter_succeeds(self):
        self.assertTrue(SessionReporter().drain(0.1))


if __name__ == "__main__":
    unittest.main()
