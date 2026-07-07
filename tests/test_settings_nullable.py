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


if __name__ == "__main__":
    unittest.main()
