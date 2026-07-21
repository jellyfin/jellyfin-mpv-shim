import unittest
from unittest import mock

# mpv_events has no side effects (no PlayerManager singleton, no mpv launch),
# so it is safe to import directly.
import jellyfin_mpv_shim.mpv_events as mpv_events
from jellyfin_mpv_shim.mpv_events import wait_property


class FakeLibmpv:
    """python-mpv-style backend: observe_property / unobserve_property.

    On registration it synchronously replays a scripted list of property-change
    notifications (mirroring mpv delivering the current value first, then any
    genuine changes), so the tests are deterministic without threads.
    """

    def __init__(self, sample, notifications):
        # getattr(instance, name) reads this during the skip_initial sample.
        self.duration = sample
        self._notifications = list(notifications)
        self.unobserved = False

    def observe_property(self, name, handler):
        for value in self._notifications:
            handler(name, value)

    def unobserve_property(self, name, handler):
        self.unobserved = True


class FakeExtMpv:
    """python-mpv-jsonipc-style backend: bind/unbind_property_observer."""

    def __init__(self, sample, notifications):
        self.duration = sample
        self._notifications = list(notifications)
        self.unbound = False

    def bind_property_observer(self, name, handler):
        for value in self._notifications:
            handler(name, value)
        return 1

    def unbind_property_observer(self, observer_id):
        self.unbound = True


not_none = lambda x: x is not None
SHORT_TIMEOUT = 0.1


class WaitPropertyTest(unittest.TestCase):
    def test_first_value_accepted_without_skip_initial(self):
        # Default behaviour: the first qualifying notification is accepted.
        inst = FakeLibmpv(sample=100, notifications=[100])
        self.assertTrue(
            wait_property(inst, "duration", not_none, timeout=SHORT_TIMEOUT)
        )
        self.assertTrue(inst.unobserved)

    def test_stale_initial_notification_skipped(self):
        # Only the stale current-value notification arrives; with skip_initial
        # it must be ignored, so the wait times out instead of accepting the
        # previous file's duration.
        inst = FakeLibmpv(sample=100, notifications=[100])
        self.assertFalse(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )
        self.assertTrue(inst.unobserved)

    def test_non_initial_change_accepted(self):
        # Stale value re-delivered as the initial notification, then the new
        # file's real duration — the change must be accepted.
        inst = FakeLibmpv(sample=100, notifications=[100, 200])
        self.assertTrue(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )

    def test_first_value_accepted_when_not_ready_at_registration(self):
        # No stale value present (duration is None between files), so nothing is
        # skipped and the first qualifying notification is accepted. Guards the
        # normal first-play / fast-load path against a spurious timeout.
        inst = FakeLibmpv(sample=None, notifications=[300])
        self.assertTrue(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )

    def test_fresh_differing_initial_value_accepted(self):
        # The new file loaded between the sample and the observer firing: the
        # first notification carries a value that differs from the sampled
        # stale one, so it is fresh and must be accepted, not skipped.
        inst = FakeLibmpv(sample=100, notifications=[200])
        self.assertTrue(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )

    def test_timeout_when_no_notification(self):
        inst = FakeLibmpv(sample=None, notifications=[])
        self.assertFalse(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )

    def test_ext_backend_skip_initial(self):
        # The jsonipc backend branch behaves identically and unbinds after.
        inst = FakeExtMpv(sample=100, notifications=[100, 200])
        self.assertTrue(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )
        self.assertTrue(inst.unbound)

    def test_ext_backend_stale_initial_skipped(self):
        inst = FakeExtMpv(sample=100, notifications=[100])
        self.assertFalse(
            wait_property(
                inst, "duration", not_none, timeout=SHORT_TIMEOUT, skip_initial=True
            )
        )
        self.assertTrue(inst.unbound)


class PollingFakeExtMpv:
    """jsonipc-style backend that never delivers property-change events —
    the pipeline-loss case seen in the field on external mpv. Successive
    ``duration`` reads consume ``reads`` (last value repeats)."""

    def __init__(self, reads):
        self._reads = list(reads)
        self.unbound = False

    @property
    def duration(self):
        if len(self._reads) > 1:
            return self._reads.pop(0)
        return self._reads[0]

    def bind_property_observer(self, name, handler):
        return 1

    def unbind_property_observer(self, observer_id):
        self.unbound = True


class WaitPropertyPollFallbackTest(unittest.TestCase):
    """Regression: external-mpv auto-advance died with 'Timeout when waiting
    for media duration' because property-change events were lost and the wait
    relied solely on the observer. The poll fallback must rescue the wait."""

    def setUp(self):
        patcher = mock.patch.object(mpv_events, "POLL_INTERVAL_SECS", 0.01)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_poll_rescues_lost_events(self):
        # Sample sees the old file's duration; no events ever arrive; a later
        # poll sees the new file's duration and must accept it.
        inst = PollingFakeExtMpv(reads=[100, 200])
        self.assertTrue(
            wait_property(
                inst, "duration", not_none, timeout=1, skip_initial=True
            )
        )
        self.assertTrue(inst.unbound)

    def test_poll_rejects_value_equal_to_stale_sample(self):
        # The polled value equals the stale sample — indistinguishable from
        # the pre-change state, so only the observer may accept it. The wait
        # must still time out rather than act on the old file.
        inst = PollingFakeExtMpv(reads=[100, 100])
        self.assertFalse(
            wait_property(
                inst, "duration", not_none, timeout=0.1, skip_initial=True
            )
        )

    def test_poll_accepts_without_skip_initial(self):
        # Without skip_initial there is no stale sample to guard against; the
        # poll may accept any qualifying value.
        inst = PollingFakeExtMpv(reads=[100])
        self.assertTrue(
            wait_property(inst, "duration", not_none, timeout=1)
        )

    def test_poll_rescues_from_none_sample(self):
        # Between files the sample reads None (nothing stale); events are
        # lost; the poll must accept the loaded duration.
        inst = PollingFakeExtMpv(reads=[None, None, 250])
        self.assertTrue(
            wait_property(
                inst, "duration", not_none, timeout=1, skip_initial=True
            )
        )


if __name__ == "__main__":
    unittest.main()


class WaitPropertyAbortTest(unittest.TestCase):
    """A load mpv has already declared dead must not burn the full timeout.

    The abort Event is what turns a failed start from `playback_timeout`
    seconds of frozen UI into a sub-second one.
    """

    def test_abort_already_set_returns_false_quickly(self):
        import threading
        import time

        abort = threading.Event()
        abort.set()
        # Never satisfies cond: without abort this would wait out the timeout.
        player = FakeLibmpv(None, [])
        started = time.time()
        with mock.patch.object(mpv_events, "POLL_INTERVAL_SECS", 0.01):
            result = wait_property(player, "duration", not_none, 5,
                                   abort=abort)
        self.assertFalse(result)
        self.assertLess(time.time() - started, 2,
                        "abort did not cut the wait short")
        self.assertTrue(player.unobserved, "observer leaked on the abort path")

    def test_abort_set_while_waiting_returns_false(self):
        import threading
        import time

        abort = threading.Event()
        threading.Timer(0.05, abort.set).start()
        player = FakeLibmpv(None, [])
        started = time.time()
        with mock.patch.object(mpv_events, "POLL_INTERVAL_SECS", 0.01):
            result = wait_property(player, "duration", not_none, 5,
                                   abort=abort)
        self.assertFalse(result)
        self.assertLess(time.time() - started, 2)

    def test_a_satisfied_wait_still_reports_success_with_abort_armed(self):
        """The abort path must not turn a genuine success into a failure —
        the return value is now a flag, not `event.is_set()`."""
        import threading

        abort = threading.Event()
        player = FakeLibmpv(None, [42])
        self.assertTrue(
            wait_property(player, "duration", not_none, 5, abort=abort))

    def test_abort_is_optional(self):
        """Every existing caller passes no abort; that must be unchanged."""
        player = FakeLibmpv(None, [7])
        self.assertTrue(wait_property(player, "duration", not_none, 5))


class WaitPropertySatisfiedByTest(unittest.TestCase):
    """A stream whose property never arrives must still be able to succeed.

    `duration` is a proxy for "the file is loaded", and an unbounded stream
    (live TV, an open-ended remote origin) never reports one. Without a second
    way to succeed, the wait times out and kills a stream that is playing.
    """

    def test_satisfied_by_succeeds_without_the_property(self):
        import threading
        import time

        loaded = threading.Event()
        loaded.set()
        player = FakeLibmpv(None, [])   # duration never arrives
        started = time.time()
        with mock.patch.object(mpv_events, "POLL_INTERVAL_SECS", 0.01):
            result = wait_property(player, "duration", not_none, 5,
                                   satisfied_by=loaded)
        self.assertTrue(result)
        self.assertLess(time.time() - started, 2)
        self.assertTrue(player.unobserved, "observer leaked on the success path")

    def test_satisfied_by_set_while_waiting(self):
        import threading
        import time

        loaded = threading.Event()
        threading.Timer(0.05, loaded.set).start()
        player = FakeLibmpv(None, [])
        started = time.time()
        with mock.patch.object(mpv_events, "POLL_INTERVAL_SECS", 0.01):
            result = wait_property(player, "duration", not_none, 5,
                                   satisfied_by=loaded)
        self.assertTrue(result)
        self.assertLess(time.time() - started, 2)

    def test_abort_wins_over_satisfied_by(self):
        """mpv can report a file loaded and then immediately fail it; a failed
        load must stay a failure."""
        import threading

        abort = threading.Event()
        loaded = threading.Event()
        abort.set()
        loaded.set()
        player = FakeLibmpv(None, [])
        with mock.patch.object(mpv_events, "POLL_INTERVAL_SECS", 0.01):
            result = wait_property(player, "duration", not_none, 5,
                                   abort=abort, satisfied_by=loaded)
        self.assertFalse(result)

    def test_property_still_wins_when_it_arrives(self):
        """The property stays the fast path for everything that has one."""
        import threading

        loaded = threading.Event()   # never set
        player = FakeLibmpv(None, [42])
        self.assertTrue(
            wait_property(player, "duration", not_none, 5,
                          satisfied_by=loaded))

    def test_satisfied_by_is_optional(self):
        player = FakeLibmpv(None, [7])
        self.assertTrue(wait_property(player, "duration", not_none, 5))
