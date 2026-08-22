"""The preset-driven picture settings: what each one writes to mpv.

Deinterlacing, motion interpolation, debanding, tone mapping, rendering
quality and network buffering. All but the first share one table shape and
one apply path (`mpv_options.PRESET_SETTINGS`), so most of what is asserted
here is asserted for every entry rather than for the one that happened to be
written first.

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
    DEBAND_PRESETS, INTERPOLATION_PRESETS, PRESET_SETTINGS,
    RENDER_QUALITY_PRESETS, deinterlace_value, interpolation_props,
    preset_keys, preset_props)


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


def _mpv_option_names():
    """Every option name ``mpv --list-options`` knows, or None.

    Separate from ``_mpv_option_choices`` because most of what the preset
    tables write is numeric -- ``deband-threshold``, ``demuxer-max-bytes`` --
    and those lines carry a type and a range rather than a choice list. The
    name is still the half that a typo lands in.
    """
    exe = shutil.which("mpv")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--list-options"], check=True,
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    return {m.group(1) for m in
            (re.match(r"\s+--([a-z0-9-]+)\s", line) for line in out.splitlines())
            if m}


def _mpv_profile(name):
    """``{option: value}`` for one of mpv's built-in profiles, or None.

    Only the profile's own lines are read: ``--show-profile`` indents an
    included profile's contents one level further (``gpu-hq`` is now nothing
    but an include of ``high-quality``), and folding those in would report a
    profile as setting things it merely inherits.
    """
    exe = shutil.which("mpv")
    if not exe:
        return None
    try:
        out = subprocess.run([exe, "--show-profile=" + name], check=True,
                             capture_output=True, text=True, timeout=30).stdout
    except Exception:
        return None
    entries = {}
    for line in out.splitlines():
        m = re.match(r" (?! )([a-z0-9-]+)=(.*)$", line)
        if m:
            entries[m.group(1)] = m.group(2).strip()
    return entries


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


class PresetRegistryTest(unittest.TestCase):
    """What every entry in PRESET_SETTINGS owes, asserted for all of them.

    Motion interpolation had these properties for a long time as the only
    member; four more settings then adopted the same shape. The repeated bug
    in this codebase is the discipline being applied to the first
    implementation and quietly dropped by the second, so these are written
    against the registry rather than against any one setting.
    """

    def _with(self, key, value):
        was = getattr(settings, key)
        self.addCleanup(setattr, settings, key, was)
        setattr(settings, key, value)
        return preset_props(key)

    def test_the_off_value_of_every_setting_writes_nothing(self):
        """The single most important property here, and the reason it is
        not "writes mpv's defaults back": every property these tables touch
        is one the user may have set in their own mpv.conf. All five default
        to off, so an off that wrote its idea of "not doing this" would
        reach out on the first item played and undo their config, with no
        setting here to put it back.

        It is also what makes "leave the setting off and write your own
        values" a supported way to use these -- which is the whole answer
        for anyone wanting a combination the presets do not offer.
        """
        for key, (presets, fallback) in PRESET_SETTINGS.items():
            with self.subTest(setting=key):
                self.assertIn(fallback, presets,
                              "%s's off value is not in its table" % key)
                self.assertEqual(presets[fallback], {},
                                 "%s's off value writes to mpv" % key)
                self.assertEqual(self._with(key, fallback), {})

    def test_an_unknown_value_reads_as_off_for_every_setting(self):
        """These are plain strings in a JSON file somebody can type into,
        and the alternative to a default is a KeyError out of the middle of
        starting playback."""
        for key in PRESET_SETTINGS:
            with self.subTest(setting=key):
                self.assertEqual(self._with(key, "nonsense-value"), {})
                self.assertEqual(self._with(key, ""), {})

    def test_preset_keys_covers_every_property_any_preset_writes(self):
        """What "off" restores is this whole set, not the properties the
        CURRENT preset happens to name. Somebody who used interpolation's
        `hq` and then switched to `off` must get their `tscale` back too --
        and somebody who went `strong` -> `light` -> `off` must get all four
        deband parameters back, not the ones `light` touched."""
        for key, (presets, _fallback) in PRESET_SETTINGS.items():
            with self.subTest(setting=key):
                every = {prop for props in presets.values() for prop in props}
                self.assertEqual(set(preset_keys(key)), every)

    def test_the_props_are_a_copy_for_every_setting(self):
        """The caller writes these to mpv; a shared dict would let one
        playback's fiddling reach every later one."""
        for key, (presets, fallback) in PRESET_SETTINGS.items():
            on = [n for n, p in presets.items() if p]
            if not on:
                continue
            with self.subTest(setting=key):
                props = self._with(key, on[0])
                prop = next(iter(props))
                props[prop] = "nonsense"
                self.assertNotEqual(preset_props(key).get(prop), "nonsense")

    def test_every_setting_is_reachable_from_the_settings_form(self):
        """A preset nobody can select is a dead table, and a dropdown entry
        with no preset behind it is a control that reads as off whatever you
        pick. Both have shipped here in other guises."""
        from jellyfin_mpv_shim.mpvtk_browser import config

        for key, (presets, _fallback) in PRESET_SETTINGS.items():
            with self.subTest(setting=key):
                labelled = config.LABELED_ENUMS.get(key)
                self.assertIsNotNone(
                    labelled, "%s has no dropdown in config.py" % key)
                self.assertEqual({v for _label, v in labelled}, set(presets))

    def test_every_setting_has_a_declared_default_that_is_its_off_value(self):
        """A setting whose shipped default is not the off value would be
        writing to mpv on a fresh install -- which is the one thing the
        whole snapshot-and-restore design exists to avoid."""
        from jellyfin_mpv_shim.conf import Settings

        for key, (_presets, fallback) in PRESET_SETTINGS.items():
            with self.subTest(setting=key):
                self.assertEqual(getattr(Settings, key), fallback)


class DebandLadderTest(unittest.TestCase):
    """The deband presets are a ladder, and the parameters are not
    independent of one another."""

    #: mpv's own defaults, measured from `mpv --list-options` on 0.41 and
    #: re-checked by MpvVocabularyTest against whatever mpv is installed.
    MPV_DEFAULTS = {"deband-iterations": 1, "deband-threshold": 48,
                    "deband-range": 16, "deband-grain": 32}

    ORDER = ("light", "standard", "strong")

    def test_every_on_preset_actually_turns_debanding_on(self):
        """`deband` is a separate flag from its four parameters, so a preset
        that set only the numbers would be tuning a filter that never
        runs -- a control that reads as applied and changes nothing."""
        for name in self.ORDER:
            with self.subTest(preset=name):
                self.assertIs(DEBAND_PRESETS[name]["deband"], True)

    def test_the_ladder_is_monotone_in_every_parameter(self):
        """Not tidiness: threshold (how flat a region must be to be touched),
        iterations (how many passes) and grain (the noise added to mask what
        debanding could not fix) trade against each other. A ladder that
        raised the threshold while dropping the grain would look worse at
        the top than in the middle, and the labels would be lying."""
        for param in self.MPV_DEFAULTS:
            values = [DEBAND_PRESETS[n][param] for n in self.ORDER]
            with self.subTest(param=param):
                self.assertEqual(values, sorted(values), values)
                self.assertLess(values[0], values[-1],
                                "%s does not move across the ladder" % param)

    def test_light_sits_below_mpv_s_own_strength(self):
        """"Light (live action)" is the preset for content debanding mostly
        no-ops on, where the risk is a threshold high enough to smear real
        low-contrast texture. It has to be more cautious than mpv's default,
        or its label is describing the wrong thing."""
        light = DEBAND_PRESETS["light"]
        self.assertLess(light["deband-threshold"],
                        self.MPV_DEFAULTS["deband-threshold"])
        self.assertLess(light["deband-range"], self.MPV_DEFAULTS["deband-range"])

    def test_no_preset_sets_grain_to_zero(self):
        """The shader pack uses `deband-grain: 0`, and that is only correct
        because `static-grain-default` re-adds noise through shaders. These
        presets ship alone, so a zero here would remove the masking without
        replacing it -- the one number that must not be copied across."""
        for name in self.ORDER:
            with self.subTest(preset=name):
                self.assertGreater(DEBAND_PRESETS[name]["deband-grain"], 0)


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

    def test_every_preset_writes_an_option_this_mpv_has(self):
        """Across the whole registry, not just interpolation. A property
        mpv does not have raises at the write, which the player catches and
        logs at debug -- so the picture is unchanged and the setting reads
        as on, which is this file's whole subject."""
        names = _mpv_option_names()
        if not names:
            self.skipTest("mpv is not on PATH")
        for key, (presets, _fallback) in PRESET_SETTINGS.items():
            for name, props in presets.items():
                for option in props:
                    with self.subTest(setting=key, preset=name, option=option):
                        self.assertIn(option, names, "mpv has no --%s" % option)

    def test_the_deband_parameters_are_spelled_the_way_mpv_spells_them(self):
        """Guards the test above the way `tscale` guards the interpolation
        one: these four are the options whose names are nearly identical to
        each other, so a swapped suffix is both easy to write and invisible
        in a diff."""
        names = _mpv_option_names()
        if not names:
            self.skipTest("mpv is not on PATH")
        for option in ("deband", "deband-grain", "deband-range",
                       "deband-threshold", "deband-iterations"):
            self.assertIn(option, names)

    def test_the_render_quality_preset_still_matches_mpv_s_own(self):
        """`render_quality: high` is a copy of mpv's `high-quality` profile,
        written out as properties because a profile cannot be read back and
        therefore cannot be taken back (which is why the shader pack lists
        `profile` in `setting-revert-ignore`). A copy silently becomes a
        different preset than the one it is named after, so it is checked
        against the installed mpv.

        Intersection rather than equality **on purpose**: the unit suite is
        run against several mpv builds here, and the profile has gained
        options across versions. An option in one and not the other is a
        version difference; an option in both with a different value is
        drift, and that is what this fails on.
        """
        profile = _mpv_profile("high-quality")
        if not profile:
            self.skipTest("this mpv has no high-quality profile")
        ours = RENDER_QUALITY_PRESETS["high"]
        shared = set(ours) & set(profile)
        self.assertIn("scale", shared,
                      "the option doing the visible work is not in mpv's "
                      "profile any more: %r" % (profile,))
        for option in sorted(shared):
            with self.subTest(option=option):
                mine, theirs = ours[option], profile[option]
                if isinstance(mine, (int, float)) and not isinstance(mine, bool):
                    self.assertAlmostEqual(float(mine), float(theirs), places=5)
                else:
                    self.assertEqual(str(mine), str(theirs))

    def test_the_deband_defaults_this_file_asserts_against_are_still_mpv_s(self):
        """DebandLadderTest compares the presets against mpv's own defaults,
        which it holds as a literal. If mpv changes one, that comparison
        starts measuring the wrong thing -- quietly, and in the direction of
        passing."""
        exe = shutil.which("mpv")
        if not exe:
            self.skipTest("mpv is not on PATH")
        out = subprocess.run([exe, "--list-options"], check=True,
                             capture_output=True, text=True, timeout=30).stdout
        for option, expected in DebandLadderTest.MPV_DEFAULTS.items():
            m = re.search(r"\s+--%s\s+\S+.*?\(default:\s*([^)]+)\)"
                          % re.escape(option), out)
            with self.subTest(option=option):
                self.assertIsNotNone(m, "no default reported for --%s" % option)
                self.assertAlmostEqual(float(m.group(1)), float(expected),
                                       places=5)


if __name__ == "__main__":
    unittest.main()
