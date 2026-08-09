"""Regression tests: empty strings in nullable config fields must load as None.

The config UI submits text fields verbatim, so an emptied nullable field
arrives (and used to persist) as "". Several consumers guard with
``is not None`` rather than truthiness and crash or misbehave on "" —
``mpv_ext_ipc=""`` made the external-mpv launch hang through its whole retry
stack and then fail, ``mpv_ext_path=""`` raised out of Popen, ``lang=""``
broke locale setup at import, ``shader_pack_profile=""`` logged load errors
for a profile named "". ``allow_none`` treats "" as null so both the load
path and the UI save path (both funnel through ``parse_obj``) are covered.
"""
import unittest

from typing import Optional

from jellyfin_mpv_shim.settings_base import SettingsBase, allow_none


class NullableSettings(SettingsBase):
    from typing import Optional  # noqa: F401 (annotation source)

    # Mirrors the shapes conf.Settings uses.
    plain_str: str = "keep"
    opt_str: "Optional[str]" = None
    opt_int: "Optional[int]" = None
    opt_float: "Optional[float]" = None


# PEP 563-style: SettingsBase looks annotations up as strings via
# __annotations__, matching how conf.py declares them.
NullableSettings.__annotations__ = {
    "plain_str": "str",
    "opt_str": "Optional[str]",
    "opt_int": "Optional[int]",
    "opt_float": "Optional[float]",
}


class AllowNoneTest(unittest.TestCase):
    def test_empty_string_becomes_none(self):
        self.assertIsNone(allow_none(str)(""))
        self.assertIsNone(allow_none(int)(""))
        self.assertIsNone(allow_none(float)(""))

    def test_null_literal_and_none_still_none(self):
        self.assertIsNone(allow_none(str)("null"))
        self.assertIsNone(allow_none(str)(None))

    def test_real_values_pass_through(self):
        self.assertEqual(allow_none(str)("/tmp/sock"), "/tmp/sock")
        self.assertEqual(allow_none(int)("300"), 300)


class ParseObjNullableTest(unittest.TestCase):
    def test_empty_strings_load_as_none_for_nullables(self):
        s = NullableSettings()
        parsed = s.parse_obj({
            "opt_str": "",       # e.g. mpv_ext_ipc emptied in the config UI
            "opt_int": "",       # e.g. health_check_interval cleared
            "opt_float": "",
        })
        self.assertIsNone(parsed.opt_str)
        self.assertIsNone(parsed.opt_int)
        self.assertIsNone(parsed.opt_float)
        # The clears must be applied (present in __fields_set__), not
        # silently dropped as invalid values.
        self.assertIn("opt_str", parsed.__fields_set__)
        self.assertIn("opt_int", parsed.__fields_set__)

    def test_plain_str_keeps_empty_string(self):
        # Non-nullable strings must NOT be nulled — "" is a valid value there.
        s = NullableSettings()
        parsed = s.parse_obj({"plain_str": ""})
        self.assertEqual(parsed.plain_str, "")

    def test_real_config_shape_round_trip(self):
        # The exact keys from the report, through the real Settings class.
        from jellyfin_mpv_shim.conf import Settings

        parsed = Settings().parse_obj({
            "mpv_ext_ipc": "",
            "mpv_ext_path": "",
            "shader_pack_profile": "",
            "lang": "",
            "health_check_interval": "",
        })
        self.assertIsNone(parsed.mpv_ext_ipc)
        self.assertIsNone(parsed.mpv_ext_path)
        self.assertIsNone(parsed.shader_pack_profile)
        self.assertIsNone(parsed.lang)
        self.assertIsNone(parsed.health_check_interval)


class SettingsFormNullableTest(unittest.TestCase):
    """The settings form must be able to write None back.

    coerce() has no None branch -- float(None) raises -- so a dropdown
    offering an explicit "unset" choice (ui_scale's "Follow display") saved
    as "Invalid value" until set_setting learned which keys are nullable.
    """

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser import config as cfg
        from jellyfin_mpv_shim.conf import settings

        self.cfg = cfg
        self.settings = settings
        self._saved_save = settings.save
        self._saved_scale = settings.ui_scale
        settings.save = lambda *a, **k: None   # no config file under test

    def tearDown(self):
        self.settings.ui_scale = self._saved_scale
        self.settings.save = self._saved_save

    def test_every_ui_scale_choice_saves(self):
        for label, value in self.cfg.LABELED_ENUMS["ui_scale"]:
            with self.subTest(choice=label):
                self.assertTrue(self.cfg.set_setting("ui_scale", value))
                self.assertEqual(self.settings.ui_scale, value)

    def test_none_is_refused_for_a_non_nullable_key(self):
        self.assertFalse(self.cfg.set_setting("local_kbps", None))

    def test_is_nullable_reads_the_annotation(self):
        self.assertTrue(self.cfg.is_nullable("ui_scale"))
        self.assertFalse(self.cfg.is_nullable("osc_style"))

    def test_the_dropdown_preselects_follow_display_when_unset(self):
        """The row picks the current index by string comparison, so None
        has to match the None-valued option rather than falling back to 0
        by accident."""
        opts = self.cfg.LABELED_ENUMS["ui_scale"]
        idx = next((i for i, (_l, v) in enumerate(opts)
                    if str(v) == str(None)), None)
        self.assertIsNotNone(idx)
        self.assertIsNone(opts[idx][1])

    def test_ui_scale_is_offered_in_the_theme_section(self):
        # Interface Scale sits with the other startup-applied "look" settings
        # (theme + cover size) rather than under Interface.
        section = next(keys for name, keys in self.cfg.SECTIONS
                       if "theme" in keys)
        self.assertIn("ui_scale", section)


class KeyBindingsAreNullableTest(unittest.TestCase):
    """`kb_*` were `str`, and the documentation has always said you can set
    them to `null` to disable the shortcut.

    Those two cannot both be true: a JSON null coerced through `str` to the
    STRING "None", which `_bind_key`'s None guard could never match. It
    looked like it worked only because no keyboard produces a key named
    None, so mpv bound something unreachable instead of binding nothing.

    #16 needs the difference to be real, not incidental: "the user cleared
    this" is how the one-time migration to input.conf tells someone who
    parked our interception on purpose from someone who never touched it.
    """

    KEYS = ("kb_stop", "kb_prev", "kb_next", "kb_watched", "kb_unwatched",
            "kb_menu", "kb_menu_esc", "kb_menu_ok", "kb_menu_left",
            "kb_menu_right", "kb_menu_up", "kb_menu_down", "kb_pause",
            "kb_fullscreen", "kb_kill_shader")

    def _settings(self):
        import sys

        sys.argv = [sys.argv[0]]
        from jellyfin_mpv_shim.conf import Settings

        return Settings()

    def test_every_key_binding_is_nullable(self):
        s = self._settings()
        for key in self.KEYS:
            with self.subTest(key=key):
                # conf.py has no `from __future__ import annotations`, so
                # these arrive as real typing objects; the string spelling
                # is what a PEP 563 module would give. Accept either.
                ann = s.__class__.__annotations__[key]
                self.assertIn(
                    ann, (Optional[str], "Optional[str]"),
                    "%s cannot be cleared, which the docs say it can" % key)

    def test_null_and_empty_both_clear_a_binding(self):
        s = self._settings().parse_obj(
            {"kb_stop": None, "kb_pause": "", "kb_next": "x"})
        self.assertIsNone(s.kb_stop)
        self.assertIsNone(s.kb_pause)
        self.assertEqual(s.kb_next, "x")

    def test_clearing_is_distinguishable_from_never_touching(self):
        # The whole point: __fields_set__ says "touched", the value says
        # "and they meant to switch it off". A migration that cannot tell
        # those apart re-binds a key somebody deliberately gave back to mpv.
        s = self._settings().parse_obj({"kb_stop": None})
        self.assertIn("kb_stop", s.__fields_set__)
        self.assertIsNone(s.kb_stop)
        self.assertNotIn("kb_pause", s.__fields_set__)
        self.assertEqual(s.kb_pause, "space")

    def test_the_binder_refuses_every_spelling_of_cleared(self):
        # A config written under the old typing holds the string "None".
        import sys

        sys.argv = [sys.argv[0]]
        from jellyfin_mpv_shim.player import PlayerManager

        bound = []
        pm = PlayerManager.__new__(PlayerManager)
        pm._player = type("P", (), {
            "on_key_press": lambda self, k: (bound.append(k)
                                             or (lambda fn: fn))})()
        for cleared in (None, "", "None"):
            pm._bind_key(cleared, lambda: None)
        self.assertEqual(bound, [])
        pm._bind_key("q", lambda: None)
        self.assertEqual(bound, ["q"])


if __name__ == "__main__":
    unittest.main()
