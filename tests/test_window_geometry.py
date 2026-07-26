"""The window must not jump when playback starts.

`geometry` reads like a stored value but every VO treats a runtime write as
a resize command: w32_common and x11_common un-maximize the window and force
a reset to the recomputed size, wayland_common calls set_geometry(resize).
So clearing it before each load — which is what the code used to do, to stop
X11 re-applying a stale size on VO reconfig — meant mpv recomputed the size
from whatever it had: the video's native size, or 960x540 while the browser
is idle, that being the dummy size mpv gives a forced window. A maximized
window un-maximized itself and shrank to fit the video, and playing from an
empty browser landed on mpv's default size.

The fix is to arm the *live* size instead of clearing, so X11's re-apply is
a no-op and no VO is ever told to resize. These pin that: what must not be
written, and when.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim import player as player_module  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class _Player:
    """Fake mpv that records every geometry write."""

    def __init__(self, w=1280, h=720, maximized=False, fullscreen=False):
        object.__setattr__(self, "geometry_writes", [])
        object.__setattr__(self, "event_log", [])
        self.osd_width = w
        self.osd_height = h
        self.window_maximized = maximized
        self.fullscreen = fullscreen
        self.force_window = True

    def __setattr__(self, name, value):
        if name == "geometry":
            self.geometry_writes.append(value)
            self.event_log.append(("geometry", value))
        elif name == "force_window":
            self.event_log.append(("force_window", value))
        object.__setattr__(self, name, value)


class _GeometryTest(unittest.TestCase):
    def _pm(self, armed="1280x720", **kw):
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = _Player(**kw)
        del pm._player.event_log[:]   # drop the fake's own construction
        pm._geometry_armed = armed
        pm.mpvtk_active = True
        return pm


class SyncOnPlaybackTest(_GeometryTest):
    """_sync_window_geometry is what runs on every load."""

    def test_geometry_is_never_cleared(self):
        """The clear is the bug: an empty geometry makes the VO recompute
        the window from the video (or mpv's 960x540 idle dummy)."""
        pm = self._pm(armed=None)
        pm._sync_window_geometry()
        self.assertNotIn("", pm._player.geometry_writes)

    def test_an_unchanged_size_is_not_rewritten(self):
        """Playing from the idle browser must not touch the window at all —
        even a same-size write un-maximizes on Windows and X11."""
        pm = self._pm(armed="1280x720", w=1280, h=720)
        pm._sync_window_geometry()
        self.assertEqual(pm._player.geometry_writes, [])

    def test_a_resized_window_re_arms_to_its_live_size(self):
        """X11 re-applies geometry on VO reconfig; the armed value has to be
        the size the user left, or the next file snaps the window back."""
        pm = self._pm(armed="1280x720", w=1600, h=900)
        pm._sync_window_geometry()
        self.assertEqual(pm._player.geometry_writes, ["1600x900"])
        self.assertEqual(pm._geometry_armed, "1600x900")

    def test_a_maximized_window_is_left_alone(self):
        """The reported bug: a maximized window un-maximized itself and fit
        to the video. Writing geometry is what does that, so don't."""
        pm = self._pm(armed="1280x720", w=3840, h=2160, maximized=True)
        pm._sync_window_geometry()
        self.assertEqual(pm._player.geometry_writes, [])
        self.assertEqual(pm._geometry_armed, "1280x720",
                         "the maximized size is not a floating size")

    def test_a_fullscreen_window_is_left_alone(self):
        pm = self._pm(armed="1280x720", w=3840, h=2160, fullscreen=True)
        pm._sync_window_geometry()
        self.assertEqual(pm._player.geometry_writes, [])

    def test_a_torn_down_window_does_not_clobber_the_armed_size(self):
        """osd-width reads 0 while the window is going away."""
        pm = self._pm(armed="1280x720", w=0, h=0)
        pm._sync_window_geometry()
        self.assertEqual(pm._player.geometry_writes, [])
        self.assertEqual(pm._geometry_armed, "1280x720")

    def test_an_unreadable_property_is_survivable(self):
        pm = self._pm()

        class _Dead:
            def __getattr__(self, name):
                raise OSError("mpv is gone")

            def __setattr__(self, name, value):
                raise OSError("mpv is gone")

        pm._player = _Dead()
        pm._sync_window_geometry()   # must not raise on the load path


class MinimizeOrderTest(_GeometryTest):
    """Releasing force_window destroys the window; the re-arm rides after."""

    def _minimized(self, **kw):
        pm = self._pm(**kw)
        pm.mpvtk_active = False      # what lets force_window=False through
        pm._save_window_geometry = lambda: None
        pm._set_force_window(False)
        return pm

    def test_the_geometry_write_lands_after_the_window_is_gone(self):
        """A write while the window lives clears mpv's own window-maximized
        option, which is the flag that re-maximizes it on the way back."""
        pm = self._minimized(armed="1280x720", w=1600, h=900)
        kinds = [name for name, _v in pm._player.event_log]
        self.assertEqual(kinds, ["force_window", "geometry"])

    def test_the_size_is_read_before_the_window_is_destroyed(self):
        pm = self._minimized(armed="1280x720", w=1600, h=900)
        self.assertEqual(pm._player.geometry_writes, ["1600x900"])

    def test_a_maximized_window_re_opens_at_the_configured_size(self):
        """Nothing to read while maximized, so fall back to settings rather
        than storing full-screen-ish dimensions."""
        for key, value in (("window_width", 1024), ("window_height", 768)):
            real = getattr(player_module.settings, key)
            self.addCleanup(
                lambda k=key, v=real: setattr(player_module.settings, k, v))
            setattr(player_module.settings, key, value)
        pm = self._minimized(armed="1280x720", w=3840, h=2160, maximized=True)
        self.assertEqual(pm._player.geometry_writes, ["1024x768"])


class StartupTest(unittest.TestCase):
    """What mpv is actually constructed with at startup."""

    def _options(self):
        from jellyfin_mpv_shim.mpv_options import build_mpv_options

        return build_mpv_options("default", [], False, False)

    def test_auto_window_resize_is_disabled(self):
        """The other half of the fix: without it mpv resizes the window to
        each video's native size on reconfig, whatever geometry says."""
        self.assertIs(self._options()["auto_window_resize"], False)

    def test_a_geometry_is_always_armed(self):
        """_rearm_window_geometry compares against the armed value to skip
        redundant writes, so startup has to set one."""
        self.assertRegex(self._options()["geometry"], r"^\d+x\d+$")

    def test_the_armed_value_is_tracked_from_startup(self):
        """_geometry_armed must be seeded from the option actually passed;
        starting it out wrong would let a redundant resize through."""
        import inspect

        self.assertIn('self._geometry_armed = mpv_options["geometry"]',
                      inspect.getsource(PlayerManager))


class WindowMixinIsWiredInTest(unittest.TestCase):
    """Window ownership, geometry and fullscreen live in ``WindowMixin``.

    The tests above drive them through PlayerManager, so they would all still
    pass if the inheritance went away and the window handling went with it.
    """

    def test_the_player_inherits_it_and_the_methods_resolve_to_it(self):
        from jellyfin_mpv_shim.player_window import WindowMixin

        self.assertTrue(issubclass(PlayerManager, WindowMixin))
        for name in ("set_browse_window", "browse_yield", "force_window",
                     "raise_window", "set_fullscreen", "toggle_fullscreen",
                     "_sync_window_geometry", "_rearm_window_geometry",
                     "_save_window_geometry", "_set_force_window",
                     "_reopen_window_size"):
            self.assertIs(getattr(PlayerManager, name),
                          getattr(WindowMixin, name),
                          "%s is no longer the mixin's" % name)

    def test_fullscreen_keeps_the_transport_lock(self):
        """The odd one out: everything else here is unsynchronized, but
        fullscreen has always run under _lock and callers rely on it."""
        import inspect

        from jellyfin_mpv_shim.player_window import WindowMixin

        for name in ("set_fullscreen", "toggle_fullscreen"):
            self.assertIn('synchronous("_lock")',
                          inspect.getsource(getattr(WindowMixin, name)),
                          "%s lost its lock" % name)

    def test_importing_it_pulls_in_neither_player_nor_a_backend(self):
        # Same guard as player_audio / player_reporting: _mpv_errors and
        # win_utils are read per call so the integration harness's fake-mpv
        # swap keeps working.
        import subprocess
        import sys

        code = ("import sys; sys.argv=[sys.argv[0]];"
                "import jellyfin_mpv_shim.player_window;"
                "bad=[m for m in ('jellyfin_mpv_shim.player','mpv',"
                "'python_mpv_jsonipc') if m in sys.modules];"
                "print(','.join(bad))")
        out = subprocess.run([sys.executable, "-c", code],
                             capture_output=True, text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(out.stdout.strip(), "",
                         "player_window captured the backend at import time")


if __name__ == "__main__":
    unittest.main()
