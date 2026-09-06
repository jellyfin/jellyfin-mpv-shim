"""HUD-only settings are not offered where they cannot do anything (#724).

Six settings reach the in-window playback HUD and nothing else -- they are
read in one place, `gateway/hud.py:hud_key_opts`, which builds the blob sent
with the `mpvtk-hud` engage, and that message is only ever sent by the HUD
modality. Under "MPV UI with thumbnails" or "MPV built-in default" they are
inert.

They were offered anyway, and that is what the report reads like from the
outside: "Left Click Pauses Playback" is on, the left button does nothing
over the video, "appears to be completely broken". The client was working
as designed -- the setting was never going to reach that player -- which is
the worst kind of correct.

`SECTIONS` already said so in a comment ("osc_style decides whether the rest
of the group applies at all") and nothing acted on it. The mechanism to act
on it also already existed: `sections()` hides the audio passthrough toggles
the selected `audio_mode` cannot carry, and `audio_exclusive` on platforms
mpv ignores it on.
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

sys.argv = ["test"]

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402


def _shown(style, **overrides):
    """Every key the settings form would draw under ``style``."""
    patches = {"osc_style": style, "enable_gui": True,
               "thumbnail_osc_builtin": True}
    patches.update(overrides)
    with mock.patch.multiple(settings, **patches):
        return {k for _title, keys in cfg.sections() for k in keys}


class VisibilityTest(unittest.TestCase):
    def test_the_jellyfin_ui_offers_them(self):
        shown = _shown("mpvtk")
        for key in cfg.HUD_ONLY:
            with self.subTest(key=key):
                self.assertIn(key, shown)

    def test_the_mpv_osc_does_not(self):
        shown = _shown("mpv")
        for key in cfg.HUD_ONLY:
            with self.subTest(key=key):
                self.assertNotIn(key, shown)

    def test_nor_the_builtin_default(self):
        shown = _shown("default")
        self.assertFalse(set(cfg.HUD_ONLY) & shown)

    def test_nor_no_controls_at_all(self):
        self.assertFalse(set(cfg.HUD_ONLY) & _shown("none"))

    def test_the_legacy_alias_counts_as_the_jellyfin_ui(self):
        """"jellyfin" is what the retired lua OSC was called and is still a
        valid value in an old conf.json. Reading `settings.osc_style`
        directly instead of resolving it would hide the HUD's own settings
        from exactly the users who have had it longest."""
        self.assertTrue(set(cfg.HUD_ONLY) <= _shown("jellyfin"))

    def test_a_fallback_that_demotes_mpvtk_hides_them_too(self):
        """`resolve_osc_style` demotes "mpvtk" when the GUI is off or the
        legacy `thumbnail_osc_builtin` opt-out is set. The HUD does not run
        in either case, so neither should its settings appear."""
        self.assertFalse(set(cfg.HUD_ONLY)
                         & _shown("mpvtk", thumbnail_osc_builtin=False))


class NotHiddenTest(unittest.TestCase):
    """The neighbours in the same group that DO work under every style."""

    def test_the_style_picker_itself_always_shows(self):
        for style in ("mpvtk", "mpv", "default", "none"):
            with self.subTest(style=style):
                self.assertIn("osc_style", _shown(style))

    def test_mouse_chapter_nav_always_shows(self):
        """Bound by `player.py`, not by the renderer, so the thumb buttons
        seek chapters under every style. It sits in the same group as the
        six above and is the easiest thing to hide by accident."""
        for style in ("mpvtk", "mpv", "default"):
            with self.subTest(style=style):
                self.assertIn("mouse_chapter_nav", _shown(style))

    def test_trickplay_fast_mode_always_shows(self):
        """thumbfast is loaded for both OSCs."""
        self.assertIn("trickplay_fast_mode", _shown("mpv"))


class SeedingTest(unittest.TestCase):
    """A hideable set has to be SEEDED into `curated`, or it cannot hide.

    `sections()` computes `hidden = curated - shown`, so a key that is not
    in `curated` can never be in `hidden` and the filter simply passes it
    through. That is why `HUD_ONLY` joins the seed beside the audio sets --
    and it is worth stating, because the failure is silent: the rows carry
    on being drawn and nothing says why.

    **The leak this class was first written to catch cannot happen.**
    `hidden` is a subset of `curated` by construction, so a hidden key is
    always claimed and can never reappear under "Advanced". Measured
    against the unseeded mutation: the keys stay in Player Controls rather
    than moving. The test asserting otherwise passed either way and is gone;
    what is left is the property that actually breaks.
    """

    def test_hud_only_is_seeded_so_the_filter_can_reach_it(self):
        with mock.patch.multiple(settings, osc_style="mpv", enable_gui=True,
                                 thumbnail_osc_builtin=True):
            groups = dict(cfg.sections())
        drawn = {k for keys in groups.values() for k in keys}
        self.assertFalse(set(cfg.HUD_ONLY) & drawn,
                         "HUD_ONLY is not reaching `hidden` -- check it is "
                         "in the `curated` seed in sections()")

    def test_they_are_still_reachable_when_the_hud_is_on(self):
        """Seeding must not become hiding-forever: with the HUD selected
        they belong in their real group, not nowhere."""
        with mock.patch.multiple(settings, osc_style="mpvtk", enable_gui=True,
                                 thumbnail_osc_builtin=True):
            groups = dict(cfg.sections())
        controls = set(groups.get("Player Controls", ()))
        self.assertTrue(set(cfg.HUD_ONLY) <= controls)

    def test_advanced_never_holds_a_hidden_key(self):
        """Not the leak test that could not fail -- this one names the
        invariant that makes the leak impossible, so a future `sections()`
        that filters a group WITHOUT seeding is caught here rather than
        shipping rows nobody can explain."""
        with mock.patch.multiple(settings, osc_style="mpv", enable_gui=True,
                                 thumbnail_osc_builtin=True):
            groups = dict(cfg.sections())
        advanced = set(groups.get("Advanced", ()))
        curated = {k for title, keys in groups.items()
                   if title != "Advanced" for k in keys}
        self.assertFalse(advanced & curated,
                         "a key is drawn twice: in a group and in Advanced")


class ResolverTest(unittest.TestCase):
    def test_it_never_takes_the_form_down(self):
        """`hud_style_selected` is only ever used to decide whether to draw
        a row; a settings screen that refuses to open is worse than one
        showing a setting that does nothing, so it fails open."""
        with mock.patch("jellyfin_mpv_shim.mpv_options.resolve_osc_style",
                        side_effect=RuntimeError("boom")):
            self.assertTrue(cfg.hud_style_selected())

    def test_every_hud_only_key_is_a_real_setting(self):
        for key in cfg.HUD_ONLY:
            with self.subTest(key=key):
                self.assertIn(key, cfg.settings_schema())

    def test_every_hud_only_key_is_read_by_the_hud(self):
        """The claim that earns the hiding: these reach the HUD and nothing
        else. Read from the source, so adding a key to HUD_ONLY that some
        other module also consumes fails here rather than silently hiding a
        setting that did something.

        **Two modules, not one.** This asked only the gateway until
        `hud_auto_scale` arrived, which was true of the five keys it
        started with by coincidence: they are all pushed to the renderer,
        which is the gateway's job. A key the HUD reads while BUILDING its
        widget tree is just as HUD-only, and asking only the gateway would
        have forced a real HUD setting to be shown to classic-OSC users --
        the #724 complaint this whole mechanism exists to answer."""
        import pathlib

        from jellyfin_mpv_shim.mpvtk_browser import hud as hud_ui
        from jellyfin_mpv_shim.mpvtk_browser.gateway import hud as hud_gw

        src = "".join(pathlib.Path(m.__file__).read_text(encoding="utf-8")
                      for m in (hud_gw, hud_ui))
        for key in cfg.HUD_ONLY:
            with self.subTest(key=key):
                self.assertIn("settings.%s" % key, src)

    def test_and_by_nothing_outside_it(self):
        """The other half, and the one that makes hiding honest: a key
        read anywhere else does something a classic-OSC user can see, so
        hiding it from them is a lie. Scans the package for reads outside
        the two HUD modules and the settings plumbing itself."""
        import pathlib

        from jellyfin_mpv_shim.mpvtk_browser import hud as hud_ui
        from jellyfin_mpv_shim.mpvtk_browser.gateway import hud as hud_gw

        allowed = {pathlib.Path(hud_gw.__file__).resolve(),
                   pathlib.Path(hud_ui.__file__).resolve()}
        root = pathlib.Path(hud_ui.__file__).resolve().parent.parent
        # conf.py declares them and config.py curates them; neither is a
        # behaviour that a classic OSC would show.
        allowed |= {(root / "conf.py").resolve(),
                    (root / "mpvtk_browser" / "config.py").resolve()}
        for key in cfg.HUD_ONLY:
            offenders = []
            for path in root.rglob("*.py"):
                if path.resolve() in allowed:
                    continue
                if "settings.%s" % key in path.read_text(encoding="utf-8"):
                    offenders.append(path.relative_to(root).as_posix())
            with self.subTest(key=key):
                self.assertEqual(offenders, [],
                                 "%s is hidden from classic-OSC users but "
                                 "read outside the HUD" % key)


if __name__ == "__main__":
    unittest.main()
