"""A kiosk box stays fullscreen when playback stops.

`set_browse_window(True)` runs whenever the in-window UI takes the screen
back — at startup and every time playback ends. It dropped fullscreen unless
`browser_fullscreen` was set, which is the right call for a desktop library
browser and the wrong one for a cast target: headless has no library, so
`browser_fullscreen` is about a screen it never shows, and a TV in a shared
space would fall out of fullscreen the moment a cast finished.

Exercised through the real `set_browse_window` against a fake mpv, because
the whole bug lives in that method's branching -- which #729 then gave a
second caller, so `LiveApplyBrowserFullscreenTest` below runs the same table
through the settings page's entry point.
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
        # Not mid-load. set_browse_window consults this before issuing `stop`,
        # because _video is not set until a start has already succeeded.
        pm._loading = False
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


class LiveApplyBrowserFullscreenTest(unittest.TestCase):
    """#729: the setting has to move the window it is named after, now.

    It used to be read only on a browse TRANSITION, so it took effect after
    the next thing you played -- which from inside Settings is not
    distinguishable from a switch that does nothing.

    The branching itself is `KioskFullscreenTest` above; both entry points
    share `_apply_browse_fullscreen`, and the table below is what says so.
    A second copy of that decision is the failure this guards: it would
    agree on the day it was written and drift on the first change.
    """

    _pm = KioskFullscreenTest._pm
    _settings = KioskFullscreenTest._settings

    def _live(self, video=None, audio=False, **kw):
        from threading import RLock

        self._settings(**kw)
        pm = self._pm(video)
        pm._lock = RLock()          # apply_browser_fullscreen is synchronous
        pm.fullscreen_disable = False
        # Music keeps `_video` set AND keeps the library on screen; a video
        # takes the window. That is the distinction the live apply turns on,
        # so the stand-in has to model it rather than assume one answer.
        pm._current_is_audio = lambda: audio
        return pm

    def test_a_deferred_apply_does_not_fullscreen_a_playing_video(self):
        """The gateway's `_act` DEFERS through `run_action`, so a write made
        while the player lock was held by a playback start lands AFTER that
        start. By then the library does not own the window, and
        `browser_fullscreen` must not be applied to the video that took it
        -- the `_video` guard only ever protected the OFF direction.
        """
        pm = self._live(video=object(), audio=False,
                        browser_fullscreen=True, headless=False)
        pm._player.fs = False           # the video is playing windowed
        pm.apply_browser_fullscreen()
        self.assertFalse(pm._player.fs,
                         "a late browser-fullscreen write took the video "
                         "fullscreen against the user's choice")

    def test_music_still_gets_it_because_the_library_is_on_screen(self):
        """The other side of the same predicate, and why it cannot just be
        `_video is None`: audio keeps `_video` set with the library up."""
        pm = self._live(video=object(), audio=True,
                        browser_fullscreen=True, headless=False)
        pm._player.fs = False
        pm.apply_browser_fullscreen()
        self.assertTrue(pm._player.fs)

    def test_it_agrees_with_a_browse_transition_on_every_branch(self):
        for browser_fs, headless, video, want in (
                (True, False, None, True),
                (False, False, None, False),
                (True, True, None, True),
                (False, True, None, True),      # kiosk: see the class above
                (False, False, object(), True),  # playing: left alone
        ):
            with self.subTest(browser_fullscreen=browser_fs,
                              headless=headless, playing=video is not None):
                pm = self._live(video=video, browser_fullscreen=browser_fs,
                                headless=headless)
                pm._player.fs = True if video is not None else not want
                pm.apply_browser_fullscreen()
                self.assertEqual(pm._player.fs, want)

    def test_toggling_it_on_takes_the_window_without_a_browse_transition(self):
        """The report: it did nothing until the app was started again."""
        pm = self._live(browser_fullscreen=True, headless=False)
        pm._player.fs = False
        pm.apply_browser_fullscreen()
        self.assertTrue(pm._player.fs)

    def test_toggling_it_off_gives_the_window_back(self):
        pm = self._live(browser_fullscreen=False, headless=False)
        pm._player.fs = True
        pm.apply_browser_fullscreen()
        self.assertFalse(pm._player.fs)

    def test_it_does_not_record_a_playback_fullscreen_intent(self):
        """The trap `set_fullscreen` would have walked into: that method
        also writes `fullscreen_disable`, which is read at the NEXT playback
        start -- so turning the library's fullscreen off would have turned
        auto-fullscreen off for videos too, with nothing on screen saying
        so."""
        pm = self._live(browser_fullscreen=False, headless=False)
        pm._player.fs = True
        pm.apply_browser_fullscreen()
        self.assertFalse(pm.fullscreen_disable,
                         "a browse-window change wrote the video's intent")

    def test_a_dead_mpv_is_left_alone(self):
        pm = self._live(browser_fullscreen=True, headless=False)
        pm._mpv_alive = False
        pm._player.fs = False
        pm.apply_browser_fullscreen()
        self.assertFalse(pm._player.fs)


class TheLibrarysFullscreenKeyIsNotTheVideosTest(unittest.TestCase):
    """Item 17 (Izzie, 2026-09-28: "if it disables standalone player
    fullscreen it is a bug"). F in the library saved `browser_fullscreen`
    AND wrote `fullscreen_disable`, the video's auto-fullscreen intent, which
    lasts the session: leave fullscreen in the library once and every film
    after opened windowed, with the video Fullscreen setting on."""

    _pm = KioskFullscreenTest._pm
    _settings = KioskFullscreenTest._settings
    _live = LiveApplyBrowserFullscreenTest._live

    def _toggle_off(self, ui=True, **live):
        from unittest import mock
        pm = self._live(fullscreen=True, browser_fullscreen=True,
                        headless=False, **live)
        pm.mpvtk_active = ui
        pm._player.fs = True
        with mock.patch.object(player_module.settings, "save"):
            pm.set_fullscreen(False, persist=True)
        return pm

    def test_leaving_fullscreen_in_the_library_leaves_films_alone(self):
        pm = self._toggle_off()
        self.assertFalse(player_module.settings.browser_fullscreen,
                         "the premise: the library's own setting was saved")
        self.assertFalse(pm.fullscreen_disable,
                         "the library's F turned off video auto-fullscreen")
        pm.browse_yield()               # the next film takes the window
        self.assertTrue(pm._player.fs, "the next film opened windowed")

    def test_the_same_with_music_playing_under_the_library(self):
        pm = self._toggle_off(video=object(), audio=True)
        self.assertFalse(pm.fullscreen_disable)

    def test_during_a_film_it_is_the_films_choice_as_before(self):
        pm = self._toggle_off(video=object(), audio=False)
        self.assertTrue(pm.fullscreen_disable)

    def test_with_no_library_ui_the_idle_window_is_the_players(self):
        """CLI mode (Izzie, 2026-09-28: "Yes, film's choice there"): with
        no library UI, mpv's idle window between plays is the player, so
        leaving fullscreen there keeps the next film windowed."""
        pm = self._toggle_off(ui=False)
        self.assertTrue(pm.fullscreen_disable)


if __name__ == "__main__":
    unittest.main()
