"""A fallback OSC style has to outlive the mpv it was decided on.

`mpv_shim.main` asks `lua_works()` once, at startup, and on a no-lua build
calls `set_osc_style("none")` so the OSD menu becomes reachable --
`toggle_settings_menu` refuses it while the resolved style is "mpvtk".

But `_init_mpv` re-runs on every mpv re-creation (idle-quit then a cast,
`set_browse_window`, `force_window`) and re-resolves the style from
settings, which know nothing about the machine having no lua. So the style
went back to "mpvtk" with no renderer behind it and `on_hud_menu` still
None: no HUD, no OSD menu, and no way to reach either. One idle timeout
undid the whole fallback.

This is the shape CLAUDE.md warns about -- state feeding back into the
input that produced it -- so it is asserted over repeated re-inits, not
one. A single-step test passes against the broken code.
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
from unittest import mock

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.player import PlayerManager        # noqa: E402


class _Fake(PlayerManager):
    """A PlayerManager with only the override state set.

    Deliberately NOT a re-implementation of the decision: it calls the real
    `_effective_osc_style`. The first version of this test carried its own
    copy of the logic in a stand-in and a mutation of the production code
    sailed straight through it -- the stand-in agreed with the test, which
    is the trap CLAUDE.md names.
    """

    def __init__(self):
        self._lua_works = None
        self._osc_style_override = None
        self._osc_style_resolved = None
        self._osc_script_loaded = False
        self.styles_built_with = []

    def _reinit(self):
        """What `_init_mpv` does with the answer, and nothing else."""
        osc_style = self._effective_osc_style()
        self.styles_built_with.append(osc_style)
        self._osc_style_resolved = osc_style
        self._osc_script_loaded = osc_style == "mpv"
        return osc_style


class OverrideSurvivesTest(unittest.TestCase):
    def test_it_survives_repeated_re_creation(self):
        # Three, not one: the bug is state feeding back into the input that
        # produced it, and a single re-init passes against the broken code
        # if the first one happens to be right.
        pm = _Fake()
        pm.set_osc_style("none")
        for _ in range(3):
            pm._reinit()
        self.assertEqual(pm.styles_built_with, ["none", "none", "none"])
        self.assertEqual(pm._osc_style_resolved, "none")

    def test_without_an_override_the_settings_still_decide(self):
        # The override must not become a one-way latch on every install.
        pm = _Fake()
        with mock.patch("jellyfin_mpv_shim.player.resolve_osc_style",
                        return_value="mpvtk"):
            pm._reinit()
        self.assertEqual(pm._osc_style_resolved, "mpvtk")

    def test_a_later_override_replaces_an_earlier_one(self):
        pm = _Fake()
        pm.set_osc_style("mpv")
        pm.set_osc_style("none")
        pm._reinit()
        self.assertEqual(pm.styles_built_with, ["none"])


class SourceContractTest(unittest.TestCase):
    """`_init_mpv` builds its options from the effective style.

    Read rather than run: constructing a PlayerManager opens a window, and
    what is being checked is an ordering.
    """

    def _src(self):
        import inspect

        from jellyfin_mpv_shim import player
        return inspect.getsource(player.PlayerManager._init_mpv)

    def test_it_asks_for_the_effective_style(self):
        src = self._src()
        self.assertIn("self._effective_osc_style()", src,
                      "_init_mpv resolves the style itself and never "
                      "consults the override, so a fallback lasts one mpv")

    def test_it_asks_before_the_options_are_built(self):
        src = self._src()
        at = src.index("self._effective_osc_style()")
        for built_from in ("mpv_scripts(", "build_mpv_options("):
            with self.subTest(built_from):
                self.assertLess(
                    at, src.index(built_from),
                    "%s is built before the effective style is known, so a "
                    "no-lua mpv is still handed lua" % built_from)

    def test_set_osc_style_records_the_override(self):
        import inspect

        from jellyfin_mpv_shim import player
        src = inspect.getsource(player.PlayerManager.set_osc_style)
        self.assertIn("self._osc_style_override = style", src)


if __name__ == "__main__":
    unittest.main()
