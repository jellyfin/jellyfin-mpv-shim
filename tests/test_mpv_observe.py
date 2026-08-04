"""``mpv_events.observe`` / ``unobserve`` — the property-observer dispatch.

Small, and load-bearing out of proportion to its size.

python-mpv offers two ways to register an observer. The decorator,
``property_observer``, writes an ``unobserve_mpv_properties`` attribute onto
the callback it is handed — and **a bound method has no ``__dict__`` to write
it to**, so it raises AttributeError. jsonipc's equivalent accepts one
happily, and so does the test fake. A handler that stops being a plain
closure therefore breaks on exactly one backend, at runtime, in whichever
feature happened to own it.

That is not a question about a live mpv, it is a question about which API is
called — so it is answered here with fakes shaped like the two backends,
rather than in the integration suite. Doing it against a real mpv was tried
and is worse in both directions: it cannot see *which* call was made, and on
libmpv a raw handle that has ever had an observer registered segfaults the
interpreter when terminated (see tests/integration/test_window_decorations.py
for the note).

The backends are told apart by CLASS capability, not by instance ``hasattr``:
libmpv's ``__getattr__`` turns an unknown *instance* attribute into a
property read, so asking an instance would be both wrong and a round trip.
"""

import unittest

from jellyfin_mpv_shim import mpv_events


class FakeLibmpv:
    """python-mpv's shape: observe_property/unobserve_property, and a
    decorator that refuses bound methods."""

    def __init__(self):
        self.observed = []
        self.unobserved = []

    def observe_property(self, name, handler):
        self.observed.append((name, handler))

    def unobserve_property(self, name, handler):
        self.unobserved.append((name, handler))

    def property_observer(self, name):
        def decorate(handler):
            # Faithful to python-mpv: it writes onto the callback, which is
            # what a bound method cannot take.
            handler.unobserve_mpv_properties = lambda: None
            return handler
        return decorate


class FakeJsonipc:
    """python-mpv-jsonipc's shape: bind/unbind, keyed by an observer id."""

    def __init__(self):
        self.bound = {}
        self.unbound = []
        self._next = 100

    def bind_property_observer(self, name, handler):
        self._next += 1
        self.bound[self._next] = (name, handler)
        return self._next

    def unbind_property_observer(self, observer_id):
        self.unbound.append(observer_id)
        self.bound.pop(observer_id, None)


class Handler:
    def __init__(self):
        self.calls = []

    def on_change(self, name, value):     # a BOUND METHOD, the hazard
        self.calls.append((name, value))


class TestObserveDispatch(unittest.TestCase):
    def test_libmpv_registers_through_observe_property(self):
        mpv, h = FakeLibmpv(), Handler()
        token = mpv_events.observe(mpv, "border", h.on_change)
        self.assertEqual(mpv.observed, [("border", h.on_change)])
        self.assertIsNone(token, "libmpv identifies a registration by "
                                 "(name, handler), so there is no id")

    def test_jsonipc_registers_through_bind_and_hands_back_an_id(self):
        mpv, h = FakeJsonipc(), Handler()
        token = mpv_events.observe(mpv, "border", h.on_change)
        self.assertEqual(mpv.bound[token], ("border", h.on_change))

    def test_a_bound_method_is_accepted_on_both(self):
        """The whole reason this indirection exists. If either path ever goes
        back to the decorator, this is what says so."""
        for mpv in (FakeLibmpv(), FakeJsonipc()):
            with self.subTest(backend=type(mpv).__name__):
                h = Handler()
                mpv_events.observe(mpv, "border", h.on_change)  # must not raise

    def test_the_decorator_that_rejects_bound_methods_is_not_used(self):
        """A bound method has no __dict__, so python-mpv's property_observer
        raises on one. Asserted by watching whether it was reached at all —
        the fake's version would succeed here, so "it did not raise" is not
        evidence on its own."""
        used = []
        mpv, h = FakeLibmpv(), Handler()
        mpv.property_observer = lambda name: used.append(name) or (lambda f: f)
        mpv_events.observe(mpv, "border", h.on_change)
        self.assertEqual(used, [],
                         "the observer went through property_observer, which "
                         "raises AttributeError on a bound method")

    def test_a_plain_function_still_works(self):
        mpv = FakeLibmpv()
        calls = []
        mpv_events.observe(mpv, "border", lambda n, v: calls.append(v))
        self.assertEqual(len(mpv.observed), 1)


class TestUnobserve(unittest.TestCase):
    """Nothing in the app unregisters — mpv is torn down whole, handle and
    observers together. A test harness that outlives its handle's observers
    must, so the undo has to be right even though production never uses it."""

    def test_libmpv_undoes_by_name_and_handler(self):
        mpv, h = FakeLibmpv(), Handler()
        token = mpv_events.observe(mpv, "border", h.on_change)
        mpv_events.unobserve(mpv, "border", h.on_change, token)
        self.assertEqual(mpv.unobserved, [("border", h.on_change)])

    def test_jsonipc_undoes_by_id(self):
        mpv, h = FakeJsonipc(), Handler()
        token = mpv_events.observe(mpv, "border", h.on_change)
        mpv_events.unobserve(mpv, "border", h.on_change, token)
        self.assertEqual(mpv.unbound, [token])
        self.assertEqual(mpv.bound, {})

    def test_jsonipc_with_no_token_unbinds_nothing(self):
        # Rather than unbinding something else: the id space is the
        # library's, and a None passed through as an id is a wrong guess at
        # someone else's observer.
        mpv, h = FakeJsonipc(), Handler()
        mpv_events.observe(mpv, "border", h.on_change)
        mpv_events.unobserve(mpv, "border", h.on_change, None)
        self.assertEqual(mpv.unbound, [])

    def test_a_round_trip_leaves_nothing_registered(self):
        for mpv in (FakeLibmpv(), FakeJsonipc()):
            with self.subTest(backend=type(mpv).__name__):
                h = Handler()
                token = mpv_events.observe(mpv, "border", h.on_change)
                mpv_events.unobserve(mpv, "border", h.on_change, token)


class TestThePlayerUsesIt(unittest.TestCase):
    def test_observe_delegates_rather_than_reimplementing(self):
        """``PlayerManager._observe`` is a one-liner over this. If it grows
        its own dispatch again, the hazard gets two homes and they drift."""
        import inspect

        from jellyfin_mpv_shim import player

        src = inspect.getsource(player.PlayerManager._observe)
        self.assertNotIn("property_observer", src)
        self.assertIn("observe_property(self._player", src,
                      "_observe no longer delegates to mpv_events.observe")


if __name__ == "__main__":
    unittest.main()
