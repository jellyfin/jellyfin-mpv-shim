"""The renderer must let go of the mouse when a classic OSC owns the window.

**Question B of two** (#724). `tools/probe_classic_osc_mouse.py` answers A
against a real mpv and this answers B against the fakes; neither is much use
alone.

The mechanism, established against mpv's source and then measured: the
renderer's `mpvtk_mouse` section claims mbtn_left/mbtn_right and sets no
mouse area, so mpv treats it as covering the whole screen
(`input.c: get_bind_section` -> `{INT_MIN..INT_MAX}`) and prefers it over the
builtin binding (`get_cmd_from_keys`). Both of the renderer's bare-video
fall-throughs gate on `state.phud.mode`, which classic-OSC modality never
enters -- so while that section is live, both buttons are swallowed and do
nothing at all.

**A said the renderer is fine.** Told `mpvtk-active no`, it drops
`mpvtk_mouse`, `mpvtk_thumb` and `mpvtk_wheel` to priority -1 and mpv's own
default section wins every mouse key. So the whole question is whether the
app SENDS it -- which is what these pin, on the transitions a video actually
goes through rather than on the one method that looks like the handoff.

The trap those transitions carry: `_start` clears `_browsing` before playback
reports in, so `on_playstate`'s `if self._browsing: self._yield()` cannot
fire for video. The real handoff is `LoadFeedback.clear()`, and it returns
early when `starting` is None. Anything that reaches playback without going
through `begin()` therefore never yields, and the renderer keeps the mouse
over a classic OSC.
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

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, StubHudApp, _SyncPool)
from tests._shell_harness import HudController as _HudModeController  # noqa: E402


class _ClassicOscController(FakeController):
    """A controller in classic-OSC modality.

    `use_hud()` is what `hud.available()` asks, and it is False for every
    `osc_style` but "mpvtk". Spelled out rather than left to
    `FakeController.__getattr__`, which would record the call and hand back
    None -- falsy, so the tests below would pass, but by accident and
    identically for both modalities. The mpvtk control beside it is the
    harness's own `HudController`, which answers True.
    """

    def use_hud(self):
        return False


def _browser(classic=True):
    app = StubHudApp()
    ctl = _ClassicOscController() if classic else _HudModeController()
    b = MpvtkBrowser(app=app, source=FakeSource(), controller=ctl)
    b._pool = _SyncPool()
    b.server = "srv1"
    b.navigate({"kind": "home", "server": "srv1"})
    b._browsing = True
    del app.calls[:]
    return b, app


def _active_calls(app):
    return [value for name, value in app.calls if name == "active"]


class HandoffTest(unittest.TestCase):
    def test_the_hud_is_not_available_in_classic_modality(self):
        """The premise the rest of this file rests on. If this ever answers
        True the branch under test is unreachable and everything below
        passes vacuously."""
        b, _app = _browser()
        self.assertFalse(b.hud.available())

    def test_and_it_IS_available_in_mpvtk_modality(self):
        """The other half of that premise, and the one that matters: with
        both fakes answering falsy, every test here would pass while
        measuring nothing. That is what `test_the_mpvtk_hud_still_keeps_it`
        caught on this file's first run."""
        b, _app = _browser(classic=False)
        self.assertTrue(b.hud.available())

    def test_yielding_to_video_releases_the_renderer(self):
        b, app = _browser()
        b._yield()
        self.assertIn(False, _active_calls(app),
                      "the renderer kept the mouse over a classic OSC")

    def test_it_does_not_engage_the_hud_instead(self):
        """The mpvtk branch keeps the renderer attached-but-idle, which
        keeps `mpvtk_mouse` live. That is right for the in-window HUD and
        wrong for every other osc_style."""
        b, app = _browser()
        b._yield()
        self.assertEqual([c for c in app.calls if c[0] == "hud"], [])

    def test_the_mpvtk_hud_still_keeps_it(self):
        """The other side of the same switch, so this file cannot pass by
        releasing the mouse in both modes."""
        b, app = _browser(classic=False)
        b._yield()
        self.assertNotIn(False, _active_calls(app))
        self.assertIn(("hud", True), app.calls)


class StartToPlaybackTest(unittest.TestCase):
    """The transition a video actually takes, not the method named after it.

    `_start` clears `_browsing` so the loading spinner can draw, which makes
    `on_playstate`'s `if self._browsing: self._yield()` dead for video. The
    handoff really happens in `LoadFeedback.clear()`.
    """

    def _play_a_video(self, b):
        b._start(audio=False, title="A Film")
        b.load.clear()

    def test_a_normal_video_start_releases_the_mouse(self):
        b, app = _browser()
        self._play_a_video(b)
        self.assertIn(False, _active_calls(app))

    def test_the_spinner_holds_the_renderer_until_playback_reports(self):
        """Between `_start` and `clear` the browser is still drawing -- the
        loading screen is its own scene -- so releasing there would throw it
        away. This is why the yield is deferred, and why the deferral is
        the thing that can be missed."""
        b, app = _browser()
        b._start(audio=False, title="A Film")
        self.assertNotIn(False, _active_calls(app))

    def test_audio_keeps_the_window_and_the_mouse(self):
        """Music plays with the library still up, so the renderer must NOT
        let go -- the now-playing bar is a scene and needs its clicks."""
        b, app = _browser()
        b._start(audio=True, title="A Song")
        b.load.clear()
        self.assertNotIn(False, _active_calls(app))

    def test_three_videos_in_a_row_each_release_it(self):
        """State feeding back: every start after the first runs against
        whatever the previous one left in `LoadFeedback`, and `clear()`
        returns early when `starting` is None -- so a start that failed to
        arm it would silently stop yielding from then on."""
        b, app = _browser()
        for round_no in range(1, 4):
            with self.subTest(round=round_no):
                del app.calls[:]
                b.enter_browse()
                self._play_a_video(b)
                self.assertIn(False, _active_calls(app),
                              "video %d kept the mouse" % round_no)

    def test_coming_back_from_playback_takes_it_again(self):
        """...and the browser needs the mouse back, or the library is dead
        to the pointer after the first film."""
        b, app = _browser()
        self._play_a_video(b)
        del app.calls[:]
        b.enter_browse()
        self.assertIn(True, _active_calls(app))


if __name__ == "__main__":
    unittest.main()
