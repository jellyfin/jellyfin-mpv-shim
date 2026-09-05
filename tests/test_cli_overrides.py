"""A command-line flag applies to one run and must not reach conf.json.

`save()` serializes `dict()`, and `dict()` walks EVERY field -- so a flag
assigned straight onto the settings object is written out by the next save
from anywhere. Saves are not rare: window geometry (`remember_window_size`
is on by default), audio device, shader profile, the deband/tone-mapping
writes, and every settings-screen edit. One launch with `--minimized` plus
one window resize and the option was on for good (#718).

Four flags have this shape and only `--scale` had a guard, which was a
comment asking future callers not to save. This is the mechanism that
replaces it, so a fifth flag is covered by construction.

**The half that is easy to get wrong.** The obvious repair -- have `dict()`
substitute the on-disk value for overridden fields -- is not enough on its
own, because `dict()` is not only the persistence boundary:
`mpvtk_browser.config.get_settings` uses it to populate the settings
screen, and `set_setting` writes the same field and saves immediately. So
an override needs a LIFETIME: the moment the user states an intent for that
key, the flag stops speaking for it. Otherwise launching with `--scale 2`
and then choosing 1.5 in Settings silently reverts to what was on disk --
a worse bug than the one being fixed, because it discards a deliberate act
rather than persisting an accidental one.
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

from jellyfin_mpv_shim.conf import Settings  # noqa: E402


#: (flag dest on `args`, settings key, a value that is not the default).
#: Every CLI flag that overrides a config key belongs here; the pairing is
#: asserted against mpv_shim.py below, so adding a flag without adding it
#: here fails rather than going quietly uncovered.
FLAGS = (
    ("enable_gui", "enable_gui", False),
    ("start_minimized", "start_minimized", True),
    ("mpv_loglevel", "mpv_log_level", "debug"),
    ("ui_scale", "ui_scale", 2.0),
)


def _loaded(**on_disk):
    """A Settings as if `load()` had just read these values from a file."""
    s = Settings()
    for key, value in on_disk.items():
        setattr(s, key, value)
    return s


class PersistenceTest(unittest.TestCase):
    def test_an_override_does_not_reach_the_serialized_form(self):
        for _flag, key, value in FLAGS:
            with self.subTest(key=key):
                s = _loaded()
                on_disk = getattr(s, key)
                s.apply_cli_override(key, value)
                self.assertNotEqual(value, on_disk, "fixture value is the "
                                    "default, so this cannot fail")
                self.assertEqual(s.dict()[key], on_disk)

    def test_but_it_is_live_on_the_object(self):
        """The run still gets what it was told. This is the whole point of
        the flag, and a `dict()` shadow that also changed the live value
        would be a different bug."""
        for _flag, key, value in FLAGS:
            with self.subTest(key=key):
                s = _loaded()
                s.apply_cli_override(key, value)
                self.assertEqual(getattr(s, key), value)

    def test_an_unrelated_save_leaves_the_disk_value_alone(self):
        """The reported shape: launch with --minimized, resize the window
        (which persists geometry), and the flag has become the setting."""
        s = _loaded(start_minimized=False)
        s.apply_cli_override("start_minimized", True)
        s.window_width = 1280            # what a geometry save writes
        self.assertIs(s.dict()["start_minimized"], False)

    def test_every_other_field_still_serializes_live(self):
        s = _loaded()
        s.apply_cli_override("start_minimized", True)
        s.window_width = 1234
        self.assertEqual(s.dict()["window_width"], 1234)

    def test_with_no_overrides_dict_is_unchanged(self):
        """The overwhelmingly common path must not move at all."""
        s = _loaded()
        self.assertEqual(s.dict(), Settings().dict())


class OverrideLifetimeTest(unittest.TestCase):
    """The half a `dict()` shadow alone gets wrong."""

    def test_an_explicit_edit_of_an_overridden_key_persists(self):
        """Launch with --scale 2, then choose 1.5 in Settings. Without a
        lifetime the shadow writes back the on-disk 1.0 and the user's
        choice is discarded."""
        s = _loaded(ui_scale=1.0)
        s.apply_cli_override("ui_scale", 2.0)
        s.ui_scale = 1.5                 # what set_setting does
        self.assertEqual(s.dict()["ui_scale"], 1.5)

    def test_and_the_flag_stops_speaking_for_it_from_then_on(self):
        s = _loaded(start_minimized=False)
        s.apply_cli_override("start_minimized", True)
        s.start_minimized = True         # the user turns it on for real
        s.window_width = 900             # some later unrelated save
        self.assertIs(s.dict()["start_minimized"], True)

    def test_editing_one_key_does_not_release_another(self):
        s = _loaded(start_minimized=False, ui_scale=1.0)
        s.apply_cli_override("start_minimized", True)
        s.apply_cli_override("ui_scale", 2.0)
        s.ui_scale = 1.5
        self.assertIs(s.dict()["start_minimized"], False)
        self.assertEqual(s.dict()["ui_scale"], 1.5)

    def test_the_settings_screen_is_shown_what_is_configured(self):
        """`config.get_settings()` returns `dict()`. Showing the flag's
        value would tell the user they had configured something they had
        not, and the next edit would fight it."""
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg

        s = cfg.settings
        was = s.start_minimized
        try:
            s.start_minimized = False
            s.apply_cli_override("start_minimized", True)
            self.assertIs(cfg.get_settings()["start_minimized"], False)
        finally:
            s.__cli_overrides__.pop("start_minimized", None)
            s.start_minimized = was


class WiringTest(unittest.TestCase):
    """One table, three consumers.

    The pairing "flag X shadows setting Y" is needed in three places --
    where the flags are APPLIED (`mpv_shim.main`), where they are kept OUT
    of the config file (`settings_base.apply_cli_override`), and where they
    are re-passed across a self-RESTART (`restart._durable_flags`). It used
    to be written out by hand in two of them and implied in the third,
    which is three chances to add a flag to some of them.
    """

    def test_this_files_table_is_the_shipped_one(self):
        """So a fifth flag cannot be added without a test value for it."""
        from jellyfin_mpv_shim.args import CLI_OVERRIDES

        self.assertEqual([o.dest for o in CLI_OVERRIDES],
                         [f[0] for f in FLAGS])
        self.assertEqual([o.key for o in CLI_OVERRIDES],
                         [f[1] for f in FLAGS])

    def test_mpv_shim_routes_every_flag_through_the_override(self):
        """A flag assigned with `settings.x = ...` is the bug. Read from
        the source, because the alternative is launching the app."""
        import pathlib

        from jellyfin_mpv_shim import mpv_shim

        src = pathlib.Path(mpv_shim.__file__).read_text()
        for _flag, key, _value in FLAGS:
            with self.subTest(key=key):
                self.assertNotIn("settings.%s = args" % key, src)
        self.assertIn("CLI_OVERRIDES", src)
        self.assertIn("apply_cli_override", src)

    def test_restart_reads_the_same_table(self):
        """`restart._durable_flags` re-passes these across a self-restart.
        A hand-written list there drifts from the one that applies them."""
        import pathlib

        from jellyfin_mpv_shim import restart

        src = pathlib.Path(restart.__file__).read_text()
        self.assertIn("CLI_OVERRIDES", src)
        for _flag, key, _value in FLAGS:
            with self.subTest(key=key):
                self.assertNotIn('"%s" not in pending' % key, src)

    def test_every_overridable_key_is_a_real_setting(self):
        s = Settings()
        for _flag, key, _value in FLAGS:
            with self.subTest(key=key):
                self.assertIn(key, s.__fields__)

    def test_an_unknown_key_is_refused(self):
        """A typo'd key would otherwise record an override nothing reads,
        and the flag would silently persist after all."""
        with self.assertRaises(KeyError):
            Settings().apply_cli_override("not_a_setting", 1)


class SpellingTest(unittest.TestCase):
    """`CliOverride.spell` is what a restart re-passes on the command line,
    so it has to round-trip through the parser it came from."""

    def test_a_boolean_flag_spells_both_ways(self):
        from jellyfin_mpv_shim.args import CLI_OVERRIDES

        gui = next(o for o in CLI_OVERRIDES if o.dest == "enable_gui")
        self.assertEqual(gui.spell(True), ["--gui"])
        self.assertEqual(gui.spell(False), ["--no-gui"])

    def test_a_value_flag_carries_its_value(self):
        from jellyfin_mpv_shim.args import CLI_OVERRIDES

        scale = next(o for o in CLI_OVERRIDES if o.dest == "ui_scale")
        self.assertEqual(scale.spell(2.0), ["--scale", "2.0"])

    def test_every_spelling_parses_back_to_the_same_value(self):
        """The round trip, against the real parser: a restart that emits a
        flag the parser rejects would not come back at all."""
        from jellyfin_mpv_shim import args as args_mod

        parser = args_mod._build_parser()
        for override, (_flag, _key, value) in zip(args_mod.CLI_OVERRIDES,
                                                  FLAGS):
            with self.subTest(dest=override.dest):
                parsed = parser.parse_args(override.spell(value))
                got = getattr(parsed, override.dest)
                self.assertEqual(got, value)


if __name__ == "__main__":
    unittest.main()
