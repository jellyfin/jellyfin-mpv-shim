""""MPV built-in default" folds into "MPV UI" (CONFIG_VERSION 5).

The two only ever differed in who loaded the OSC: `default` let mpv load its
own, `mpv` had the shim load a bundled fork. Once the shim started using
mpv's OWN OSC for the `mpv` style -- driven through the 0.41 OSC Preview API
-- the difference stopped being visible as anything but a bug: nothing
suppressed mpv's "Drop files or URLs to play" logo under `default`, so it sat
behind the library.

[iw], on the fact that `default` was the only style that left mpv's OSC
loading alone: *"default has the 'drag to start' issue present, so we need to
do something about it, we already have a custom and no osc option for people
who want that."* So the shim owns this one, `custom` stays for a user's own
OSC and `none` for no controls, and whether previews appear is
`thumbnail_enable` -- a question about trickplay, not about which controls
you want.

Two ways an old value can still arrive, and both are covered: the migration
rewrites what is on disk, and `resolve_osc_style` keeps accepting it for a
hand-edited conf.json or one restored from an older backup.
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

from jellyfin_mpv_shim.conf import CONFIG_VERSION, Settings  # noqa: E402


def _at(version, **kw):
    """A Settings as an older config would have loaded it."""
    s = Settings()
    s.config_version = version
    for key, value in kw.items():
        setattr(s, key, value)
    return s


class MigrationTest(unittest.TestCase):
    def test_default_becomes_mpv(self):
        s = _at(4, osc_style="default")
        self.assertTrue(s._migrate({}))
        self.assertEqual(s.osc_style, "mpv")

    def test_and_the_version_is_stamped(self):
        s = _at(4, osc_style="default")
        s._migrate({})
        self.assertEqual(s.config_version, CONFIG_VERSION)

    def test_every_other_style_is_left_alone(self):
        """It is a rename, not a reset: `mpvtk`, `custom` and `none` are
        real choices and must survive untouched."""
        for style in ("mpvtk", "mpv", "custom", "none", "jellyfin"):
            with self.subTest(style=style):
                s = _at(4, osc_style=style)
                s._migrate({})
                self.assertEqual(s.osc_style, style)

    def test_it_does_not_re_run(self):
        """A config already at the current version is not migrated again --
        which matters because somebody may have hand-set `default` back."""
        s = _at(CONFIG_VERSION, osc_style="default")
        s._migrate({})
        self.assertEqual(s.osc_style, "default",
                         "a current config was migrated a second time")

    def test_the_default_style_is_not_the_one_being_removed(self):
        """A fresh install must not land on a value the form no longer
        offers. (`Settings()` itself is version 0 -- `load()` is what stamps
        a new install as current, which is why this asserts the style and
        not the version.)"""
        s = Settings()
        self.assertEqual(s.osc_style, "mpvtk")
        self.assertLess(s.config_version, CONFIG_VERSION)


class OfferedChoicesTest(unittest.TestCase):
    def test_the_settings_screen_no_longer_offers_default(self):
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg

        values = [v for _label, v in cfg.LABELED_ENUMS["osc_style"]]
        self.assertNotIn("default", values)
        self.assertEqual(values, ["mpvtk", "mpv", "custom", "none"])

    def test_the_survivors_cover_what_default_was_for(self):
        """Nothing is lost: `custom` is "my own OSC, leave it alone" and
        `none` is "no controls". Those are the two reasons anyone chose
        `default`, and both are still one click away."""
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg

        values = [v for _label, v in cfg.LABELED_ENUMS["osc_style"]]
        self.assertIn("custom", values)
        self.assertIn("none", values)

    def test_a_migrated_config_lands_on_an_offered_value(self):
        """The migration must not strand anyone on a value the form cannot
        display -- a dropdown with no matching entry is a setting the user
        can see and cannot change back."""
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg

        s = _at(4, osc_style="default")
        s._migrate({})
        values = [v for _label, v in cfg.LABELED_ENUMS["osc_style"]]
        self.assertIn(s.osc_style, values)


if __name__ == "__main__":
    unittest.main()
