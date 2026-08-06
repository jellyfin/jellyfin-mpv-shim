"""Permissions the server grants separately, and the UI that has to follow.

SyncPlay and Live TV *recording* are granted independently of everything
else, and the shim offered both to everyone — so a user without them got a
button that could only fail, with a message ("Could not join the SyncPlay
group.", a bare 403) indistinguishable from a network problem. See
`docs/PERMISSION_GAPS.md`.

Two rules run through all of it and both are asserted here as much as the
hiding is:

* **Fail open.** Only an answer the server actually gave closes a gate. A
  policy that could not be fetched, a source that does not answer the
  question, a test double without the method — all of those leave the
  feature exactly where it was. Taking a working button away because a
  request failed is a worse bug than the one being fixed.
* **SyncPlayAccess is three-valued.** `JoinGroups` may join a group somebody
  else made and may not make one, so the dialog is reachable and the New
  Group button is not. Treating it as a boolean gets one of those wrong.
"""

import sys
import unittest

# The app parses argv the first time anything resolves the config directory,
# which importing the shell harness does. Under `discover` some earlier
# module has already neutralised it; running this one alone, nothing has.
sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim import user_policy  # noqa: E402


class _Client:
    """The shape `policy_for` needs: `client.jellyfin.get_user()`."""

    def __init__(self, policy=None, raises=False):
        self.calls = 0
        self._policy = policy
        self._raises = raises
        outer = self

        class _Api:
            def get_user(self):
                outer.calls += 1
                if outer._raises:
                    raise RuntimeError("server said no")
                return {"Policy": outer._policy} if outer._policy is not None \
                    else {}

        self.jellyfin = _Api()


class PolicyFetch(unittest.TestCase):

    def test_it_is_read_once_and_cached_on_the_client(self):
        """One request per server per session. The consumers ask on every
        repaint — the top bar asks whenever it draws — so an uncached read
        would be a request per frame."""
        client = _Client({"SyncPlayAccess": "JoinGroups"})
        for _ in range(5):
            user_policy.policy_for(client)
        self.assertEqual(client.calls, 1)

    def test_refresh_asks_again(self):
        client = _Client({"SyncPlayAccess": "JoinGroups"})
        user_policy.policy_for(client)
        user_policy.policy_for(client, refresh=True)
        self.assertEqual(client.calls, 2)

    def test_a_failed_fetch_is_not_cached_as_a_no(self):
        """Caching an error would turn one bad moment into a session-long
        loss of the feature."""
        client = _Client(raises=True)
        self.assertEqual(user_policy.policy_for(client), {})
        self.assertTrue(user_policy.may_use_syncplay(client))
        self.assertTrue(user_policy.may_manage_live_tv(client))

    def test_no_client_at_all(self):
        self.assertEqual(user_policy.policy_for(None), {})
        self.assertTrue(user_policy.may_use_syncplay(None))


class SyncPlayAccess(unittest.TestCase):

    def _client(self, access):
        return _Client({"SyncPlayAccess": access} if access else {})

    def test_the_three_values(self):
        cases = [
            (user_policy.CREATE_AND_JOIN, True, True),
            (user_policy.JOIN_ONLY, True, False),
            (user_policy.NO_SYNCPLAY, False, False),
        ]
        for access, may_use, may_create in cases:
            with self.subTest(access=access):
                client = self._client(access)
                self.assertIs(user_policy.may_use_syncplay(client), may_use)
                self.assertIs(user_policy.may_create_syncplay_group(client),
                              may_create)

    def test_an_absent_setting_is_full_access(self):
        """An older server has no such field, and must not lose the feature
        to a client that reads its absence as a refusal."""
        client = self._client(None)
        self.assertEqual(user_policy.syncplay_access(client),
                         user_policy.CREATE_AND_JOIN)
        self.assertTrue(user_policy.may_create_syncplay_group(client))


class LiveTvManagement(unittest.TestCase):

    def test_the_flag_is_read(self):
        self.assertFalse(user_policy.may_manage_live_tv(
            _Client({"EnableLiveTvManagement": False})))
        self.assertTrue(user_policy.may_manage_live_tv(
            _Client({"EnableLiveTvManagement": True})))

    def test_an_absent_flag_is_permitted(self):
        """Absent means an answer we did not get — not a refusal. It is a
        separate permission from `EnableLiveTvAccess`, so the presence of
        that one says nothing either way."""
        self.assertTrue(user_policy.may_manage_live_tv(
            _Client({"EnableLiveTvAccess": True})))


class ContentDownloading(unittest.TestCase):
    """`EnableContentDownloading` gates `/Items/{id}/Download`, which is not
    only the Download button: it is the only path to a Photo's original
    bytes and the only path to a Book's bytes at all."""

    def test_the_flag_is_read(self):
        self.assertFalse(user_policy.may_download(
            _Client({"EnableContentDownloading": False})))
        self.assertTrue(user_policy.may_download(
            _Client({"EnableContentDownloading": True})))

    def test_an_absent_flag_is_permitted(self):
        """Fail open. Closing this one on a policy we could not read would
        send every photo through the resizer."""
        self.assertTrue(user_policy.may_download(
            _Client({"EnableLiveTvAccess": True})))

    def test_a_fetch_that_failed_is_permitted(self):
        self.assertTrue(user_policy.may_download(_Client(raises=True)))

    def test_no_client_is_permitted(self):
        self.assertTrue(user_policy.may_download(None))


class CollectionManagement(unittest.TestCase):
    """`EnableCollectionManagement` gates the whole of `CollectionController`
    — the `[Authorize]` is on the controller, not on its routes — so create,
    add and remove are one permission and one 403."""

    def test_the_flag_is_read(self):
        self.assertFalse(user_policy.may_manage_collections(
            _Client({"EnableCollectionManagement": False})))
        self.assertTrue(user_policy.may_manage_collections(
            _Client({"EnableCollectionManagement": True})))

    def test_an_absent_flag_is_permitted(self):
        self.assertTrue(user_policy.may_manage_collections(
            _Client({"EnableLiveTvAccess": True})))

    def test_a_fetch_that_failed_is_permitted(self):
        self.assertTrue(user_policy.may_manage_collections(
            _Client(raises=True)))

    def test_no_client_is_permitted(self):
        self.assertTrue(user_policy.may_manage_collections(None))

    def test_being_an_administrator_is_not_the_question(self):
        """jellyfin-web reads this as `IsAdministrator ||
        EnableCollectionManagement`. The API does not: `UserPermissionHandler`
        asks `HasPermission` and stops, so an admin without the flag is
        refused like anybody else and offering the button lies to them. That
        spelling is right for `BoxSet.IsAuthorizedToDelete`, which really
        does bypass, and wrong for the endpoint this button calls.
        """
        self.assertFalse(user_policy.may_manage_collections(
            _Client({"IsAdministrator": True,
                     "EnableCollectionManagement": False})))


class TheOfflineSourceAnswers(unittest.TestCase):
    """The offline source is what the online one falls back TO, so a missing
    method turns a degraded screen into an AttributeError on the fallback
    path — the same reasoning `has_live_tv` is declared there for."""

    def test_it_declares_both(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import OfflineLibrarySource

        for name in ("syncplay_access", "can_manage_live_tv",
                     "can_manage_collections", "has_live_tv"):
            with self.subTest(name=name):
                self.assertTrue(callable(getattr(OfflineLibrarySource, name, None)))

    def test_there_is_nobody_to_sync_with_offline(self):
        from jellyfin_mpv_shim.mpvtk_browser.repository import OfflineLibrarySource

        source = OfflineLibrarySource.__new__(OfflineLibrarySource)
        self.assertEqual(source.syncplay_access("srv1"),
                         user_policy.NO_SYNCPLAY)
        self.assertFalse(source.can_manage_live_tv("srv1"))
        self.assertFalse(source.can_manage_collections("srv1"))


class TheTopBarButton(unittest.TestCase):
    """`nav-syncplay` leads to a dialog whose join/create then 403s."""

    def _bar(self, access):
        from tests._shell_harness import build_scene, ids
        from tests.test_live_tv import browser

        b = browser()
        if access is not None:
            b.source.syncplay_access = lambda _srv, a=access: a
        return ids(build_scene(b)[0])

    def test_it_is_there_by_default(self):
        self.assertIn("nav-syncplay", self._bar(None))

    def test_full_access_keeps_it(self):
        self.assertIn("nav-syncplay", self._bar(user_policy.CREATE_AND_JOIN))

    def test_join_only_keeps_it(self):
        """`JoinGroups` can use the dialog — only creating is refused."""
        self.assertIn("nav-syncplay", self._bar(user_policy.JOIN_ONLY))

    def test_a_refusal_removes_it(self):
        self.assertNotIn("nav-syncplay", self._bar(user_policy.NO_SYNCPLAY))

    def test_a_source_that_cannot_answer_keeps_it(self):
        from tests._shell_harness import build_scene, ids
        from tests.test_live_tv import browser

        b = browser()

        def boom(_srv):
            raise RuntimeError("no")

        b.source.syncplay_access = boom
        self.assertIn("nav-syncplay", ids(build_scene(b)[0]))


class ThePlayerSideButtons(unittest.TestCase):
    """The HUD's SyncPlay button and the OSD menu's row.

    Asked of the *player's* client rather than the browser's source: these
    are up while something is playing, and what is playing decides which
    server the question is about.
    """

    def _bridge(self, access):
        from jellyfin_mpv_shim.osc_bridge import OscBridge
        from tests.test_osc_bridge import FakePlayerManager, FakeVideo

        pm = FakePlayerManager(FakeVideo())
        pm.get_current_client = lambda: _Client({"SyncPlayAccess": access})
        return OscBridge(pm), pm

    def test_the_hud_button_goes(self):
        """`None` from `_syncplay` is what removes the button entirely —
        hud.py draws it only `if syncplay is not None`."""
        bridge, _pm = self._bridge(user_policy.NO_SYNCPLAY)
        self.assertNotIn("syncplay", bridge.build_state())

    def test_the_hud_button_stays_for_join_only(self):
        bridge, _pm = self._bridge(user_policy.JOIN_ONLY)
        self.assertIn("syncplay", bridge.build_state())

    def test_a_player_that_cannot_answer_keeps_it(self):
        """`FakePlayerManager` has no `get_current_client` at all, which is
        the inconclusive case: the button stays."""
        from jellyfin_mpv_shim.osc_bridge import OscBridge
        from tests.test_osc_bridge import FakePlayerManager, FakeVideo

        bridge = OscBridge(FakePlayerManager(FakeVideo()))
        self.assertIn("syncplay", bridge.build_state())

    def test_the_osd_menu_asks_the_same_question(self):
        """Two entry points to one dialog; they must not disagree about who
        may reach it."""
        import inspect

        from jellyfin_mpv_shim.menu import OSDMenu

        source = inspect.getsource(OSDMenu.show_menu)
        self.assertIn("_may_syncplay()", source,
                      "the OSD menu offers SyncPlay without consulting the "
                      "permission the HUD button consults")


class TheNewGroupButton(unittest.TestCase):
    """The whole reason `SyncPlayAccess` is not a boolean."""

    def _dialog(self, access):
        from tests._shell_harness import build_scene, ids
        from tests.test_live_tv import browser

        b = browser()
        b.source.syncplay_access = lambda _srv, a=access: a
        b._show_syncplay("srv1", [], None)
        return ids(build_scene(b)[0])

    def test_full_access_can_create(self):
        self.assertIn("sp-new", self._dialog(user_policy.CREATE_AND_JOIN))

    def test_join_only_cannot(self):
        found = self._dialog(user_policy.JOIN_ONLY)
        self.assertNotIn("sp-new", found)
        # ...but the dialog itself is still there and still usable.
        self.assertIn("sp-close", found)


class TheLiveTvTabs(unittest.TestCase):
    """Every tab, to everyone who can see Live TV at all.

    `EnableLiveTvManagement` gates changing the DVR, not reading it. Schedule
    and Series were hidden without it, which took away information the server
    itself hands out: both endpoints answer 200 for an account with no
    management permission (measured against a real server; the 403 is on the
    writes). jellyfin-web's `getTabs` consults no policy either and gates the
    mutating context-menu entries instead.
    """

    def _tabs(self, may_manage):
        from tests.test_live_tv import browser, open_live_tv

        b = browser()
        b.source.can_manage_live_tv = lambda _srv: may_manage
        page = open_live_tv(b)
        return [key for key, _label in page._tabs()]

    def test_all_six_with_management(self):
        self.assertEqual(len(self._tabs(True)), 6)

    def test_all_six_without_it_as_well(self):
        """What is going to record is worth knowing whether or not you may
        change it. The actions are what go — see TheTimerEditor."""
        self.assertEqual(sorted(self._tabs(False)),
                         sorted(self._tabs(True)))

    def test_watching_recordings_survives(self):
        """The permission is about managing recordings, not about playing
        the ones that exist."""
        tabs = self._tabs(False)
        for key in ("programs", "guide", "channels", "recordings"):
            self.assertIn(key, tabs)

    def test_the_scheduling_tabs_are_reachable_by_route(self):
        """The route carries `_tab`; a remote or a deep link can land on
        Schedule directly, and it must not bounce to Programs."""
        from tests.test_live_tv import browser, open_live_tv

        b = browser()
        b.source.can_manage_live_tv = lambda _srv: False
        for tab in ("schedule", "series"):
            with self.subTest(tab):
                page = open_live_tv(b, tab=tab)
                self.assertEqual(page._current_tab(), tab)

    def test_an_unknown_tab_still_falls_back(self):
        """The guard that outlived the permission it was added for: without
        it the tab bar shows nothing selected and the body draws a screen
        with no way out of it."""
        from tests.test_live_tv import browser, open_live_tv

        b = browser()
        page = open_live_tv(b, tab="nonsense")
        self.assertEqual(page._current_tab(), page.DEFAULT_TAB)

    def test_a_source_that_cannot_answer_keeps_them(self):
        from tests.test_live_tv import browser, open_live_tv

        b = browser()
        if hasattr(b.source, "can_manage_live_tv"):
            del b.source.can_manage_live_tv
        page = open_live_tv(b)
        self.assertEqual(len(page._tabs()), 6)


class TheTimerEditor(unittest.TestCase):
    """Opening a scheduled recording without permission to change it.

    The dialog is where the information lives — channel, air time, padding,
    which episodes a series rule keeps — so it still opens. What goes is
    everything that would be answered with a 403.
    """

    def _dialog(self, may_manage, series=True):
        from tests.test_live_tv import browser, open_live_tv
        from jellyfin_mpv_shim.mpvtk.layout import layout

        b = browser()
        b.source.can_manage_live_tv = lambda _srv: may_manage
        page = open_live_tv(b, "series" if series else "schedule")
        if series:
            page._open_series_timer({"Id": "st1"})
        else:
            page._open_timer({"Id": "tm1"})
        nodes, _h = layout(b.build((1280, 720)), 1280, 720)
        return {n.get("id"): n for n in nodes if n.get("id")}

    def test_the_form_is_still_shown(self):
        """Read-only, not withheld: the settings are the reason to open it."""
        nodes = self._dialog(False)
        for node_id in ("tm-showtype", "tm-channels", "tm-airtime",
                        "tm-keep", "tm-pre", "tm-post"):
            with self.subTest(node_id):
                self.assertIn(node_id, nodes)

    def test_nothing_that_would_be_refused_is_offered(self):
        nodes = self._dialog(False)
        self.assertNotIn("tm-save", nodes)
        self.assertNotIn("tm-cancel", nodes)

    def test_close_is_still_there(self):
        """A dialog you cannot dismiss is worse than one you cannot use."""
        self.assertIn("tm-close", self._dialog(False))

    def test_the_controls_are_disabled(self):
        """Left live, they would take an edit that Save is not there to
        apply — a form that silently discards what you type."""
        nodes = self._dialog(False)
        for node_id in ("tm-showtype", "tm-channels", "tm-airtime",
                        "tm-keep", "tm-pre", "tm-post"):
            with self.subTest(node_id):
                self.assertTrue(nodes[node_id].get("dis"),
                                "%s is still editable" % node_id)

    def test_with_permission_it_is_an_editor_again(self):
        nodes = self._dialog(True)
        self.assertIn("tm-save", nodes)
        self.assertIn("tm-cancel", nodes)
        self.assertFalse(nodes["tm-pre"].get("dis"))

    def test_a_single_timer_is_read_only_too(self):
        nodes = self._dialog(False, series=False)
        self.assertIn("tm-pre", nodes)
        self.assertTrue(nodes["tm-pre"].get("dis"))
        self.assertNotIn("tm-cancel", nodes)

    def test_it_fails_open(self):
        """Same doctrine as everywhere else: only an answer the server gave
        closes a gate. A source that cannot answer leaves it an editor."""
        from tests.test_live_tv import browser, open_live_tv
        from jellyfin_mpv_shim.mpvtk.layout import layout

        b = browser()
        if hasattr(b.source, "can_manage_live_tv"):
            del b.source.can_manage_live_tv
        page = open_live_tv(b, "series")
        page._open_series_timer({"Id": "st1"})
        nodes, _h = layout(b.build((1280, 720)), 1280, 720)
        self.assertIn("tm-save", {n.get("id") for n in nodes})


class TheRecordButtons(unittest.TestCase):
    """`can_record` now answers two questions — can the apiclient, and may
    this user — and both have to say yes."""

    def test_it_still_fails_open(self):
        from tests.test_live_tv import browser

        self.assertTrue(browser()._actions.can_record("srv1"))

    def test_the_permission_closes_it(self):
        from tests.test_live_tv import browser

        b = browser()
        b.source.can_manage_live_tv = lambda _srv: False
        self.assertFalse(b._actions.can_record("srv1"))

    def test_the_apiclient_probe_still_closes_it(self):
        """The older gate has to keep working: a user with every permission
        on an apiclient that cannot schedule is still offered nothing."""
        from tests.test_live_tv import FakeController, browser

        controller = FakeController()
        controller.live_tv_apis = lambda: False
        b = browser(controller=controller)
        b.source.can_manage_live_tv = lambda _srv: True
        self.assertFalse(b._actions.can_record("srv1"))

    def test_no_server_in_hand_fails_open(self):
        """Callers that do not know which server they are asking about get
        the old behaviour rather than a guess."""
        from tests.test_live_tv import browser

        b = browser()
        b.source.can_manage_live_tv = lambda _srv: False
        self.assertTrue(b._actions.can_record())


class TheCollectionAffordances(unittest.TestCase):
    """Add to Collection and Remove from Collection, against the permission.

    Both call `CollectionController`, which is gated in one piece, so both
    answer to one question. What must NOT move with them is **Add to
    Playlist**: `PlaylistController` carries no such policy, so a user who
    cannot touch a collection can still make a playlist, and taking that
    away would be a second bug wearing the first one's fix.
    """

    def _browser(self, may_manage=None, edit_apis=True):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import FakeController, FakeSource, _SyncPool

        controller = FakeController()
        controller.edit_apis = lambda: edit_apis
        source = FakeSource()
        source.get_collections = lambda srv: [{"Id": "c1", "Name": "Set"}]
        source.get_playlists = lambda srv: [{"Id": "p1", "Name": "List"}]
        if may_manage is not None:
            source.can_manage_collections = lambda _srv: may_manage
        browser = MpvtkBrowser(app=None, source=source, controller=controller)
        browser._pool = _SyncPool()
        browser.server = "srv1"
        return browser

    def _tile_actions(self, browser):
        browser.nav_stack = [{"kind": "grid", "server": "srv1",
                              "parent_id": "c1", "parent_type": "BoxSet"}]
        return [entry[2] for entry in browser._tile_menu_entries(
            {"Id": "m1", "Type": "Movie"})]

    def _add_to_handlers(self, browser):
        from tests._shell_harness import build_scene

        browser._open_add_to({"Id": "m1", "Type": "Movie"})
        _nodes, handlers = build_scene(browser)
        return handlers

    # -- the gate ----------------------------------------------------------

    def test_a_refusal_takes_remove_from_collection_away(self):
        self.assertNotIn("uncollect", self._tile_actions(
            self._browser(may_manage=False)))

    def test_a_refusal_takes_the_collections_button_away(self):
        self.assertNotIn("add-collections",
                         self._add_to_handlers(self._browser(may_manage=False)))

    def test_permission_keeps_both(self):
        self.assertIn("uncollect", self._tile_actions(
            self._browser(may_manage=True)))
        self.assertIn("add-collections",
                      self._add_to_handlers(self._browser(may_manage=True)))

    # -- fail open ---------------------------------------------------------

    def test_a_source_that_cannot_answer_keeps_both(self):
        """A test double or an older source without the method leaves the
        affordances exactly where they were."""
        self.assertIn("uncollect", self._tile_actions(self._browser()))
        self.assertIn("add-collections",
                      self._add_to_handlers(self._browser()))

    def test_a_source_that_raises_keeps_both(self):
        browser = self._browser()

        def boom(_srv):
            raise RuntimeError("server said no")

        browser.source.can_manage_collections = boom
        self.assertIn("uncollect", self._tile_actions(browser))
        self.assertIn("add-collections", self._add_to_handlers(browser))

    def test_no_server_in_hand_fails_open(self):
        browser = self._browser(may_manage=False)
        self.assertTrue(browser._actions.can_manage_collections())

    # -- what must not move with it ---------------------------------------

    def test_the_playlist_half_survives_a_refusal(self):
        """The whole dialog is Add to Playlist; the collections picker is a
        door out of it. Gating the door must not shut the room."""
        from tests._shell_harness import build_scene, ids

        browser = self._browser(may_manage=False)
        browser._open_add_to({"Id": "m1", "Type": "Movie"})
        nodes, handlers = build_scene(browser)
        self.assertIn("add-newname", ids(nodes), "the playlist name box went")
        self.assertIn("add-create", handlers, "the playlist Create went")
        self.assertIn("add-pl-0", handlers, "the playlist picker went")

    def test_add_to_playlist_is_still_offered_on_the_tile(self):
        self.assertIn("addto", self._tile_actions(
            self._browser(may_manage=False)))

    # -- the older gate ----------------------------------------------------

    def test_an_apiclient_that_cannot_edit_still_closes_it(self):
        """`can_edit` is the other half and has to keep working: every
        permission on an apiclient too old to call these is still nothing."""
        browser = self._browser(may_manage=True, edit_apis=False)
        self.assertNotIn("uncollect", self._tile_actions(browser))
        self.assertFalse(browser._actions.can_manage_collections("srv1"))


if __name__ == "__main__":
    unittest.main()
