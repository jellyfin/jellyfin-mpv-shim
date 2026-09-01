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

import errno
import os
import errno
import os
import sys
import unittest

from tests import _tmpdirs

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
    """A player with a real task queue.

    `put_task` is modelled rather than left off: the play path defers the
    library lookup to the action thread now, so a stand-in without it does
    not leave that path uncovered -- it makes the deferral silently a no-op
    and every "no request was made" assertion pass for the wrong reason.
    """

    def __init__(self, item=None, client=None):
        self.item, self.client = item, client
        self.tasks = []
        # Modelled for the same reason as put_task. The pack writes several
        # properties the picture settings also write -- `deband` above all,
        # which every profile pulls in through `default-setting-groups` --
        # so loading and unloading a profile has to hand the last word back
        # to the settings. A stand-in without this method would send that
        # call into `_reassert_user_settings`'s except clause and leave the
        # whole interaction green and untested.
        self.reasserts = 0

    def get_video(self):
        if self.item is None:
            return None
        return type("V", (), {"item": self.item, "client": self.client})()

    def put_task(self, fn, *a):
        self.tasks.append(lambda: fn(*a))

    def reapply_render_presets(self):
        self.reasserts += 1

    def drain(self):
        """Run what the action thread would have run."""
        tasks, self.tasks = self.tasks, []
        for t in tasks:
            t()
        return len(tasks)


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
    mgr.session_default = None
    mgr.suppressed = False
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

        path = _tmpdirs.tmpfile("shader_profiles.json", "jms-shaderscope-")
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

        path = _tmpdirs.tmpfile("shader_profiles.json", "jms-shaderscope-")
        with open(path, "w", encoding="utf-8") as fh:
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

    def test_the_play_path_never_asks_the_server(self):
        """apply_for_item runs inside _play_media, which holds the player
        lock for the whole of a playback start. run_action's non-blocking
        fast path is built on that lock being held, and the apiclient
        retries an unresponsive server for about two and a half minutes —
        so the request goes to the action thread, not here."""
        m = make_manager()
        m.overrides.set("library", "srv/lib1", "b")
        with mock.patch.object(m, "load_profile", return_value=True):
            m.apply_for_item(EPISODE)
        self.assertEqual(m.api.calls, [], "asked the server under the lock")
        self.assertEqual(len(m.playerManager.tasks), 1,
                         "and did not schedule the lookup either")

    def test_one_request_per_series_however_many_episodes(self):
        m = make_manager()
        m.overrides.set("library", "srv/lib1", "b")
        with mock.patch.object(m, "load_profile", return_value=True):
            for n in range(4):
                m.apply_for_item(dict(EPISODE, Id="ep%d" % n))
                m.playerManager.drain()
        self.assertEqual(m.api.calls, ["show1"])

    def test_a_server_that_cannot_answer_is_asked_once(self):
        m = make_manager(library=None)
        m.overrides.set("library", "srv/lib1", "b")
        for _n in range(3):
            m.apply_for_item(EPISODE)
            m.playerManager.drain()
        self.assertEqual(m.api.calls, ["show1"])

    def test_a_downloaded_item_needs_no_request_at_all(self):
        """The library is recorded in the catalog at download time, so an
        offline play resolves its scope with the server away — which is
        what stopped the play path reaching for the previous item's client
        to ask a server it already knew was unreachable."""
        m = make_manager()
        m.overrides.set("library", "srv/lib1", "b")
        with mock.patch.object(m, "_catalog_library_id", return_value="lib1"), \
                mock.patch.object(m, "load_profile",
                                  return_value=True) as load:
            m.apply_for_item(EPISODE)
        self.assertEqual(m.api.calls, [])
        self.assertEqual(m.playerManager.tasks, [])
        load.assert_called_once_with("b")   # ...and the override applied


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
        m.session_default = "a"
        with mock.patch.object(m, "load_profile",
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
        m.session_default = "a"
        m.overrides.set("series", "srv/show1", None)
        with mock.patch.object(m, "unload_profile") as unload:
            m.apply_for_item(EPISODE)
        unload.assert_called_once_with()

    def test_the_same_item_over_and_over_reloads_nothing(self):
        """Three passes, not one. Reapplying a whole profile between every
        two files writes every default and every setting again for
        nothing, and a one-step test cannot see it."""
        m = make_manager()
        m.current_profile = "a"
        m.session_default = "a"
        with mock.patch.object(m, "load_profile") as load, \
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
        m.session_default = "a"
        if True:
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
        m.session_default = "a"
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
        with mock.patch.object(video_profile.settings, "save"), \
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

    def test_remember_off_still_means_for_this_session(self):
        """The regression the review caught.

        `menu_handle` loaded the profile, skipped the settings write because
        "Remember Last Used Profile" was off, then re-resolved the default
        scope from `shader_pack_profile` -- the value that had deliberately
        not been written. It was None, so the re-resolve unloaded what had
        just been loaded and the user watched their pick revert. `remember`
        off meant "for zero items" rather than "for this session".
        """
        m = make_manager()
        with mock.patch.object(video_profile.settings,
                               "shader_pack_remember", False), \
                mock.patch.object(video_profile.settings, "save") as save, \
                mock.patch.object(m, "unload_profile") as unload, \
                mock.patch.object(m, "load_profile",
                                  side_effect=lambda n, **k: (
                                      setattr(m, "current_profile", n)
                                      or True)) as load:
            m.set_scope_profile(EPISODE, "default", "a")
        load.assert_called_once_with("a")
        unload.assert_not_called()
        self.assertEqual(m.current_profile, "a")
        save.assert_not_called()          # ...and still not persisted

    def test_the_escape_hatch_survives_an_item_boundary(self):
        """`k` unloaded the profile, and the next episode's resolve put the
        override's profile straight back -- on the one key whose entire
        purpose is recovering from a profile that breaks playback."""
        m = make_manager()
        m.overrides.set("series", "srv/show1", "b")
        m.current_profile = "b"
        m.suppressed = True               # what _on_kill_shader_key sets
        with mock.patch.object(m, "load_profile") as load, \
                mock.patch.object(m, "unload_profile") as unload:
            for _n in range(3):           # three episodes, not one
                m.apply_for_item(EPISODE)
        load.assert_not_called()
        # ...and a deliberate pick brings it back.
        m.current_profile = None
        with mock.patch.object(video_profile.settings, "save"), \
                mock.patch.object(m, "load_profile",
                                  return_value=True) as load:
            m.set_scope_profile(EPISODE, "series", "b")
        self.assertFalse(m.suppressed)
        load.assert_called_once_with("b")


class SettingsKeepTheLastWordTest(unittest.TestCase):
    """The pack and the picture settings write some of the same properties.

    ``pack.json`` lists ``deband-default`` under ``default-setting-groups``,
    so **every** profile turns debanding on and every unload turns it back
    off -- to whatever mpv had when ``VideoProfileManager`` snapshotted it,
    which is not the ``deband`` setting. Before the reassert existed, picking
    an upscaler mid-film and dropping it again silently took the user's
    debanding with it for the rest of the film, because ``_play_media`` does
    not run again until the next item.
    """

    def _mgr(self):
        m = make_manager()
        m.profiles = {"a": {"displayname": "A"}}
        return m

    def test_unloading_a_profile_hands_the_settings_back(self):
        m = self._mgr()
        m.unload_profile()
        self.assertEqual(m.playerManager.reasserts, 1)

    def test_loading_a_profile_hands_the_settings_back(self):
        """After the pack's writes, not before: the settings outrank the
        pack's bundled defaults while they are set, and a reassert that ran
        first would be overwritten by the very group it exists to outrank."""
        m = self._mgr()
        order = []
        m.playerManager.reapply_render_presets = lambda: order.append("settings")
        real = m.process_setting_group

        def spy(*a, **k):
            order.append("pack")
            return real(*a, **k)

        m.process_setting_group = spy
        m.default_groups = ["deband-default"]
        m.groups = {"deband-default": {"settings": [["deband", True]]}}
        m.defaults = {"deband": False}
        self.assertTrue(m.load_profile("a"))
        self.assertEqual(order[-1], "settings", order)
        self.assertIn("pack", order)

    def test_a_player_that_cannot_reassert_does_not_break_the_profile(self):
        """The profile has already been applied by the time this runs, so a
        failure to reassert a preference must not turn a working load into
        an error the menu reports."""
        m = self._mgr()
        m.playerManager.reapply_render_presets = mock.Mock(
            side_effect=RuntimeError("mpv went away"))
        self.assertTrue(m.load_profile("a"))
        m.unload_profile()          # and this must not raise either


if __name__ == "__main__":
    unittest.main()


class SavingIsAtomicTest(unittest.TestCase):
    """A failed save must not destroy the overrides that were already there.

    `open(path, "w")` truncates the existing file before a byte is written, so
    ENOSPC -- or a crash, or a kill -- mid-`json.dump` left an empty or
    fragmentary file. `load()` then treats it as unreadable and silently
    ignores EVERY override: the blast radius is the whole store, not the key
    being written, and the user finds out at the next launch.

    `conf.py:save` and `users.py:save` have written tmp-then-replace all
    along, and `input_conf.py` carries a comment warning about exactly this
    hazard. This was the last non-atomic whole-file write in the tree.

    `tests/test_shader_scopes.py::test_a_broken_file_is_not_fatal` covers
    `load()` against an already-corrupt file. Nothing covered `save()` being
    able to *produce* one.
    """

    def _store(self):
        from jellyfin_mpv_shim import shader_overrides

        path = os.path.join(_tmpdirs.tmpdir(), "shader_profiles.json")
        store = shader_overrides.ShaderOverrides(path)
        store.set("series", "s1", "anime4k")
        store.set("library", "lib1", "rtx-vsr")
        return store, path

    def test_a_failed_write_leaves_the_previous_overrides_intact(self):
        from unittest import mock

        from jellyfin_mpv_shim import shader_overrides

        store, path = self._store()

        def boom(*_a, **_k):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("json.dump", side_effect=boom):
            store.set("series", "s2", "nnedi3")

        reopened = shader_overrides.ShaderOverrides(path)
        self.assertEqual(reopened.get("series", "s1"), "anime4k",
                         "a failed save destroyed the whole override store")
        self.assertEqual(reopened.get("library", "lib1"), "rtx-vsr")

    def test_a_failed_write_leaves_no_debris(self):
        from unittest import mock

        store, path = self._store()

        def boom(*_a, **_k):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("json.dump", side_effect=boom):
            store.set("series", "s2", "nnedi3")
        leftovers = [n for n in os.listdir(os.path.dirname(path))
                     if n != os.path.basename(path)]
        self.assertEqual(leftovers, [],
                         "a half-written temp file was left behind")

    def test_a_failed_save_is_reported(self):
        """`set`/`clear` returned True unconditionally, so the menu confirmed
        a change that never reached disk."""
        from unittest import mock

        store, _path = self._store()

        def boom(*_a, **_k):
            raise OSError(errno.ENOSPC, "No space left on device")

        with mock.patch("json.dump", side_effect=boom):
            self.assertFalse(store.set("series", "s2", "nnedi3"))

    def test_an_ordinary_save_still_round_trips(self):
        """The control: atomicity must not cost the feature."""
        from jellyfin_mpv_shim import shader_overrides

        store, path = self._store()
        self.assertTrue(store.set("series", "s3", "fsr"))
        reopened = shader_overrides.ShaderOverrides(path)
        self.assertEqual(reopened.get("series", "s3"), "fsr")
        self.assertTrue(store.clear("series", "s3"))
        self.assertEqual(
            shader_overrides.ShaderOverrides(path).get("series", "s3"),
            shader_overrides.UNSET)
