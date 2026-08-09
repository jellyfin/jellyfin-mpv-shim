"""No lua means no UI at all, so the shim has to notice (#16 / [iw]).

Everything the shim draws is lua: the library browser and the playback HUD
are `renderer.lua`, the stock OSC is lua, `mouse.lua` is lua. An mpv built
without it — or with it broken — leaves the app running and drawing nothing
but video.

And until this existed there was no menu either. `toggle_settings_menu`
refuses the OSD menu whenever the *configured* style is mpvtk, live renderer
or not, so a default install on a no-lua mpv had no surface whatsoever and
no way to reach one.
"""

import sys
import threading
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.player import PlayerManager                # noqa: E402


class _Player:
    """An mpv that either runs the probe script or silently does not.

    Silently is the real behaviour: `load-script` on a script that cannot
    run raises nothing on either backend (measured), which is why the probe
    is a message-or-timeout rather than a try/except.
    """

    def __init__(self, owner, lua=True):
        self.owner, self.lua = owner, lua
        self.loaded = []

    def command(self, *args):
        if args[0] == "load-script":
            self.loaded.append(args[1])
            if self.lua and self.owner._lua_probe is not None:
                self.owner._lua_probe.set()


def _pm(lua=True):
    pm = PlayerManager.__new__(PlayerManager)
    pm._lua_works = None
    pm._lua_probe = None
    pm._player = _Player(pm, lua)
    return pm


class LuaProbeTest(unittest.TestCase):
    def test_a_working_mpv_answers(self):
        pm = _pm(lua=True)
        self.assertTrue(pm.lua_works(timeout=0.2))
        self.assertEqual(len(pm._player.loaded), 1)

    def test_an_mpv_without_lua_times_out(self):
        pm = _pm(lua=False)
        self.assertFalse(pm.lua_works(timeout=0.05))

    def test_the_answer_is_cached(self):
        # Both ways: the failing probe costs the timeout, and paying it once
        # per question would be a two-second stall per caller.
        pm = _pm(lua=False)
        pm.lua_works(timeout=0.05)
        pm.lua_works(timeout=0.05)
        self.assertEqual(len(pm._player.loaded), 1)

    def test_a_raising_load_script_is_not_lua_working(self):
        pm = _pm(lua=False)

        def boom(*_a):
            raise RuntimeError("no scripting")

        pm._player.command = boom
        self.assertFalse(pm.lua_works(timeout=0.05))

    def test_the_probe_slot_is_released(self):
        # Left set, a later stray jms-lua message would fire a dead event.
        pm = _pm(lua=True)
        pm.lua_works(timeout=0.2)
        self.assertIsNone(pm._lua_probe)


class OscStyleOverrideTest(unittest.TestCase):
    """The fallback's other half: there is no OSC to fall back TO, since
    every one of them is lua — so the resolved style becomes "none", which
    is what makes the OSD menu reachable again."""

    def test_it_clears_the_mpvtk_style(self):
        pm = PlayerManager.__new__(PlayerManager)
        pm._osc_style_resolved = "mpvtk"
        pm._osc_script_loaded = True
        pm.set_osc_style("none")
        self.assertEqual(pm._osc_style_resolved, "none")
        self.assertFalse(pm._osc_script_loaded)

    def test_the_osd_menu_is_reachable_afterwards(self):
        """toggle_settings_menu refuses the OSD menu under "mpvtk" alone,
        so this is the whole of what makes a no-lua user able to reach a
        menu."""
        from unittest import mock

        pm = PlayerManager.__new__(PlayerManager)
        pm._osc_style_resolved = "mpvtk"
        pm._osc_script_loaded = True
        pm.do_not_handle_pause = False
        pm._video = object()
        pm.on_hud_menu = None
        pm.menu = mock.Mock(is_menu_shown=False)
        pm.set_osc_style("none")
        pm.toggle_settings_menu()
        pm.menu.show_menu.assert_called_once_with()


class MainFallsBackTest(unittest.TestCase):
    """Checked from the source: constructing main's UI selection needs a
    real mpv and a real server."""

    @staticmethod
    def _source():
        import os

        import jellyfin_mpv_shim

        base = os.path.dirname(os.path.abspath(jellyfin_mpv_shim.__file__))
        with open(os.path.join(base, "mpv_shim.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_main_probes_lua_and_drops_the_gui(self):
        src = self._source()
        self.assertIn("lua_works()", src)
        self.assertIn("use_gui = False", src)
        self.assertIn('set_osc_style("none")', src)

    def test_the_probe_runs_before_the_ui_is_chosen(self):
        # After it, cli_mgr would already have been skipped.
        src = self._source()
        self.assertLess(src.index("lua_works()"),
                        src.index("from .cli_mgr import user_interface"))


if __name__ == "__main__":
    unittest.main()
