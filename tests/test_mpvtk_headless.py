"""Headless mode: the cast screen is the only page.

The point of the flag is that plugging a mouse into a cast-target box does
not let someone browse and play the library. That is a claim about EVERY way
in, not about the obvious one — so these tests enumerate the doors found by
reading the code and check each is shut:

    playback ends -> enter_browse()          app.py
    a tile / any view calling navigate()     app.py
    remote GoHome / GoToSettings             app.py on_nav_command
    a phone's DisplayContent                 app.py display_item
    the now-playing bar's Queue button       music.py
    tray "Show Library Browser"              ui.py -> enter_browse()

A half-enforced lockdown is worse than none, because the operator believes
the box is locked. If a new route or entry point appears, the catch-all at
the bottom is what fails.

NOT a security boundary, and the tests do not pretend otherwise: anyone who
can attach input can usually edit config.json, and the tray deliberately
still reaches Settings (a documented choice). This stops accidents and
casual misuse, which is what it is for.
"""

import sys
import unittest

sys.argv = [sys.argv[0]]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests.test_mpvtk_browser_shell import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene, ids)


class HeadlessBase(unittest.TestCase):
    HEADLESS = True

    def _browser(self, **ctl):
        c = FakeController()
        for k, v in ctl.items():
            setattr(c, k, v)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=c)
        b._pool = _SyncPool()
        b.headless = self.HEADLESS
        b.server = "srv1"
        self.ctl = c
        if self.HEADLESS:
            b.show_cast()
        else:
            b.navigate({"kind": "home", "server": "srv1"}, reset=True)
        return b


class TestTheDoorsAreShut(HeadlessBase):
    def test_it_starts_on_the_cast_screen(self):
        b = self._browser()
        self.assertEqual(b.route["kind"], "cast")

    def test_navigating_to_a_library_page_is_refused(self):
        b = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1"})
        self.assertEqual(b.route["kind"], "cast",
                         "a library page was reachable in headless mode")

    def test_opening_an_item_is_refused(self):
        b = self._browser()
        b._open_item({"Id": "m1", "Name": "A Movie", "Type": "Movie"})
        self.assertEqual(b.route["kind"], "cast")

    def test_settings_is_refused(self):
        """Settings holds the server list and the headless flag itself."""
        b = self._browser()
        b._open_settings()
        self.assertEqual(b.route["kind"], "cast")

    def test_the_queue_view_is_refused(self):
        b = self._browser()
        b._open_queue()
        self.assertEqual(b.route["kind"], "cast")

    def test_a_remote_declines_home_and_settings(self):
        """Declining lets the player fall back to its own OSD menu, which is
        transport-only — rather than opening a page here."""
        b = self._browser()
        self.assertFalse(b.on_nav_command("home"))
        self.assertFalse(b.on_nav_command("settings"))
        self.assertEqual(b.route["kind"], "cast")

    def test_playback_ending_returns_to_the_cast_screen(self):
        b = self._browser()
        b.on_playstate({"stopped": False, "is_audio": False, "id": "v1",
                        "title": "Film", "position": 1, "duration": 10})
        b.on_playstate({"stopped": True})
        self.assertEqual(b.route["kind"], "cast",
                         "playback ending dropped us into the library")

    def test_enter_browse_lands_on_the_cast_screen(self):
        """The tray's "Show Library Browser" goes through here."""
        b = self._browser()
        b.nav_stack = [{"kind": "home", "server": "srv1"}]
        b.enter_browse()
        self.assertEqual(b.route["kind"], "cast")

    def test_a_phone_showing_an_item_paints_it_rather_than_opening_it(self):
        """Asserts the item is PAINTED, not merely that navigation was
        refused — those are different, and checking only the route passes
        even when display_item does nothing at all, because navigate()
        would have blocked it regardless."""
        b = self._browser()
        shown = []
        b.display_cast_item = lambda srv, item_id: shown.append(item_id)
        b.display_item("srv1", "m1")
        self.assertEqual(shown, ["m1"],
                         "DisplayContent did not reach the cast screen")
        self.assertEqual(b.route["kind"], "cast",
                         "DisplayContent opened a browsable page")

    def test_the_now_playing_bar_has_no_queue_button(self):
        """The queue is a normal route and normal routes render the nav
        chrome, so this button was a two-click path to the whole library."""
        b = self._browser()
        b.display_cast_item = lambda srv, iid: None   # no real item fetch
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "title": "Song", "position": 1, "duration": 10})
        nodes, _h = build_scene(b)
        self.assertNotIn("np-queue", ids(nodes))

    def test_music_transport_still_works(self):
        """Locking the library must not cost you playback control."""
        b = self._browser()
        b.display_cast_item = lambda srv, iid: None
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "title": "Song", "position": 1, "duration": 10})
        nodes, h = build_scene(b)
        for nid in ("np-pp", "np-next", "np-prev", "np-seek", "np-vol"):
            self.assertIn(nid, ids(nodes), "%s went missing" % nid)
        h["np-pp"]["click"]()
        self.assertIn("toggle_pause", [c[0] for c in self.ctl.transport])

    def test_the_cast_screen_shows_no_nav_chrome(self):
        b = self._browser()
        nodes, _h = build_scene(b)
        for nid in ("nav-home", "nav-search", "nav-settings", "nav-syncplay"):
            self.assertNotIn(nid, ids(nodes),
                             "%s is on screen in headless mode" % nid)

    def test_the_screens_headless_needs_are_still_reachable(self):
        """The lock must not brick startup: connecting and the PIN gate have
        to work, or a headless box with a slow server has no way forward."""
        b = self._browser()
        b.show_connecting()
        self.assertEqual(b.route["kind"], "connecting")
        b.show_locked()
        self.assertEqual(b.route["kind"], "locked")


class TestWithoutTheFlagNothingChanges(HeadlessBase):
    HEADLESS = False

    def test_the_library_is_reachable(self):
        b = self._browser()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1"})
        self.assertEqual(b.route["kind"], "grid")

    def test_the_queue_button_is_present(self):
        b = self._browser()
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "title": "Song", "position": 1, "duration": 10})
        nodes, _h = build_scene(b)
        self.assertIn("np-queue", ids(nodes))

    def test_a_remote_still_opens_home(self):
        b = self._browser()
        self.assertTrue(b.on_nav_command("home"))
        self.assertEqual(b.route["kind"], "home")

    def test_a_phone_still_opens_the_item_page(self):
        b = self._browser()
        b.display_item("srv1", "m1")
        self.assertEqual(b.route["kind"], "detail",
                         "DisplayContent no longer opens the item")


class TestThePathsThatBypassNavigate(HeadlessBase):
    """navigate() is the choke point, but three places assign nav_stack
    DIRECTLY and so never reach it. That is not hypothetical: it shipped.
    Setting headless=true fullscreened the window and then landed on the
    library anyway, because a successful connect calls set_source, which
    resets the stack to home itself.

    The catch-all below only exercised navigate(), so it could not see this.
    These drive the real entry points instead.
    """

    def test_a_successful_connect_does_not_land_on_the_library(self):
        """The reported bug, exactly."""
        b = self._browser()
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.route["kind"], "cast",
                         "connecting dropped a headless box on the library")

    def test_a_reconnect_does_not_either(self):
        b = self._browser()
        b.set_source(FakeSource(), server_uuid="srv1", keep_place=True)
        self.assertEqual(b.route["kind"], "cast")

    def test_a_fresh_browser_starts_on_the_cast_screen(self):
        """Before anything calls show_cast(): the initial stack is assigned
        in __init__, which is another bypass."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController(), config=_HeadlessConfig())
        b._pool = _SyncPool()
        self.assertTrue(b.headless, "the flag was not read from config")
        self.assertEqual(b.route["kind"], "cast")

    def test_deleting_a_playlist_falls_back_to_the_cast_screen(self):
        """The third bypass: pruning the stack falls back to a default."""
        b = self._browser()
        b.nav_stack = [{"kind": "playlist", "server": "srv1",
                        "item_id": "pl1"}]
        b.after_playlist_deleted("pl1")
        self.assertEqual(b.route["kind"], "cast")

    def test_the_offline_fallback_does_not_open_the_library(self):
        b = self._browser()
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.route["kind"], "cast")


class _HeadlessConfig:
    """Minimal settings accessor with headless on."""

    @staticmethod
    def get_settings():
        return {"headless": True}

    @staticmethod
    def settings_schema():
        return []

    @staticmethod
    def sections():
        return []


class TestNoRouteEscapesTheLockdown(unittest.TestCase):
    """The catch-all. Every route kind the browser declares must either be
    on the headless allow-list or be refused by navigate(). A new route
    added later is refused by default — this asserts that stays true, so
    the failure mode is a locked box, never an open one."""

    def test_every_declared_route_is_either_allowed_or_refused(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.headless = True
        b.server = "srv1"
        b.show_cast()

        leaked = []
        for kind in b._routes():
            if kind in b.HEADLESS_ROUTES:
                continue
            b.nav_stack = [{"kind": "cast"}]
            b.navigate({"kind": kind, "server": "srv1", "parent_id": "lib1",
                        "item_id": "m1", "person_id": "p1"})
            if b.route["kind"] != "cast":
                leaked.append(kind)
        self.assertEqual(leaked, [],
                         "these routes are reachable in headless mode: %s"
                         % leaked)

    def test_the_allow_list_is_only_what_headless_needs(self):
        """Guards the other direction: something added to HEADLESS_ROUTES
        for convenience would silently widen the lock."""
        self.assertEqual(set(MpvtkBrowser.HEADLESS_ROUTES),
                         {"cast", "connecting", "locked"})

    def test_the_scan_saw_the_routes(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        self.assertGreater(len(b._routes()), 10)

    def test_no_new_code_path_assigns_nav_stack_behind_navigate(self):
        """navigate() is where the lockdown lives, so anything assigning
        nav_stack directly is outside it. That is not theoretical — a
        successful connect did exactly this and put a headless box on the
        library while every navigate()-based test stayed green.

        Every such assignment must land on _default_route(), which is
        headless-aware. Read the source rather than trusting a behavioural
        sweep, because the whole failure mode is a path no test drives.
        """
        import ast
        import inspect
        import os
        from jellyfin_mpv_shim import mpvtk_browser

        pkg = os.path.dirname(inspect.getfile(mpvtk_browser))
        offenders = []
        for name in sorted(os.listdir(pkg)):
            if not name.endswith(".py"):
                continue
            with open(os.path.join(pkg, name)) as fh:
                src = fh.read()
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if not isinstance(node, ast.Assign):
                    continue
                for t in node.targets:
                    if not (isinstance(t, ast.Attribute)
                            and t.attr == "nav_stack"):
                        continue
                    seg = ast.get_source_segment(src, node) or ""
                    # An empty stack is navigate(reset=True) clearing before
                    # it appends; that one IS inside navigate.
                    if seg.rstrip().endswith("= []"):
                        continue
                    if "_default_route()" not in seg:
                        offenders.append("%s:%d %s"
                                         % (name, node.lineno,
                                            " ".join(seg.split())[:70]))
        self.assertEqual(
            offenders, [],
            "these assign nav_stack without _default_route(), so they "
            "bypass the headless lockdown: %s" % offenders)


if __name__ == "__main__":
    unittest.main()


class TestTheCastScreenFollowsPlayback(HeadlessBase):
    """The cast screen sits behind the now-playing bar, so it has to show
    what is PLAYING. It kept showing whatever a phone last cast, so starting
    a playlist left an unrelated film on screen for the whole album."""

    def _browser_with_cast_spy(self):
        b = self._browser()
        self.shown = []

        def spy(srv, iid):
            # Mirror what the real one does to the state the code reads,
            # or "is it still idle?" never becomes false and the stop path
            # looks like a no-op for the wrong reason.
            self.shown.append((srv, iid))
            b._cast = {"idle": False, "title": iid}

        b.display_cast_item = spy
        self.idled = []
        real_idle = b.show_cast_idle
        b.show_cast_idle = lambda: (self.idled.append(1) or real_idle())
        return b

    def _play(self, b, track_id, **kw):
        payload = {"stopped": False, "is_audio": True, "id": track_id,
                   "title": "Track %s" % track_id, "position": 1,
                   "duration": 100}
        payload.update(kw)
        b.on_playstate(payload)

    def test_starting_a_track_shows_that_track(self):
        b = self._browser_with_cast_spy()
        self._play(b, "t1")
        self.assertEqual(self.shown, [("srv1", "t1")])

    def test_each_track_in_a_playlist_updates_the_screen(self):
        b = self._browser_with_cast_spy()
        self._play(b, "t1")
        self._play(b, "t2")
        self._play(b, "t3")
        self.assertEqual([i for _s, i in self.shown], ["t1", "t2", "t3"])

    def test_the_ticker_does_not_refetch_the_same_track(self):
        """The now-playing ticker pushes a playstate every second. Refetching
        the item each time would be one API call per second, forever."""
        b = self._browser_with_cast_spy()
        self._play(b, "t1")
        for _ in range(5):
            self._play(b, "t1")     # same track, later positions
        self.assertEqual(len(self.shown), 1,
                         "the cast screen refetched on every tick")

    def test_stopping_returns_to_ready_to_cast(self):
        """Leaving the last thing played on screen reads as though it is
        still playing."""
        b = self._browser_with_cast_spy()
        self._play(b, "t1")
        b.on_playstate({"stopped": True})
        self.assertTrue(self.idled, "stopping left the last track on screen")

    def test_a_phones_cast_is_replaced_by_what_actually_plays(self):
        """The reported bug, in order: cast an item, then play something
        unrelated."""
        b = self._browser_with_cast_spy()
        b.display_item("srv1", "film1")
        self.assertEqual(self.shown, [("srv1", "film1")])
        self._play(b, "song9")
        self.assertEqual(self.shown[-1], ("srv1", "song9"),
                         "the cast screen kept showing the old item")

    def test_video_does_not_touch_the_cast_screen(self):
        """Video takes the whole window; the cast screen is not visible, so
        fetching an item for it is pure waste."""
        b = self._browser_with_cast_spy()
        b.on_playstate({"stopped": False, "is_audio": False, "id": "v1",
                        "title": "Film", "position": 1, "duration": 100})
        self.assertEqual(self.shown, [])


class TestWithoutTheFlagPlaybackDoesNotTouchTheCastScreen(HeadlessBase):
    HEADLESS = False

    def test_playing_audio_leaves_the_cast_screen_alone(self):
        b = self._browser()
        shown = []
        b.display_cast_item = lambda srv, iid: shown.append(iid)
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "title": "Song", "position": 1, "duration": 100})
        self.assertEqual(shown, [],
                         "a normal browser fetched for a screen it never shows")


class TestTheCastScreenUsesThePlayingItemsServer(HeadlessBase):
    """self.server is the server the BROWSER has selected, which is not
    necessarily where the playing item lives. Guessing it would fetch the
    wrong item, or nothing, on a multi-server setup — so the playstate
    carries the real one."""

    def test_the_playstate_server_wins_over_the_selected_one(self):
        b = self._browser()
        b.server = "srv1"
        shown = []
        b.display_cast_item = lambda srv, iid: shown.append(srv)
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "server_uuid": "srv2", "title": "S",
                        "position": 1, "duration": 10})
        self.assertEqual(shown, ["srv2"],
                         "fetched from the browser's server, not the "
                         "playing item's")

    def test_it_falls_back_to_the_selected_server(self):
        """Older payloads (and the fake player in tests) carry no uuid."""
        b = self._browser()
        b.server = "srv1"
        shown = []
        b.display_cast_item = lambda srv, iid: shown.append(srv)
        b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                        "title": "S", "position": 1, "duration": 10})
        self.assertEqual(shown, ["srv1"])
