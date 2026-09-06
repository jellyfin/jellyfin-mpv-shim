"""`thumbnail_enable` is a curated setting, and `thumbnail_osc_builtin` is gone.

Two changes with one subject: which of the trickplay settings a user can
see, and which of them still exist.

**The promotion.** `thumbnail_enable` was uncurated, so it fell through to
Advanced -- behind a disclosure, under a name nobody types. It is the switch
for the whole feature (the worker in `_init_mpv`, and thumbfast, which
`mpv_scripts` loads only if the worker came up), it applies to every
`osc_style`, and since CONFIG_VERSION 5 folded "MPV built-in default" into
"MPV UI" it is the only thing that answers "do I want previews?". So it
belongs directly under the style dropdown, which is where the question is
asked.

**The deletion.** `thumbnail_osc_builtin`'s one documented meaning was "use
your own custom osc but leave trickplay enabled" (the README that introduced
it, `acbc3e9d`). That is `osc_style: custom`, and `mpv_scripts` loads
thumbfast under every style, so `custom` keeps the previews that sentence
promises. Nothing it could express was lost. It is deleted with no
migration -- a `false` in an old config is ignored, exactly as `enable_osc`
is; docs/do-not-fix.md carries why. The half that is invisible from here --
that its branch was the last thing which could resolve to "default", so
nothing does now -- is pinned in `test_mpv_options`, beside the function it
is about.
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

from jellyfin_mpv_shim.conf import Settings, settings  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import config as cfg  # noqa: E402


def _groups(**overrides):
    patches = {"osc_style": "mpvtk", "enable_gui": True,
               "thumbnail_enable": True}
    patches.update(overrides)
    with mock.patch.multiple(settings, **patches):
        return dict(cfg.sections())


class PlacementTest(unittest.TestCase):
    def test_it_sits_directly_under_the_style_dropdown(self):
        """The placement asked for, as the assertion.

        Directly under, not merely in the group: the dropdown is where a
        user decides what their controls look like, and "do they show
        previews" is the follow-up question to that one."""
        controls = dict(cfg.sections("playback"))["Player Controls"]
        self.assertEqual(
            controls[controls.index("osc_style"):][:2],
            ["osc_style", "thumbnail_enable"])

    def test_it_is_no_longer_in_advanced(self):
        """The half the promotion is FOR. A curated key is removed from
        Advanced by `sections()` computing that list as the complement, so
        this cannot fail while the row above passes -- which is why it
        names Advanced explicitly rather than trusting that."""
        self.assertNotIn("thumbnail_enable", _groups().get("Advanced", []))

    def test_it_is_offered_under_every_style(self):
        """NOT a HUD_ONLY setting, and the distinction is the whole point of
        that mechanism. thumbfast is loaded whatever the style, so previews
        reach the HUD, both classic OSCs and a thumbfast-aware script of the
        user's -- and turning them off saves the download under all four,
        including "none", where nothing draws them but the images are still
        being fetched."""
        for style in ("mpvtk", "mpv", "custom", "none"):
            with self.subTest(style=style):
                controls = _groups(osc_style=style)["Player Controls"]
                self.assertIn("thumbnail_enable", controls)

    def test_it_has_a_label_and_a_note(self):
        self.assertEqual(cfg.label_for("thumbnail_enable"),
                         "Enable Trickplay Thumbnails")
        self.assertIn("thumbnail_enable", cfg.NOTES)

    def test_it_needs_a_restart(self):
        """`_init_mpv` reads it once per mpv, and mpv is not re-created
        between queue items -- so without the banner, turning it on looks
        like a switch that does nothing until the user happens to restart.

        The direction that would be worst is ON: the worker starts and
        fetches, and thumbfast was never loaded to draw any of it."""
        self.assertIn("thumbnail_enable", cfg.RESTART_REQUIRED)


class SearchTest(unittest.TestCase):
    """Advanced is behind a disclosure, so search was how this setting was
    reachable at all before the promotion. It stays reachable after it."""

    def _finds(self, query):
        return {k for _tab, _title, keys in cfg.search(query) for k in keys}

    def test_the_words_people_type_find_it(self):
        for query in ("thumbnail", "trickplay", "preview", "scrubbing",
                      "seek preview", "chapter"):
            with self.subTest(query=query):
                self.assertIn("thumbnail_enable", self._finds(query))


class DependentTest(unittest.TestCase):
    """`trickplay_fast_mode` tunes how the previews are fetched, so with
    previews off it does nothing at all. Hidden, not disabled
    (docs/settings-curation.md section 2)."""

    def test_it_is_offered_while_thumbnails_are_on(self):
        self.assertIn("trickplay_fast_mode",
                      _groups(thumbnail_enable=True)["Player Controls"])

    def test_it_is_hidden_while_they_are_off(self):
        """Every group, not just its own: this is also what proves the key
        was seeded into `sections()`'s `curated` set.

        `hidden` is computed as `curated - shown`, so a hideable key nobody
        seeded is never in `hidden` and the filter simply does not reach it
        -- the row goes on being drawn and nothing says why. The mirror
        test, that a hidden key leaks into Advanced instead, is the one
        that CANNOT fail here (c06e0351): `hidden` is a subset of `curated`
        by construction, so the leak it looks for is impossible."""
        drawn = {k for keys in _groups(thumbnail_enable=False).values()
                 for k in keys}
        self.assertNotIn("trickplay_fast_mode", drawn)


class OptOutIsGoneTest(unittest.TestCase):
    def test_the_setting_no_longer_exists(self):
        self.assertNotIn("thumbnail_osc_builtin", settings.__fields__)

    def test_an_old_config_is_ignored_rather_than_migrated(self):
        """No migration, deliberately [iw]: the flag defaulted ON and was
        turned off by hand, which says "don't replace my OSC" but is not
        proof that a replacement exists. `custom` -- the faithful reading of
        its own documentation -- sets `osc=False` and loads nothing, and a
        silent upgrade must never leave somebody with no controls at all.

        So the value is dropped and they land on the `mpvtk` default, and
        anyone genuinely running uosc sets `osc_style: custom` once and can
        see that they have.

        Driven the way `load` drives it -- `parse_obj`, then the copy loop,
        then `_migrate` with the raw dict -- because `_migrate` alone is the
        only step that could read a removed key, and asserting on it alone
        would pass for the trivial reason that there is no code there. The
        three assertions are the three things a user experiences: the value
        does not load, nothing translates it, and it leaves the file."""
        s = Settings()
        data = {"thumbnail_osc_builtin": False, "osc_style": "mpvtk"}
        parsed = s.parse_obj(data)
        self.assertNotIn("thumbnail_osc_builtin", parsed.__fields_set__)
        for key in parsed.__fields_set__:
            setattr(s, key, getattr(parsed, key))
        s._migrate(data)
        self.assertEqual(s.osc_style, "mpvtk")
        self.assertNotIn("thumbnail_osc_builtin", s.dict())


if __name__ == "__main__":
    unittest.main()
