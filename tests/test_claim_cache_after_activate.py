"""Re-activating the renderer must not claim it dropped what it still holds.

`claim_keys` and `set_picture_pan` are compare-and-skip caches, and
`set_active` forgets them because **the renderer drops its key claim and its
pan model on its own when it goes inactive and never says so**. That is true
of `mpvtk-active no`. It is not true of `yes`: the handler there drops
nothing, so recording "we have pushed the empty set" is a statement about the
renderer that is false, and the next genuine release compares equal and is
skipped.

Reported, and the sequence matters:

    play music -> STOP it -> start a video  =>  SPACE and m dead

Music claims 9/0/m/SPACE, because `browse_block_keys` swallows them
otherwise. Stopping routes through `enter_browse()` -> `set_active(True)`,
which zeroed the cache while the renderer kept the keys; the release that
followed was skipped, and the keys stayed force-bound with nothing driving
them. A forced binding outranks the player's own, so it is not a missing
feature -- it is pause and mute dead for every video after. `p` keeps
working throughout, because `p` is never claimed, and that asymmetry is what
identified it.

Going straight from music to a video does NOT reproduce it: no
`set_active(True)` runs, so the cache still holds the real value and the
release goes out. The stop is the whole of the difference.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = [sys.argv[0]]

MUSIC_KEYS = ("9", "0", "m", "SPACE")


class _Backend:
    def __init__(self):
        self.sent = []

    def command(self, *args):
        self.sent.append(args)

    def claims(self):
        import json
        out = []
        for args in self.sent:
            if len(args) >= 3 and args[1] == "mpvtk-keys":
                out.append(tuple(json.loads(args[2])["keys"]))
        return out


class ClaimSurvivesReactivationTest(unittest.TestCase):

    def _app(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp

        app = MpvtkApp.__new__(MpvtkApp)
        app.backend = _Backend()
        import threading
        app._pan_lock = threading.RLock()
        app._claimed_keys = ()
        app._picture_pan = None
        return app

    def test_a_release_after_reactivating_reaches_the_renderer(self):
        app = self._app()
        app.claim_keys(MUSIC_KEYS)          # music: the keys are claimed
        app.set_active(True)                # stopping music re-enters browse
        app.claim_keys(())                  # the build that should release
        self.assertEqual(
            [MUSIC_KEYS, ()], app.backend.claims(),
            "the release was skipped, so SPACE and m stay force-bound and "
            "the next video cannot be paused or muted")

    def test_the_straight_path_still_releases(self):
        """The control, and the reason this went unnoticed: music straight to
        a video never re-activates, so the cache held the real value and the
        release went out."""
        app = self._app()
        app.claim_keys(MUSIC_KEYS)
        app.claim_keys(())
        self.assertEqual([MUSIC_KEYS, ()], app.backend.claims())

    def test_an_unchanged_claim_is_still_skipped(self):
        """The cache still has to do its job: this is pushed from build(),
        i.e. every frame."""
        app = self._app()
        app.claim_keys(MUSIC_KEYS)
        app.claim_keys(MUSIC_KEYS)
        self.assertEqual([MUSIC_KEYS], app.backend.claims())

    def test_stopping_a_pan_after_reactivating_reaches_the_renderer(self):
        """The same cache, the same fix: `None` is a real request here --
        "stop panning" -- so the sentinel cannot be None."""
        app = self._app()
        app.set_picture_pan({"unitx": 1, "unity": 1})
        app.set_active(True)
        app.set_picture_pan(None)
        pans = [a for a in app.backend.sent
                if len(a) >= 2 and a[1] == "mpvtk-vpan"]
        self.assertEqual(
            2, len(pans),
            "the pan model was left set, so the wheel keeps panning against "
            "a clamp for a picture that is gone")


if __name__ == "__main__":
    unittest.main()
