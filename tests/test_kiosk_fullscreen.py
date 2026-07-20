"""A kiosk box stays fullscreen when playback stops.

`set_browse_window(True)` runs whenever the in-window UI takes the screen
back — at startup and every time playback ends. It dropped fullscreen unless
`browser_fullscreen` was set, which is the right call for a desktop library
browser and the wrong one for a cast target: headless has no library, so
`browser_fullscreen` is about a screen it never shows, and a TV in a shared
space would fall out of fullscreen the moment a cast finished.

Exercised through the real `set_browse_window` against a fake mpv, because
the whole bug lives in that method's branching.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import player as player_module  # noqa: E402
from jellyfin_mpv_shim.player import PlayerManager  # noqa: E402


class _Player:
    """Records the properties set_browse_window writes."""

    def __init__(self):
        self.fs = None
        self.keepaspect = True
        self.image_display_duration = 0
        self.keep_open = False
        self.commands = []

    def command(self, *a):
        self.commands.append(a)

    def __setattr__(self, name, value):
        object.__setattr__(self, name, value)


class KioskFullscreenTest(unittest.TestCase):
    def _pm(self, video=None):
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = _Player()
        pm._video = video
        pm._showing_browse_bg = False
        pm._mpv_alive = True
        pm._set_force_window = lambda *a, **k: None
        return pm

    def _settings(self, **kw):
        """Patch just the keys under test, restoring the rest."""
        for key, value in kw.items():
            real = getattr(player_module.settings, key)
            self.addCleanup(
                lambda k=key, v=real: setattr(player_module.settings, k, v))
            setattr(player_module.settings, key, value)

    def test_kiosk_stays_fullscreen_with_browser_fullscreen_off(self):
        """The reported behaviour: stopping playback dropped out of
        fullscreen on a cast-target box."""
        self._settings(browser_fullscreen=False, headless=True)
        pm = self._pm()
        pm.set_browse_window(True)
        self.assertTrue(pm._player.fs,
                        "a kiosk left fullscreen when playback stopped")

    def test_a_normal_browser_still_leaves_fullscreen(self):
        """Unchanged for everyone else — browsing is a desktop activity."""
        self._settings(browser_fullscreen=False, headless=False)
        pm = self._pm()
        pm.set_browse_window(True)
        self.assertFalse(pm._player.fs)

    def test_browser_fullscreen_still_wins_when_set(self):
        self._settings(browser_fullscreen=True, headless=False)
        pm = self._pm()
        pm.set_browse_window(True)
        self.assertTrue(pm._player.fs)

    def test_both_set_is_fullscreen(self):
        self._settings(browser_fullscreen=True, headless=True)
        pm = self._pm()
        pm.set_browse_window(True)
        self.assertTrue(pm._player.fs)

    def test_it_does_not_yank_fullscreen_away_from_a_playing_video(self):
        """The `not self._video` guard: a video owns the fullscreen state,
        and set_browse_window must not fight it."""
        self._settings(browser_fullscreen=False, headless=False)
        pm = self._pm(video=object())
        pm._player.fs = True
        pm.set_browse_window(True)
        self.assertTrue(pm._player.fs)


if __name__ == "__main__":
    unittest.main()
