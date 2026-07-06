import unittest

# mpv_events has no side effects (no PlayerManager singleton, no mpv launch),
# so it is safe to import directly.
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


if __name__ == "__main__":
    unittest.main()
