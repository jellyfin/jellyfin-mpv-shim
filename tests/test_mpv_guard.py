"""A property write mpv refuses leaves nothing behind, on either backend.

The stand-ins here are the two libraries' `__setattr__` contracts, because
that contract **is** the field these tests are named after: a stand-in that
merely recorded writes would make the shadow unreachable while reporting a
pass (docs/testing.md section 4). Each one is paired with a control that
drives it *unguarded* and asserts the shadow does appear -- without that, a
stand-in that had quietly stopped modelling the defect would leave every test
below passing for the wrong reason.

The real measurement is a real libmpv, not these: `mpv_guard`'s module
docstring records it, and the refused-write cleanup the harness registers is
what puts the question to the mpv on the box.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import inspect
import sys
import unittest

sys.argv = ["test"]      # the app parses argv on first config-dir resolution

from jellyfin_mpv_shim import mpv_guard  # noqa: E402


class _Unavailable(AttributeError):
    """python-mpv's `PropertyUnavailableError`: an `AttributeError` subclass,
    which is the whole mechanism -- `MPV.__setattr__` catches that base."""


class _LibmpvLike:
    """python-mpv's `MPV.__setattr__`, and the `_set_property` under it.

    Two refusals reach the fallback and they are different errors: a property
    this build does not have raises a plain `AttributeError` (-8,
    `MPV_ERROR_PROPERTY_NOT_FOUND`), and one that exists but has no value
    right now raises `PropertyUnavailableError` (-10). A bad *value* raises
    `TypeError`, which the library does **not** catch.
    """

    def __init__(self, known=(), unavailable=()):
        object.__setattr__(self, "_values", {})
        object.__setattr__(self, "_known", set(known))
        object.__setattr__(self, "_unavailable", set(unavailable))
        object.__setattr__(self, "handle", object())
        # Assigned through __setattr__ on the way up, exactly as the real
        # constructor assigns `osd`, `raw`, `lazy` and `mpv_version_tuple`:
        # a plain Python attribute that only arrives because the property
        # write failed first.
        self.osd = "a proxy object"

    def _set_property(self, name, value):
        if name not in self._known:
            raise AttributeError("mpv property does not exist", -8)
        if name in self._unavailable:
            raise _Unavailable("mpv property is not available", -10)
        if not isinstance(value, (str, int, float, bool, list)):
            raise TypeError(value)
        self._values[name] = value

    def __setattr__(self, name, value):
        try:
            if name != "handle" and not name.startswith("_"):
                self._set_property(name.replace("_", "-"), value)
            else:
                object.__setattr__(self, name, value)
        except AttributeError:
            object.__setattr__(self, name, value)

    def __getattr__(self, name):
        # Only consulted when normal lookup fails, so a shadow in __dict__
        # never reaches mpv again.
        try:
            return self._values[name.replace("_", "-")]
        except KeyError:
            raise AttributeError(name)


class _JsonipcLike:
    """python-mpv-jsonipc's `MPV.__setattr__`.

    No inner raise at all: the name is either in the property list mpv
    reported at connect, or it becomes a Python attribute. So the absence
    check is the refusal, and this backend has only the absent-property half
    of the defect -- a runtime property with no value is still in the list.
    """

    def __init__(self, properties=()):
        object.__setattr__(self, "properties",
                           {n.replace("-", "_") for n in properties})
        object.__setattr__(self, "_values", {})
        # jsonipc keeps assigning these for the life of the object, once per
        # observer and per key binding. Nothing else in this file is about
        # them; `arm` is.
        object.__setattr__(self, "observer_id", 1)

    def bind_property_observer(self, name, callback):
        """The class capability both `mpv_events` and `mpv_guard` dispatch
        on. Present so this stand-in is recognised as the backend it is."""

    def command(self, verb, name=None, value=None):
        if verb == "set_property":
            self._values[name] = value

    def __setattr__(self, name, value):
        if name not in {"properties", "command"} and name in self.properties:
            return self.command("set_property", name.replace("_", "-"), value)
        return object.__setattr__(self, name, value)

    def __getattr__(self, name):
        if name in self.properties:
            return self._values[name.replace("_", "-")]
        return object.__getattribute__(self, name)


def _guarded(base, *args, **kwargs):
    """A guarded player, and the refusal count at the moment it was built.

    The count is on the *class*, so that a re-created mpv still reports to it
    -- which means it also carries the test before this one. Every assertion
    below is therefore a delta, exactly as the suite's own hook is.
    """
    player = mpv_guard.guarded(base)(*args, **kwargs)
    mpv_guard.arm(player)
    return player, mpv_guard.refused_count(player)


class TheLibmpvContractTest(unittest.TestCase):
    def test_unguarded_a_refusal_becomes_an_attribute(self):
        """The control: the stand-in can host the defect."""
        player = _LibmpvLike(known=["playback-time"],
                             unavailable=["playback-time"])

        player.playback_time = 1079

        self.assertIn("playback_time", player.__dict__)
        self.assertEqual(1079, player.playback_time)

    def test_an_unavailable_property_leaves_nothing_behind(self):
        player, mark = _guarded(_LibmpvLike, known=["playback-time"],
                                unavailable=["playback-time"])

        player.playback_time = 1079          # must not raise

        self.assertNotIn("playback_time", player.__dict__)
        self.assertEqual(1, player.failed_writes - mark)
        self.assertEqual("playback_time", player.last_failed_write.name)
        self.assertIsInstance(player.last_failed_write.error, _Unavailable)

    def test_a_property_this_build_does_not_have_leaves_nothing_behind(self):
        """The other error code, and the half jsonipc shares. -8 rather than
        -10, and both are an `AttributeError` to the library."""
        player, mark = _guarded(_LibmpvLike, known=[])

        player.osd_border_style = "outline-and-shadow"

        self.assertNotIn("osd_border_style", player.__dict__)
        self.assertEqual(1, player.failed_writes - mark)

    def test_a_later_read_reaches_mpv(self):
        """What the shadow actually costs: the position for the rest of the
        session. `_check_stalled_finish` reads this."""
        player, _mark = _guarded(_LibmpvLike, known=["playback-time"],
                                 unavailable=["playback-time"])

        player.playback_time = 1079
        player._unavailable.clear()          # the next file loads
        player.playback_time = 3.0

        self.assertEqual(3.0, player.playback_time)

    def test_a_write_that_works_still_works(self):
        player, mark = _guarded(_LibmpvLike, known=["volume", "fullscreen"])

        player.volume = 44
        player.fullscreen = True

        self.assertEqual(44, player.volume)
        self.assertIs(True, player.fullscreen)
        self.assertEqual(0, player.failed_writes - mark)

    def test_a_bad_value_still_raises(self):
        """Not a refusal, and the caller's own `except` is what handles it.
        Swallowing this would hide a real programming error."""
        player, _mark = _guarded(_LibmpvLike, known=["volume"])

        with self.assertRaises(TypeError):
            player.volume = object()

    def test_the_constructors_own_attributes_survive(self):
        """python-mpv reaches `object.__setattr__` for `osd`, `raw`, `lazy`,
        `strict`, `file_local`, `overlays`, `overlay_ids` and
        `mpv_version_tuple` -- through the very fallback this guard replaces.
        Deleting those would break the library, which is why the guard is
        armed only once construction has returned."""
        player, mark = _guarded(_LibmpvLike, known=[])

        self.assertEqual("a proxy object", player.osd)
        self.assertEqual(0, player.failed_writes - mark)


class TheJsonipcContractTest(unittest.TestCase):
    def test_unguarded_a_name_outside_the_property_list_shadows(self):
        """The control. This is the fact that made the item cover both
        backends: the pattern is not libmpv-only."""
        player = _JsonipcLike(properties=["volume"])

        player.osd_border_style = "outline-and-shadow"

        self.assertIn("osd_border_style", player.__dict__)

    def test_a_name_outside_the_property_list_leaves_nothing_behind(self):
        player, mark = _guarded(_JsonipcLike, properties=["volume"])

        player.osd_border_style = "outline-and-shadow"

        self.assertNotIn("osd_border_style", player.__dict__)
        self.assertEqual(1, player.failed_writes - mark)

    def test_a_write_that_works_still_works(self):
        player, mark = _guarded(_JsonipcLike, properties=["volume"])

        player.volume = 44

        self.assertEqual(44, player.volume)
        self.assertEqual(0, player.failed_writes - mark)

    def test_the_librarys_own_counters_keep_working_after_arming(self):
        """jsonipc assigns `observer_id` once per observer and `keybind_id`
        once per key binding, for the life of the object -- so unlike
        python-mpv's, its bookkeeping writes do not all happen before the
        guard is armed. `arm` snapshots what is already on the object for
        exactly this, and without it every observer registration would be
        recorded as a refusal and the counter would never advance."""
        player, mark = _guarded(_JsonipcLike, properties=["volume"])

        for expected in (2, 3, 4):
            player.observer_id += 1
            self.assertEqual(expected, player.observer_id)

        self.assertEqual(0, player.failed_writes - mark)


class TheSameClassIsReusedTest(unittest.TestCase):
    def test_guarded_caches_per_base(self):
        """`_init_mpv` runs again on every re-creation. A fresh class each
        time leaks one per re-open and puts the record where the next
        instance cannot see it."""
        self.assertIs(mpv_guard.guarded(_LibmpvLike),
                      mpv_guard.guarded(_LibmpvLike))
        self.assertIsNot(mpv_guard.guarded(_LibmpvLike),
                         mpv_guard.guarded(_JsonipcLike))

    def test_a_re_created_player_reports_to_the_same_record(self):
        first, _mark = _guarded(_LibmpvLike, known=[])
        first.not_a_property = 1
        second, _second_mark = _guarded(_LibmpvLike, known=[])

        self.assertGreaterEqual(second.failed_writes, first.failed_writes)
        self.assertIn("not_a_property",
                      [r.name for r in mpv_guard.refusals(second)])


class TheWindowCannotGoBackwardsTest(unittest.TestCase):
    """The record is class-level, so a mark only means something against the
    class it came from.

    `_assert_no_refused_writes` takes a mark at build time and asks for the
    window at cleanup. If the player it asks about belongs to a DIFFERENT
    guarded class -- a stand-in rebuilt from a re-imported module, which the
    integration harness does by evicting modules from `sys.modules` -- that
    class's counter starts at zero and the window comes out negative. Reading
    that as "nothing was refused" would report a pass for a case in which
    everything was refused, which is the shape this hook exists to catch.
    """

    def test_a_mark_from_another_class_is_an_error_not_silence(self):
        other, _mark = _guarded(_LibmpvLike, known=[])
        other.not_a_property = 1
        other.nor_this_one = 2
        stale_mark = mpv_guard.refused_count(other)
        fresh, _fresh_mark = _guarded(_JsonipcLike, properties=())

        self.assertLess(mpv_guard.refused_count(fresh), stale_mark,
                        "the two classes share a counter, so this case no "
                        "longer constructs a backwards window")
        with self.assertRaises(AssertionError) as caught:
            mpv_guard.refusals_since(fresh, stale_mark)

        self.assertIn("backwards", str(caught.exception))

    def test_an_empty_window_is_still_empty(self):
        """The control, and the case that must NOT become an error: an
        unguarded stand-in answers 0 to `refused_count` on purpose, so mark
        and count are both zero and the window is legitimately empty."""
        player, mark = _guarded(_LibmpvLike, known=[])

        self.assertEqual((), mpv_guard.refusals_since(player, mark))
        self.assertEqual((), mpv_guard.refusals_since(object(), 0))


class TheContractsModelledHereAreTheRealOnesTest(unittest.TestCase):
    """The stand-ins above are a reading of two libraries, and a reading is
    a belief about someone else's code. These pin the parts the guard
    actually depends on, so the day a library changes shape is a failure
    here rather than a guard that silently stops guarding."""

    def test_python_mpv_still_writes_through_set_property(self):
        try:
            import mpv
        except Exception as error:        # OSError when libmpv is absent
            self.skipTest("python-mpv is not importable here: %s" % error)
        self.assertIsInstance(
            mpv.MPV, type,
            "MPV is no longer a class, so mpv_guard.guarded returns it "
            "unguarded -- and silently, which is the one way this whole "
            "mechanism can stop existing without a failure")
        self.assertTrue(hasattr(mpv.MPV, "_set_property"),
                        "mpv_guard writes through _set_property")
        source = inspect.getsource(mpv.MPV.__setattr__)
        self.assertIn("except AttributeError", source,
                      "python-mpv no longer swallows a refused write, which "
                      "is the premise of this whole guard")
        self.assertTrue(
            issubclass(mpv.PropertyUnavailableError, AttributeError),
            "an unavailable property is no longer an AttributeError, so the "
            "guard would not catch it")

    def test_jsonipc_still_gates_on_its_property_list(self):
        try:
            import python_mpv_jsonipc
        except Exception as error:
            self.skipTest("python-mpv-jsonipc is not importable: %s" % error)
        self.assertIsInstance(python_mpv_jsonipc.MPV, type,
                              "see the libmpv test above")
        self.assertTrue(
            hasattr(python_mpv_jsonipc.MPV, "bind_property_observer"),
            "the discriminator mpv_guard and mpv_events both use")
        source = inspect.getsource(python_mpv_jsonipc.MPV.__setattr__)
        self.assertIn("self.properties", source,
                      "jsonipc no longer decides by its property list")
        self.assertIn("object.__setattr__", source,
                      "jsonipc no longer makes a Python attribute out of a "
                      "name it does not know, which is its half of the "
                      "defect")


if __name__ == "__main__":
    unittest.main()
