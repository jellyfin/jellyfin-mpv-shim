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
    def test_only_what_the_user_actually_changed(self):
        # A default is ours to take back; a value they typed is not.
        self.assertEqual(input_conf.plan(_settings()), [])
        self.assertEqual(input_conf.plan(_settings(kb_pause="P")),
                         [("kb_pause", "P", "cycle pause")])

    def test_a_saved_all_default_config_migrates_nothing(self):
        """The regression [iw] found in a real input.conf: `space cycle
        pause`, `f cycle fullscreen` and the four arrows, every one of them
        mpv's own default, written back as an explicit binding for nothing.

        __fields_set__ says "this key was in the file", and save() writes
        all 186 of them — so after one save the whole config reads as
        deliberately chosen. Comparing to the class default is the honest
        question."""
        saved = Settings().dict()
        self.assertGreater(len(saved), 100, "test premise: save writes all")
        self.assertEqual(input_conf.plan(_settings(**saved)), [])

    def test_clearing_is_by_name_not_by_value(self):
        """`written` used to be a set of key STRINGS, and any setting whose
        value was in it got nulled — so `kb_pause = "right"` cleared
        `kb_menu_right`, whose binding the plan had deliberately declined to
        migrate. Dropped feature, arriving by the back door."""
        import os
        import tempfile

        s = _settings(**dict(Settings().dict(),
                             use_web_seek=True, kb_pause="right"))
        path = os.path.join(tempfile.mkdtemp(), "input.conf")
        self.assertEqual([e[0] for e in input_conf.plan(s)], ["kb_pause"])
        input_conf.migrate(s, path)
        self.assertIsNone(s.kb_pause)
        self.assertEqual(s.kb_menu_right, "right",
                         "a setting the plan declined must not be cleared")

    def test_a_cleared_binding_is_not_re_bound(self):
        """[iw]: setting one to null means they were parking our
        interception away. Writing it into input.conf would undo the exact
        thing this change is for."""
        for value in (None, "", "None"):
            with self.subTest(value=value):
                self.assertEqual(input_conf.plan(_settings(kb_pause=value)),
                                 [])

    def test_the_arrows_are_never_migrated(self):
        """An arrow is not one binding: it seeks during playback AND drives
        the OSD menu, and input.conf can express the first and not the
        second.

        Migrating one therefore either leaves the shim binding a key mpv now
        also binds (two seeks per press) or clears the setting and takes the
        menu's navigation with it — which is what the first version did,
        permanently, for anyone who had changed a seek distance. They keep
        their Python binding exactly when they differ from mpv and are given
        back untouched when they do not, which needs no migration at all.
        """
        for kw in ({"kb_menu_up": "a"}, {"seek_up": 30},
                   {"seek_h_exact": True}, {"kb_menu_left": "z",
                                            "seek_left": -2}):
            with self.subTest(**kw):
                self.assertEqual(input_conf.plan(_settings(**kw)), [])

    def test_the_pause_key_still_migrates_beside_untouchable_arrows(self):
        """The arrows are never migrated (see above), and that must not stop
        the settings that ARE expressible from moving."""
        for extra in ({"use_web_seek": True}, {"skip_intro_on_seek": True}):
            with self.subTest(**extra):
                got = input_conf.plan(
                    _settings(kb_menu_up="a", kb_pause="P", **extra))
                self.assertEqual(got, [("kb_pause", "P", "cycle pause")])

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
        s = _settings(kb_pause="P", kb_fullscreen="F")
        written = input_conf.migrate(s, path)
        self.assertEqual(len(written), 2)
        self.assertIsNone(s.kb_pause)
        self.assertIsNone(s.kb_fullscreen)
        body = open(path, encoding="utf-8").read()
        self.assertIn("P cycle pause", body)
        self.assertIn("F cycle fullscreen", body)

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
