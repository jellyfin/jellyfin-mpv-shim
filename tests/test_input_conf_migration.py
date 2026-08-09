"""Carrying the user's key choices into input.conf (#16).

The shim expressed "which key pauses" as a setting of its own and bound it
in Python. #16 gives those keys back to mpv — but a user who *changed* one
made a real choice, and dropping the binding without carrying it across
would silently undo it.

Two things are load-bearing and neither is obvious: where in the file it
writes, and what it declines to write at all.
"""

import os
import sys
import tempfile
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import input_conf                       # noqa: E402
from jellyfin_mpv_shim.conf import Settings                    # noqa: E402


def _settings(**kw):
    return Settings().parse_obj(kw)


class PlanTest(unittest.TestCase):
    def test_only_what_the_user_actually_set(self):
        # A default is ours to take back; a value they typed is not.
        self.assertEqual(input_conf.plan(_settings()), [])
        self.assertEqual(input_conf.plan(_settings(kb_pause="P")),
                         [("P", "cycle pause")])

    def test_a_cleared_binding_is_not_re_bound(self):
        """[iw]: setting one to null means they were parking our
        interception away. Writing it into input.conf would undo the exact
        thing this change is for."""
        for value in (None, "", "None"):
            with self.subTest(value=value):
                self.assertEqual(input_conf.plan(_settings(kb_pause=value)),
                                 [])

    def test_a_seek_carries_the_users_own_distance(self):
        got = input_conf.plan(_settings(kb_menu_up="a", seek_up=30))
        self.assertEqual(got, [("a", "seek 30")])

    def test_exactness_travels_too(self):
        got = input_conf.plan(
            _settings(kb_menu_left="z", seek_left=-2, seek_h_exact=True))
        self.assertEqual(got, [("z", "seek -2 exact")])

    def test_the_arrows_are_left_alone_when_something_shim_only_rides_them(self):
        """use_web_seek and skip_intro_on_seek have no mpv equivalent at
        all. A migration that quietly dropped a feature would be worse than
        no migration."""
        for extra in ({"use_web_seek": True}, {"skip_intro_on_seek": True}):
            with self.subTest(**extra):
                got = input_conf.plan(
                    _settings(kb_menu_up="a", kb_pause="P", **extra))
                # The pause key still migrates -- it is expressible.
                self.assertEqual(got, [("P", "cycle pause")])

    def test_a_shim_action_is_never_migrated(self):
        # kb_watched/kb_next/kb_stop name things mpv has no opinion about;
        # there is nothing to hand it.
        got = input_conf.plan(
            _settings(kb_watched="W", kb_next="N", kb_stop="Q"))
        self.assertEqual(got, [])


class WriteTest(unittest.TestCase):
    def _path(self):
        return os.path.join(tempfile.mkdtemp(), "input.conf")

    def test_it_writes_above_the_first_section(self):
        """mpv's sections run to the next header, so appending to a file
        with any section puts these bindings INSIDE it -- written, looking
        right, and never firing."""
        out = input_conf.insert_before_first_section(
            "a cycle mute\n[myprofile]\nb quit\n", "BLOCK\n")
        self.assertEqual(out, "a cycle mute\nBLOCK\n[myprofile]\nb quit\n")

    def test_a_file_with_no_section_gets_it_appended(self):
        out = input_conf.insert_before_first_section("a cycle mute\n",
                                                     "BLOCK\n")
        self.assertEqual(out, "a cycle mute\nBLOCK\n")

    def test_a_file_not_ending_in_a_newline_does_not_glue(self):
        out = input_conf.insert_before_first_section("a cycle mute",
                                                     "BLOCK\n")
        self.assertEqual(out, "a cycle mute\nBLOCK\n")

    def test_the_settings_are_cleared_so_nothing_binds_twice(self):
        path = self._path()
        s = _settings(kb_pause="P", kb_menu_up="a", seek_up=30)
        written = input_conf.migrate(s, path)
        self.assertEqual(len(written), 2)
        self.assertIsNone(s.kb_pause)
        self.assertIsNone(s.kb_menu_up)
        body = open(path, encoding="utf-8").read()
        self.assertIn("P cycle pause", body)
        self.assertIn("a seek 30", body)

    def test_it_does_not_run_twice(self):
        path = self._path()
        s = _settings(kb_pause="P")
        input_conf.migrate(s, path)
        first = open(path, encoding="utf-8").read()
        again = _settings(kb_pause="P")
        self.assertEqual(input_conf.migrate(again, path), [])
        self.assertEqual(open(path, encoding="utf-8").read(), first)
        self.assertEqual(again.kb_pause, "P",
                         "nothing was written, so nothing may be cleared")

    def test_an_unwritable_file_leaves_the_settings_alone(self):
        # The user keeps their bindings the old way rather than losing them
        # to a read-only config directory.
        s = _settings(kb_pause="P")
        self.assertEqual(
            input_conf.migrate(s, "/nonexistent-dir/input.conf"), [])
        self.assertEqual(s.kb_pause, "P")


class FieldsSetSurvivesLoadTest(unittest.TestCase):
    """The record of which keys came from the file has to reach the global
    settings object, or every question above answers "untouched".

    parse_obj built it on a throwaway and the load loop copied only the
    values, so `settings.__fields_set__` was empty however much the user
    had configured. Nothing consulted it until #16 -- which asks exactly
    that, to tell "our default, take it back" from "their choice, honour
    it".
    """

    def test_load_preserves_which_keys_the_user_set(self):
        import json

        path = os.path.join(tempfile.mkdtemp(), "conf.json")
        with open(path, "w") as fh:
            json.dump({"kb_menu_up": "a", "config_version": 4}, fh)
        s = Settings()
        s.load(path)
        self.assertIn("kb_menu_up", s.__fields_set__)
        self.assertEqual(s.kb_menu_up, "a")


if __name__ == "__main__":
    unittest.main()
