"""Per-library and per-series shader profiles (#15).

There is no one right Anime4K profile -- there is a right one per kind of
source -- so a profile can be pinned to a series or to a library, and the
narrowest scope that speaks wins.

Two things here are easy to get wrong in ways nothing else would notice:
the difference between "this scope says nothing" and "this scope says no
shaders", and the *cost* of resolving which library an item is in. Both get
their own tests, and the multi-step ones exist because the failure shape
this codebase keeps producing is state feeding back into its own input.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from unittest import mock                                      # noqa: E402

from jellyfin_mpv_shim import video_profile                    # noqa: E402
from jellyfin_mpv_shim.shader_overrides import (               # noqa: E402
    SCOPES, UNSET, ShaderOverrides, key_for)
from jellyfin_mpv_shim.video_profile import (                  # noqa: E402
    VideoProfileManager)


EPISODE = {"Id": "ep1", "ServerId": "srv", "SeriesId": "show1"}
FILM = {"Id": "mv1", "ServerId": "srv"}


class FakeApi:
    def __init__(self, library="lib1"):
        self.library = library
        self.calls = []

    def get_ancestors(self, item_id):
        self.calls.append(item_id)
        if self.library is None:
            return []
        return [{"Type": "Season", "Id": "se1"},
                {"Type": "CollectionFolder", "Id": self.library},
                {"Type": "UserRootFolder", "Id": "root"}]


class FakeClient:
    def __init__(self, api):
        self.jellyfin = api


class FakePlayerManager:
    def __init__(self, item=None, client=None):
        self.item, self.client = item, client

    def get_video(self):
        if self.item is None:
            return None
        return type("V", (), {"item": self.item, "client": self.client})()


def make_manager(item=EPISODE, library="lib1", path=None):
    api = FakeApi(library)
    mgr = VideoProfileManager.__new__(VideoProfileManager)
    mgr.profiles = {"a": {"displayname": "A"}, "b": {"displayname": "B"}}
    mgr.menu = None
    mgr.player = mock.Mock()
    mgr.current_profile = None
    mgr.groups, mgr.default_groups, mgr.defaults = {}, [], {}
    mgr.revert_ignore, mgr.used_settings = set(), set()
    mgr._suspended = None
    mgr.playerManager = FakePlayerManager(item, FakeClient(api))
    mgr.overrides = ShaderOverrides(path)
    mgr.active_scope = "default"
    mgr._library_ids = {}
    mgr._menu_scope = "default"
    mgr.api = api
    return mgr


class StoreTest(unittest.TestCase):
    def test_absent_and_null_are_different_answers(self):
        """The whole feature turns on this. "Nothing set here" inherits;
        "no shaders here" is an override that must not."""
        o = ShaderOverrides(None)
        self.assertIs(o.get("series", "srv/show1"), UNSET)
        o.set("series", "srv/show1", None)
        self.assertIsNone(o.get("series", "srv/show1"))
        self.assertEqual(o.resolve({"series": "srv/show1"}, "a"),
                         ("series", None))

    def test_the_narrowest_scope_that_speaks_wins(self):
        o = ShaderOverrides(None)
        keys = {"series": "srv/show1", "library": "srv/lib1"}
        self.assertEqual(o.resolve(keys, "a"), ("default", "a"))
        o.set("library", "srv/lib1", "b")
        self.assertEqual(o.resolve(keys, "a"), ("library", "b"))
        o.set("series", "srv/show1", "a")
        self.assertEqual(o.resolve(keys, "a"), ("series", "a"))
        o.clear("series", "srv/show1")
        self.assertEqual(o.resolve(keys, "a"), ("library", "b"))

    def test_the_order_is_the_scope_list(self):
        """Not a second copy of it: SCOPES is narrowest-first and resolve
        walks it, so adding a scope cannot leave the resolver behind."""
        self.assertEqual(SCOPES, ("series", "library"))

    def test_keys_carry_the_server(self):
        # Item ids are unique per server, and a multi-server setup is the
        # normal case here.
        self.assertNotEqual(key_for("s1", "x"), key_for("s2", "x"))
        self.assertIsNone(key_for("", "x"))
        self.assertIsNone(key_for("s1", None))

    def test_it_survives_the_file(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "shader_profiles.json")
        o = ShaderOverrides(path)
        o.set("series", "srv/show1", "b")
        o.set("library", "srv/lib1", None)
        again = ShaderOverrides(path)
        self.assertEqual(again.get("series", "srv/show1"), "b")
        self.assertIsNone(again.get("library", "srv/lib1"))
        self.assertIs(again.get("series", "srv/other"), UNSET)

    def test_a_broken_file_is_not_fatal(self):
        import os
        import tempfile

        path = os.path.join(tempfile.mkdtemp(), "shader_profiles.json")
        with open(path, "w") as fh:
            fh.write("{ not json")
        o = ShaderOverrides(path)
        self.assertEqual(o.resolve({"series": "srv/show1"}, "a"),
                         ("default", "a"))


class ResolutionCostTest(unittest.TestCase):
    """Which library an item is in takes a request. It must not be made for
    someone who has never set a library override, and it must not be made
    twice for one series."""

    def test_no_library_override_means_no_request(self):
        m = make_manager()
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", None):
            m.apply_for_item(EPISODE)
        self.assertEqual(m.api.calls, [])

    def test_one_request_per_series_however_many_episodes(self):
        m = make_manager()
        m.overrides.set("library", "srv/lib1", "b")
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", None), \
                mock.patch.object(m, "load_profile", return_value=True):
            for n in range(4):
                m.apply_for_item(dict(EPISODE, Id="ep%d" % n))
        self.assertEqual(m.api.calls, ["show1"])

    def test_a_server_that_cannot_answer_is_asked_once(self):
        m = make_manager(library=None)
        m.overrides.set("library", "srv/lib1", "b")
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", None):
            for _n in range(3):
                m.apply_for_item(EPISODE)
        self.assertEqual(m.api.calls, ["show1"])


    def test_a_warmed_cache_makes_the_row_appear_without_a_request(self):
        """The HUD's shape: it asks with force=False and warms the cache
        from the action thread. A gate on has_any() alone stays shut for
        exactly the user who has not made a library override yet -- which
        is everyone about to make their first, so the row never appeared.
        """
        m = make_manager()
        # Nothing set, so the read path asks for nothing.
        self.assertNotIn("library", m.scope_keys(EPISODE))
        # The menu opens; the action thread resolves it once.
        m.scope_keys(EPISODE, force=True)
        self.assertEqual(m.api.calls, ["show1"])
        # Now the row is there, and asking again is free.
        self.assertEqual(m.scope_keys(EPISODE).get("library"), "srv/lib1")
        self.assertEqual(m.api.calls, ["show1"])
        self.assertEqual([r[0] for r in m.scope_rows(EPISODE, force=False)],
                         ["series", "library", "default"])


class ApplyTest(unittest.TestCase):
    def test_the_series_override_is_what_plays(self):
        m = make_manager()
        m.overrides.set("series", "srv/show1", "b")
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", "a"), \
                mock.patch.object(m, "load_profile",
                                  return_value=True) as load:
            m.apply_for_item(EPISODE)
        load.assert_called_once_with("b")
        self.assertEqual(m.active_scope, "series")

    def test_a_film_has_no_series_scope(self):
        """"series -> library -> default" taken literally: a film has no
        series, so it gets no series row and cannot be given one."""
        m = make_manager(item=FILM)
        keys = m.scope_keys(FILM, force=True)
        self.assertNotIn("series", keys)
        self.assertEqual(keys.get("library"), "srv/lib1")

    def test_an_override_of_none_turns_shaders_off_for_it(self):
        m = make_manager()
        m.current_profile = "a"
        m.overrides.set("series", "srv/show1", None)
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", "a"), \
                mock.patch.object(m, "unload_profile") as unload:
            m.apply_for_item(EPISODE)
        unload.assert_called_once_with()

    def test_the_same_item_over_and_over_reloads_nothing(self):
        """Three passes, not one. Reapplying a whole profile between every
        two files writes every default and every setting again for
        nothing, and a one-step test cannot see it."""
        m = make_manager()
        m.current_profile = "a"
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", "a"), \
                mock.patch.object(m, "load_profile") as load, \
                mock.patch.object(m, "unload_profile") as unload:
            for _n in range(3):
                m.apply_for_item(EPISODE)
        load.assert_not_called()
        unload.assert_not_called()

    def test_a_still_in_the_middle_does_not_strand_the_profile(self):
        """The play path's suspension and the per-item resolution are the
        same call now, so a photo between two episodes has to leave the
        second one wearing the profile again."""
        m = make_manager()
        m.current_profile = "a"
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", "a"):
            m.suspend_for_still()
            self.assertIsNotNone(m._suspended)
            with mock.patch.object(m, "load_profile",
                                   return_value=True) as load:
                m.apply_for_item(EPISODE)
        load.assert_called_once_with("a")
        self.assertIsNone(m._suspended)


class MenuTest(unittest.TestCase):
    def test_the_scope_rows_report_each_scope_and_who_wins(self):
        m = make_manager()
        m.overrides.set("series", "srv/show1", "b")
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", "a"):
            rows = m.scope_rows(EPISODE)
        self.assertEqual([r[0] for r in rows],
                         ["series", "library", "default"])
        self.assertEqual([(r[2], r[3]) for r in rows],
                         [("b", True), (None, False), ("a", True)])

    def test_setting_the_default_does_not_outrank_an_override(self):
        """The menu says the series is in effect. If picking a default
        changed what is on screen, the menu would be lying."""
        m = make_manager()
        m.overrides.set("series", "srv/show1", "b")
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", "a"), \
                mock.patch.object(video_profile.settings, "save"), \
                mock.patch.object(m, "load_profile",
                                  return_value=True) as load:
            m.set_scope_profile(EPISODE, "default", "a")
        self.assertEqual(load.call_args[0][0], "b")

    def test_clearing_an_override_falls_back_to_the_next_scope(self):
        m = make_manager()
        m.overrides.set("library", "srv/lib1", "a")
        m.overrides.set("series", "srv/show1", "b")
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", None), \
                mock.patch.object(m, "load_profile",
                                  return_value=True) as load:
            m.clear_scope(EPISODE, "series")
        self.assertEqual(load.call_args[0][0], "a")
        self.assertIs(m.overrides.get("series", "srv/show1"), UNSET)

    def test_the_default_row_says_what_is_loaded_when_nothing_is_remembered(self):
        """With "Remember Last Used Profile" off, shader_pack_profile stays
        None while a profile is visibly running -- so reading the setting
        alone would report the default as "None (Disabled)" to somebody
        looking straight at its output."""
        m = make_manager()
        m.current_profile = "a"
        m.active_scope = "default"
        with mock.patch.object(video_profile.settings,
                               "shader_pack_profile", None):
            self.assertEqual(m._default_profile(), "a")
            # ...but once an override is what is playing, "what is loaded"
            # is not the default's value any more.
            m.active_scope = "series"
            self.assertIsNone(m._default_profile())


if __name__ == "__main__":
    unittest.main()
