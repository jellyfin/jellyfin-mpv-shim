"""Per-user display preferences shared with jellyfin-web.

The encoding is the load-bearing part: these live in the *same*
DisplayPreferences CustomPrefs blob as the home layout and the guide
settings, and jellyfin-web reads them as strings. Get that wrong and the
setting silently reverts every time the user opens the web client.
"""

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser import user_prefs  # noqa: E402


class ResolveTest(unittest.TestCase):
    def test_an_untouched_server_gets_webs_default(self):
        """False -- web made series artwork the default because a Next Up
        still is a spoiler for the episode you have not watched."""
        self.assertFalse(user_prefs.resolve_prefs({})["episode_images"])

    def test_none_is_not_a_crash(self):
        self.assertFalse(user_prefs.resolve_prefs(None)["episode_images"])

    def test_the_string_true_is_true(self):
        prefs = user_prefs.resolve_prefs(
            {user_prefs.EPISODE_IMAGES_KEY: "true"})
        self.assertTrue(prefs["episode_images"])

    def test_a_json_boolean_is_accepted_too(self):
        """Not what web writes, but some clients do, and reading one as
        False would look like the setting had reverted."""
        prefs = user_prefs.resolve_prefs(
            {user_prefs.EPISODE_IMAGES_KEY: True})
        self.assertTrue(prefs["episode_images"])

    def test_an_empty_string_is_the_default_not_false(self):
        prefs = user_prefs.resolve_prefs({user_prefs.EPISODE_IMAGES_KEY: ""})
        self.assertFalse(prefs["episode_images"])

    def test_anything_else_is_false(self):
        for junk in ("false", "TRUE ", "1", "yes", 0):
            with self.subTest(junk=junk):
                got = user_prefs.resolve_prefs(
                    {user_prefs.EPISODE_IMAGES_KEY: junk})["episode_images"]
                self.assertEqual(got, str(junk).strip().lower() == "true")


class WriteTest(unittest.TestCase):
    def test_booleans_go_out_as_strings(self):
        """jellyfin-web wrote these with val.toString() and reads them back
        through toBoolean. A JSON boolean reads as false there, so the user
        would see the setting revert whenever they opened web."""
        for value, expected in ((True, "true"), (False, "false")):
            with self.subTest(value=value):
                out = user_prefs.prefs_to_custom({"episode_images": value})
                self.assertEqual(out[user_prefs.EPISODE_IMAGES_KEY], expected)
                self.assertIsInstance(
                    out[user_prefs.EPISODE_IMAGES_KEY], str)

    def test_a_round_trip_is_lossless(self):
        for value in (True, False):
            with self.subTest(value=value):
                custom = user_prefs.prefs_to_custom(
                    {"episode_images": value})
                self.assertEqual(
                    user_prefs.resolve_prefs(custom)["episode_images"], value)

    def test_it_writes_only_its_own_key(self):
        """The saver read-modify-writes the whole DTO; this returning a
        stray key would clobber the home layout in the same blob."""
        self.assertEqual(set(user_prefs.prefs_to_custom({"episode_images": 1})),
                         {user_prefs.EPISODE_IMAGES_KEY})


class InheritTest(unittest.TestCase):
    """The double negative is web's, and it is the easiest thing here to get
    backwards: the setting is *use episode images*, the renderer wants
    *inherit series artwork*."""

    def test_the_default_inherits(self):
        self.assertTrue(user_prefs.inherit_artwork({}))

    def test_using_episode_images_stops_inheriting(self):
        self.assertFalse(
            user_prefs.inherit_artwork({"episode_images": True}))

    def test_no_prefs_at_all_inherits(self):
        """The offline source has none; it must behave like the default
        rather than flip the home screen to episode stills."""
        self.assertTrue(user_prefs.inherit_artwork(None))


if __name__ == "__main__":
    unittest.main()
