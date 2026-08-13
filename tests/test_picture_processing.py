"""Deinterlacing and motion interpolation: what each setting writes to mpv.

Both are settings whose failure mode is that **nothing happens and nobody
is told**. `--interpolation` without a display-sync `--video-sync` is, in
mpv's own words, "silently disabled"; a `--tscale` or `--deinterlace` value
mpv does not recognise is refused at the property write, which the player
catches and logs at debug. Either way the picture is unchanged and the
setting reads as on.

So the vocabulary is checked against mpv itself rather than against our own
spelling of it, the way `test_mpv_stat_properties` checks property names --
`mpv --list-options` answers offline, in milliseconds, with no playback and
no window. Skipped where mpv is not on PATH, since the unit suite must not
require it.
"""

import re
import shutil
import subprocess
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.conf import settings  # noqa: E402
from jellyfin_mpv_shim.mpv_options import (  # noqa: E402
    INTERPOLATION_PRESETS, deinterlace_value, interpolation_props)


def _mpv_option_choices():
    """``{option: {choice, ...}}`` from ``mpv --list-options``, or None."""
    exe = shutil.which("mpv")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--list-options"], check=True,
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    choices = {}
    for line in out.splitlines():
        m = re.match(r"\s+--([a-z0-9-]+)\s+Choices:\s+(.*?)(?:\s+\(default:.*)?$",
                     line)
        if m:
            choices[m.group(1)] = set(m.group(2).split())
        elif re.match(r"\s+--([a-z0-9-]+)\s+Flag\b", line):
            choices[re.match(r"\s+--([a-z0-9-]+)", line).group(1)] = {"yes", "no"}
    return choices


class DeinterlaceResolutionTest(unittest.TestCase):
    def setUp(self):
        self.was = settings.deinterlace_auto
        self.addCleanup(setattr, settings, "deinterlace_auto", self.was)

    def test_off_by_default(self):
        settings.deinterlace_auto = False
        self.assertEqual(deinterlace_value(), "no")

    def test_the_setting_asks_for_auto_not_for_yes(self):
        """The distinction the whole feature turns on: "deinterlace what is
        flagged" is not "deinterlace everything", and writing `yes` here
        would soften every progressive file in the library."""
        settings.deinterlace_auto = True
        self.assertEqual(deinterlace_value(), "auto")

    def test_the_session_override_outranks_the_setting_both_ways(self):
        for auto in (False, True):
            with self.subTest(deinterlace_auto=auto):
                settings.deinterlace_auto = auto
                self.assertEqual(deinterlace_value(True), "yes")
                self.assertEqual(deinterlace_value(False), "no")

    def test_no_override_is_None_rather_than_False(self):
        """False is a real answer -- "off, whatever the setting says" -- so
        the absent case cannot be spelled the same way. With `auto` on, a
        None that was read as False would turn the setting off for everyone
        who never touched the menu."""
        settings.deinterlace_auto = True
        self.assertEqual(deinterlace_value(None), "auto")
        self.assertNotEqual(deinterlace_value(None), deinterlace_value(False))


class InterpolationPresetTest(unittest.TestCase):
    def setUp(self):
        self.was = settings.motion_interpolation
        self.addCleanup(setattr, settings, "motion_interpolation", self.was)

    def _props(self, value):
        settings.motion_interpolation = value
        return interpolation_props()

    def test_off_writes_nothing(self):
        """Not "writes the defaults": `video-sync` is a timing mode somebody
        may have chosen in their own mpv.conf, and an off that wrote `audio`
        over it would be this setting overriding a more specific statement.
        """
        self.assertEqual(self._props("off"), {})

    def test_an_unknown_value_reads_as_off(self):
        """It is a plain string in a JSON file somebody can type into. The
        alternative to a default is a KeyError out of the middle of starting
        playback."""
        self.assertEqual(self._props("smoooth"), {})
        self.assertEqual(self._props(""), {})

    def test_every_on_preset_sets_video_sync_as_well(self):
        """mpv: "--interpolation requires setting the --video-sync option to
        one of the display- modes, or it will be SILENTLY DISABLED". A
        preset that set only `interpolation` would be a setting that does
        nothing and reports success -- which is the single most likely way
        for this feature to be wrong."""
        for name in ("smooth", "blend", "hq"):
            with self.subTest(preset=name):
                props = self._props(name)
                self.assertTrue(props["interpolation"])
                self.assertTrue(
                    str(props["video-sync"]).startswith("display-"),
                    "%s does not enable display sync: %r" % (name, props))

    def test_every_on_preset_names_its_own_filter(self):
        """The three differ only in `tscale`, so a preset that inherited
        another's filter would be a duplicate menu entry."""
        filters = [self._props(n)["tscale"] for n in ("smooth", "blend", "hq")]
        self.assertEqual(len(set(filters)), 3, filters)

    def test_the_props_are_a_copy(self):
        """The caller writes these to mpv; a shared dict would let one
        playback's fiddling reach every later one."""
        first = self._props("smooth")
        first["tscale"] = "nonsense"
        self.assertNotEqual(interpolation_props()["tscale"], "nonsense")


class MpvVocabularyTest(unittest.TestCase):
    """The values are mpv's, checked against mpv."""

    def setUp(self):
        self.choices = _mpv_option_choices()
        if not self.choices:
            self.skipTest("mpv is not on PATH")

    def test_every_interpolation_preset_uses_options_mpv_has(self):
        for name, props in INTERPOLATION_PRESETS.items():
            for option, value in props.items():
                with self.subTest(preset=name, option=option):
                    self.assertIn(option, self.choices,
                                  "mpv has no --%s" % option)
                    if isinstance(value, bool):
                        continue          # a flag; the name was the question
                    self.assertIn(str(value), self.choices[option],
                                  "--%s does not accept %r" % (option, value))

    def test_tscale_is_checked_and_not_merely_present(self):
        """Guards the test above: `tscale` is the option whose values are a
        long list, so it is the one where a typo survives an "is the option
        real" check. If mpv ever stops enumerating them, this stops being a
        test and should fail rather than pass quietly."""
        self.assertGreater(len(self.choices.get("tscale") or ()), 5)

    def test_deinterlace_accepts_every_value_we_resolve_to(self):
        for value in ("no", "yes", "auto"):
            with self.subTest(value=value):
                self.assertIn(value, self.choices.get("deinterlace") or set())

    def test_the_labelled_enum_matches_the_presets(self):
        """The Settings dropdown and the table it drives are two lists of
        the same thing, and a value in one and not the other is either a
        dead menu entry or a preset nobody can reach."""
        from jellyfin_mpv_shim.mpvtk_browser import config

        labelled = {v for _label, v in
                    config.LABELED_ENUMS["motion_interpolation"]}
        self.assertEqual(labelled, set(INTERPOLATION_PRESETS))


if __name__ == "__main__":
    unittest.main()
