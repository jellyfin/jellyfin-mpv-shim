"""The idle window must not stay stuck in the display's old colorspace.

Issue #605: play something, turn Windows HDR on mid-playback, stop, then turn
HDR back off — and the library UI comes back washed out with clipped
highlights. mpv only revisits the swapchain colorspace while a video frame
exists (`vo_gpu_next.c`, `if (target_hint && frame->current)`), so the hint it
set for the video is never withdrawn, and an app whose UI *is* the idle mpv
window goes on encoding sRGB artwork as PQ.

The workaround parks `--target-colorspace-hint` at `no` while the browser owns
the window, which takes mpv's other branch and resets the swapchain to sRGB.
What these pin is the half that could hurt: giving the user's own value back
before anything plays. Leaving it parked would cost HDR passthrough, which is
a worse bug than the one being worked around.
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
import threading
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402

HINT = "target_colorspace_hint"


class _Player:
    """Fake mpv that records colorspace-hint writes."""

    def __init__(self, hint="auto"):
        object.__setattr__(self, "writes", [])
        # object.__setattr__: the fake's own construction is not a write the
        # code under test made. hint=None is an mpv without the option at all.
        if hint is not None:
            object.__setattr__(self, HINT, hint)

    def __setattr__(self, name, value):
        if name == HINT:
            self.writes.append(value)
        object.__setattr__(self, name, value)


class _RefusingPlayer(_Player):
    """An mpv that has the option but will not take a write — a disconnect
    mid-transition, which must not be recorded as a successful park."""

    def __setattr__(self, name, value):
        if name == HINT:
            self.writes.append(value)
            raise BrokenPipeError("mpv went away")
        object.__setattr__(self, name, value)


class ColorspaceHintTest(unittest.TestCase):
    def _pm(self, player):
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = player
        pm._mpv_alive = True
        pm._colorspace_hint_suspended = False
        return pm

    def test_browsing_parks_the_hint(self):
        pm = self._pm(_Player())
        pm.suspend_colorspace_hint()
        self.assertEqual(pm._player.writes, ["no"])

    def test_playback_gets_the_user_value_back(self):
        """The half that must never be skipped: a parked hint through a real
        start would mean no HDR passthrough."""
        pm = self._pm(_Player(hint="auto"))
        pm.suspend_colorspace_hint()
        pm.resume_colorspace_hint()
        self.assertEqual(pm._player.writes, ["no", "auto"])

    def test_a_deliberate_user_setting_survives(self):
        """`yes` is someone's HDR setup, not a default to overwrite. The
        restore is of what was read, not of mpv's default."""
        pm = self._pm(_Player(hint=True))
        pm.suspend_colorspace_hint()
        pm.resume_colorspace_hint()
        self.assertEqual(pm._player.writes, ["no", True])

    def test_parking_twice_does_not_lose_the_saved_value(self):
        """set_browse_window(True) runs repeatedly — every stop reaches it
        twice — and the second one must not save "no" as the user's value."""
        pm = self._pm(_Player(hint="auto"))
        pm.suspend_colorspace_hint()
        pm.suspend_colorspace_hint()
        pm.resume_colorspace_hint()
        self.assertEqual(pm._player.writes, ["no", "auto"])

    def test_resume_without_a_park_writes_nothing(self):
        """Every start calls resume; only the ones that follow a park have
        anything to undo."""
        pm = self._pm(_Player())
        pm.resume_colorspace_hint()
        self.assertEqual(pm._player.writes, [])

    def test_an_mpv_without_the_option_is_left_alone(self):
        """Built without gpu-next, or simply too old. The read is how we find
        out, and finding out must not be fatal."""
        pm = self._pm(_Player(hint=None))
        pm.suspend_colorspace_hint()
        pm.resume_colorspace_hint()
        self.assertEqual(pm._player.writes, [])
        self.assertFalse(pm._colorspace_hint_suspended)

    def test_a_failed_park_is_not_recorded_as_parked(self):
        pm = self._pm(_RefusingPlayer())
        pm.suspend_colorspace_hint()
        self.assertFalse(pm._colorspace_hint_suspended)

    def test_a_failed_restore_is_retried_by_the_next_start(self):
        """Clearing the flag on a write that did not land would leave the
        hint parked for the rest of the session."""
        pm = self._pm(_Player())
        pm.suspend_colorspace_hint()
        pm._player.__class__ = _RefusingPlayer
        pm.resume_colorspace_hint()
        self.assertTrue(pm._colorspace_hint_suspended)
        pm._player.__class__ = _Player
        pm.resume_colorspace_hint()
        self.assertFalse(pm._colorspace_hint_suspended)
        self.assertEqual(pm._player.writes[-1], "auto")

    def test_a_dead_mpv_is_not_written_to(self):
        pm = self._pm(_Player())
        pm._mpv_alive = False
        pm.suspend_colorspace_hint()
        self.assertEqual(pm._player.writes, [])


class _InterleavingPlayer(_Player):
    """Forces the #771 interleaving: the first park's write of "no" waits
    until a second thread has read the hint, and that read waits until the
    first park has finished. Every wait is bounded, so a caller that cannot
    get in -- which is the fix -- costs the timeout rather than a hang."""

    WAIT = 0.5

    def __init__(self, hint="auto"):
        super().__init__(hint)
        for name in ("first_wrote", "second_read", "first_done"):
            object.__setattr__(self, name, threading.Event())
        object.__setattr__(self, "reads", 0)

    def __getattribute__(self, name):
        if name != HINT:
            return object.__getattribute__(self, name)
        value = object.__getattribute__(self, name)
        n = object.__getattribute__(self, "reads") + 1
        object.__setattr__(self, "reads", n)
        if n == 2:
            self.second_read.set()
            self.first_done.wait(self.WAIT)
        return value

    def __setattr__(self, name, value):
        super().__setattr__(name, value)
        if name == HINT and len(self.writes) == 1:
            self.first_wrote.set()
            self.second_read.wait(self.WAIT)


class ConcurrentParkTest(unittest.TestCase):
    """#771: a stop parks the hint from two threads at once -- the browser's
    `on_browse_enter` and `stop_to_browser` -- and a second park that reads
    the first one's "no" saves it as the user's value. Every later start then
    restores "no", and HDR passthrough is gone until the shim restarts."""

    def test_two_parks_at_once_keep_the_users_value(self):
        player = _InterleavingPlayer(hint="auto")
        pm = ColorspaceHintTest._pm(None, player)

        def first():
            pm.suspend_colorspace_hint()
            player.first_done.set()

        a = threading.Thread(target=first)
        a.start()
        self.assertTrue(player.first_wrote.wait(5))
        b = threading.Thread(target=pm.suspend_colorspace_hint)
        b.start()
        a.join(5)
        b.join(5)
        self.assertFalse(a.is_alive() or b.is_alive())

        # Several start/stop cycles: a poisoned value would come back from
        # every one of them, not just the first.
        for _ in range(3):
            pm.resume_colorspace_hint()
            self.assertEqual(player.writes[-1], "auto")
            pm.suspend_colorspace_hint()
            self.assertEqual(player.writes[-1], "no")


class BrowseWindowIntegrationTest(unittest.TestCase):
    """Through the real `set_browse_window`, where the park actually happens."""

    class _BrowsePlayer(_Player):
        def __init__(self):
            super().__init__()
            self.fs = None
            self.keepaspect = True
            self.image_display_duration = 0
            self.keep_open = False
            self.commands = []

        def command(self, *a):
            self.commands.append(a)

    def _pm(self, video=None, loading=False):
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = self._BrowsePlayer()
        pm._video = video
        pm._showing_browse_bg = False
        pm._mpv_alive = True
        pm._loading = loading
        pm._colorspace_hint_suspended = False
        pm._set_force_window = lambda *a, **k: None
        return pm

    def test_taking_the_window_parks_the_hint(self):
        pm = self._pm()
        pm.set_browse_window(True)
        self.assertEqual(pm._player.writes, ["no"])

    def test_live_media_keeps_its_hint(self):
        """This runs with media still playing too — music keeps the browser
        up, and the library can be opened over a video. Parking the hint there
        would cost that video its HDR passthrough."""
        pm = self._pm(video=object())
        pm.set_browse_window(True)
        self.assertEqual(pm._player.writes, [])

    def test_a_start_in_flight_is_left_alone(self):
        """_video is not set until a start has succeeded, so mid-load it
        looks like an idle window and is not one."""
        pm = self._pm(loading=True)
        pm.set_browse_window(True)
        self.assertEqual(pm._player.writes, [])


if __name__ == "__main__":
    unittest.main()
