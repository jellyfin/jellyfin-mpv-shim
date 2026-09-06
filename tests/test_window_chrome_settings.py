"""The two window-chrome settings from #727.

The report had three parts. One was working as designed and now has an
opt-out, one was a missing setting, and one was the classic OSC drawing its
own buttons where we draw ours.

* **"Only when the window has no title bar" shows nothing in full screen.**
  Deliberate -- `window_controls_wanted` returns False on fullscreen before
  it even reads the mode, because there is no title bar anywhere and nothing
  to move or maximize. `window_controls_fullscreen` is the opt-in for the
  other reading: with no title bar and no keyboard, full screen is a room
  with no door.
* **No way to hide the desktop title bar.** `hide_title_bar` is MPV's
  `border`, and it is set only when ON -- writing `border=yes` otherwise
  would override someone who put `border=no` in their own `mpv.conf` and
  take the top-bar buttons away from exactly the person who wants them,
  since `window_controls` "auto" reads that same property.
* **A classic OSC drew its own window buttons.** Pushed off with
  `osc-windowcontrols=no` beside the idlescreen opt, because the stock OSC
  defaults it to "auto" -- which means "whenever the window has no border",
  i.e. precisely when `hide_title_bar` is on and we are already drawing a
  set.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import pathlib
import sys
import unittest
from unittest import mock

sys.argv = ["test"]

from jellyfin_mpv_shim import mpv_options  # noqa: E402
from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parent.parent


class _FakePlayer:
    def __init__(self, fullscreen=False, border=True):
        self.fullscreen = fullscreen
        self.border = border
        self.window_maximized = False


class _Chrome:
    """`WindowMixin.window_controls_wanted` on a stand-in.

    The mixin is imported rather than a whole PlayerManager built: importing
    `player` opens a real mpv window (CLAUDE.md), and this method only
    touches `settings`, `_player` and `_mpv_alive`.
    """

    def __init__(self, player):
        self._player = player
        self._mpv_alive = True

    @property
    def _CSD_PROPS(self):
        from jellyfin_mpv_shim.player_window import WindowMixin

        return WindowMixin._CSD_PROPS

    def __getattr__(self, name):
        from jellyfin_mpv_shim.player_window import WindowMixin

        attr = getattr(WindowMixin, name)
        return attr.__get__(self, type(self))


def _wanted(fullscreen=False, border=True, **overrides):
    opts = {"window_controls": "always",
            "window_controls_fullscreen": False}
    opts.update(overrides)
    with mock.patch.multiple(settings, **opts):
        return _Chrome(_FakePlayer(fullscreen, border)).window_controls_wanted()


class FullscreenTest(unittest.TestCase):
    def test_full_screen_hides_them_by_default(self):
        self.assertFalse(_wanted(fullscreen=True))

    def test_windowed_still_shows_them(self):
        """The premise: without this the test above passes for the wrong
        reason on any change that turns the buttons off everywhere."""
        self.assertTrue(_wanted(fullscreen=False))

    def test_the_opt_in_keeps_them(self):
        self.assertTrue(_wanted(fullscreen=True,
                                window_controls_fullscreen=True))

    def test_it_does_not_override_never(self):
        """"Never" is the user saying they do not want these at all. A
        full-screen opt-in must not talk them into it -- the same rule the
        `none` OSC style gets."""
        self.assertFalse(_wanted(fullscreen=True, window_controls="never",
                                 window_controls_fullscreen=True))

    def test_nor_does_it_override_auto_on_a_decorated_window(self):
        """With a real title bar, "auto" says no -- and staying in full
        screen does not change what the desktop draws when you leave it."""
        self.assertFalse(_wanted(fullscreen=True, border=True,
                                 window_controls="auto",
                                 window_controls_fullscreen=True))

    def test_but_auto_plus_no_border_says_yes_in_full_screen(self):
        self.assertTrue(_wanted(fullscreen=True, border=False,
                                window_controls="auto",
                                window_controls_fullscreen=True))


class HideTitleBarTest(unittest.TestCase):
    def _border_option(self, **overrides):
        opts = {"hide_title_bar": False, "enable_gui": True,
                "osc_style": "mpvtk"}
        opts.update(overrides)
        with mock.patch.multiple(settings, **opts):
            built = mpv_options.build_mpv_options(
                "mpvtk", [], False, True)
        return built.get("border", "unset")

    def test_on_asks_for_no_border(self):
        self.assertIs(self._border_option(hide_title_bar=True), False)

    def test_off_says_nothing_at_all(self):
        """Not `border=True`. `window_controls` "auto" reads the same
        property to decide whether to draw its own buttons, so writing the
        option unconditionally would override someone who put `border=no`
        in their mpv.conf -- taking the title bar back AND the replacement
        with it."""
        self.assertEqual(self._border_option(hide_title_bar=False), "unset")

    def test_it_needs_a_restart(self):
        """A construction option. The marker is what stops the row claiming
        something happened when nothing did."""
        self.assertIn("hide_title_bar", cfg.RESTART_REQUIRED)

    def test_the_fullscreen_one_does_not(self):
        """It is read on every chrome snapshot, and editing it takes one --
        see `_set_setting`. Marking it would be the lie the RESTART_REQUIRED
        comment warns about."""
        self.assertNotIn("window_controls_fullscreen", cfg.RESTART_REQUIRED)


class LiveApplyTest(unittest.TestCase):
    def test_editing_either_one_re_takes_the_chrome_snapshot(self):
        """The snapshot is PUSHED, on decoration changes only, and editing a
        setting is not one -- so without this the row waits for the next
        fullscreen or maximize to take effect and looks broken. True of
        `window_controls` before this, too."""
        src = ROOT.joinpath("jellyfin_mpv_shim", "mpvtk_browser",
                            "settings", "base.py").read_text(encoding="utf-8")
        self.assertIn('"window_controls", "window_controls_fullscreen"', src)
        self.assertIn("refresh_window_controls", src)


class ClassicOscTest(unittest.TestCase):
    def _init_mpv_body(self):
        src = ROOT.joinpath("jellyfin_mpv_shim", "player.py").read_text(
            encoding="utf-8")
        body = src[src.index("    def _init_mpv(self):"):]
        return body[:body.index("\n    def ")]

    def test_the_options_are_pushed_by_appending(self):
        """One place that knows how to hand an option to a classic OSC, and
        it appends rather than replacing `script-opts` -- which is shared
        with the user's own scripts."""
        body = self._init_mpv_body()
        self.assertIn('"change-list", "script-opts", "append"', body)
        self.assertIn("osc_script_opts(", body)
        self.assertLess(body.index("osc_script_opts("),
                        body.index("_load_classic_osc()"))


class OscScriptOptsTest(unittest.TestCase):
    """WHICH `osc-*` options are pushed, by resolved style.

    This was a flat pair of literals in `_init_mpv`, asserted by reading the
    source -- which is why the question below was never put to it: the text
    said `osc-windowcontrols=no` and every style got it.
    """

    def _opts(self, style):
        return set(mpv_options.osc_script_opts(style))

    def test_idlescreen_is_off_under_every_style(self):
        """It has to cover the styles where mpv loads the OSC itself as well
        as the one we load by hand, and is inert where there is no OSC."""
        for style in ("mpv", "mpvtk", "default", "custom", "none"):
            with self.subTest(style=style):
                self.assertIn("osc-idlescreen=no", self._opts(style))

    def test_only_mpvtk_suppresses_the_osc_s_window_buttons(self):
        """We draw our own over PLAYBACK in exactly one style -- the mpvtk
        HUD asks `window_chrome.window_controls` for a set. Everywhere else
        the OSC on screen is the only thing that could offer any, and the
        stock OSC, our own `trickplay-osc.lua` fork under "mpv", and a
        custom one can all draw them. Telling them not to, with
        `hide_title_bar` on, left a windowed video with no title bar and no
        buttons anywhere.
        """
        self.assertIn("osc-windowcontrols=no", self._opts("mpvtk"))
        for style in ("mpv", "default", "custom", "none"):
            with self.subTest(style=style):
                self.assertNotIn("osc-windowcontrols=no", self._opts(style),
                                 "took the window buttons away from the "
                                 "only thing drawing any")

    def test_nothing_else_is_pushed(self):
        """`window_controls_fullscreen` has no spelling here: the OSC's own
        `window_controls_enabled` reads `windowcontrols` and `border` and
        never asks about full screen, so saying "yes" to carry the opt-in
        would show ITS buttons in full screen even when ours are hidden."""
        for style in ("mpv", "mpvtk", "default", "custom", "none"):
            with self.subTest(style=style):
                self.assertTrue(
                    self._opts(style) <= {"osc-idlescreen=no",
                                          "osc-windowcontrols=no"})


if __name__ == "__main__":
    unittest.main()
