"""Methods that must hold the player lock still do.

This exists because of a bug that no behavioural test could see. A helper
was inserted between `@synchronous("_lock")` and the `def` it decorated, so
the decorator silently migrated to the new function and `_play_media` — the
whole of a playback start — ran **unlocked** for eleven commits. Nothing
failed: the suite passed, the app worked, and the damage was a race that
only shows up under timing nobody reproduces on purpose.

`inspect.getsource` on a decorated function includes its decorators, so the
check is cheap and exact. Asserted on `__wrapped__` as well, because a
source check alone would pass on a *commented-out* decorator.

The list is deliberately explicit rather than derived. "Which methods need
the lock" is a design decision, and a test that recomputes it from the code
would agree with whatever the code currently says — which is precisely what
went wrong.
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

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


#: (method, why it matters if the lock is lost).
LOCKED = [
    ("_play_media",
     "run_action's non-blocking fast path is built on _lock being held for "
     "the whole of a playback start; without it a browser Stop runs inline "
     "mid-start and the started video resurrects itself afterwards"),
    ("play_prev",
     "widens the queue backwards with a blocking server call and republishes "
     "queue/seq"),
    ("play_next", "advances the queue"),
    ("stop", "clears _video, which every other path reads"),
    ("set_paused", "routes through SyncPlay or writes mpv's pause"),
    ("seek", "moves the playhead and reports it"),
    ("get_queue", "reads queue and seq together"),
    ("get_queue_ids", "reads the queue while another thread may republish it"),
]


class LockedMethodsTest(unittest.TestCase):

    def test_they_still_carry_the_decorator(self):
        missing = []
        for name, why in LOCKED:
            method = getattr(PlayerManager, name, None)
            if method is None:
                missing.append("%s: no such method" % name)
                continue
            try:
                src = inspect.getsource(method)
            except OSError:                     # pragma: no cover
                continue
            if 'synchronous("_lock")' not in src:
                missing.append("%s lost @synchronous('_lock') — %s"
                               % (name, why))
        self.assertEqual(missing, [])

    def test_the_decorator_is_actually_applied(self):
        """A source check alone passes on a commented-out decorator, and on
        one that has drifted onto the function above."""
        plain = [name for name, _why in LOCKED
                 if getattr(PlayerManager, name, None) is not None
                 and not hasattr(getattr(PlayerManager, name), "__wrapped__")]
        self.assertEqual(
            plain, [],
            "decorated in the source but not at runtime: %r" % (plain,))

    def test_a_helper_cannot_quietly_take_a_decorator(self):
        """The exact shape of the bug: `@synchronous` immediately followed by
        a function whose body does not need it, with the method that does
        need it defined next. Pinned by checking that the two hwdec helpers
        — the ones that caused it — are NOT the decorated pair."""
        for name in ("_forced_hwdec", "_needs_copy_hwdec"):
            method = getattr(PlayerManager, name, None)
            if method is None:
                continue
            self.assertFalse(
                hasattr(method, "__wrapped__"),
                "%s is holding the lock that belongs to _play_media" % name)


if __name__ == "__main__":
    unittest.main()
