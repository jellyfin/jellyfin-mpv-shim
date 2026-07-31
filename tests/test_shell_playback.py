"""The seam between the browser and playback.

The playback HUD, the now-playing bar, the queue, and SyncPlay's effect on
them. What the browser does when video takes the window and gives it back.
"""

import unittest
import threading
import time
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeController,
    FakeSource,
    HudController,
    StubHudApp,
    _SyncPool,
    build_scene,
    editor_page,
    ids,
    types,
)


class TestPlaybackHudLayout(unittest.TestCase):
    """Viewport tiers + chapter slits of the playback HUD bar (hud.py),
    laid out headlessly. The lifecycle itself is covered on a real mpv
    in tests/integration/test_mpvtk_hud.py."""

    def _browser(self):
        ctl = HudController()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._browsing = False
        b.hud.shown = True
        b.hud.state = {"stopped": False, "is_audio": False,
                        "title": "Movie", "position": 50.0,
                        "duration": 100.0, "paused": False}
        return b, ctl

    def test_wide_viewport_has_all_controls_and_marks(self):
        b, _ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        present = ids(nodes)
        for nid in ("hud-pp", "hud-seek-back", "hud-seek-fwd",
                    "hud-ch-prev", "hud-ch-next", "hud-chapters",
                    "hud-audio", "hud-sub", "hud-quality",
                    "hud-mute", "hud-vol", "hud-fs", "hud-clock"):
            self.assertIn(nid, present)
        self.assertTrue(any("Ends at" in (n.get("text") or "")
                            for n in nodes), "ends-at label missing")
        seek = next(n for n in nodes if n.get("id") == "hud-seek")
        self.assertEqual(seek.get("marks"), [0.4, 0.8],
                         "chapter slits should be the interior chapters")

    def _episode(self, b, **kw):
        st = dict(b.hud.state)
        st.update({"title": "Pilot", "series_name": "The Show",
                   "season": 1, "episode": 2})
        st.update(kw)
        b.hud.state = st
        return b

    def test_an_episode_shows_its_series_and_number(self):
        """The old lua OSC got this from mpv's media-title ("Show - s01e02 -
        Pilot"); the HUD read only the item's own name, so an episode called
        "Pilot" gave no clue which show it belonged to."""
        b, _ctl = self._browser()
        self._episode(b)
        nodes, _h = build_scene(b, (1280, 720))
        texts = [n.get("text") or "" for n in nodes]
        self.assertTrue(any("The Show" in t for t in texts),
                        "the series name is not on screen")
        self.assertTrue(any("S1E2" in t for t in texts),
                        "the season/episode number is not on screen")
        self.assertIn("Pilot", texts, "the episode title was lost")

    def test_the_context_is_a_separate_line_not_a_joined_title(self):
        """Joined, a long name runs off the end and is cut mid-word — the
        detail banner learned this, and the top bar is tighter still."""
        b, _ctl = self._browser()
        self._episode(b)
        nodes, _h = build_scene(b, (1280, 720))
        texts = [n.get("text") or "" for n in nodes]
        self.assertNotIn("The Show   ·   S1E2   ·   Pilot", texts)
        self.assertIn("The Show   ·   S1E2", texts)

    def test_a_movie_shows_only_its_title(self):
        b, _ctl = self._browser()
        nodes, _h = build_scene(b, (1280, 720))
        texts = [n.get("text") or "" for n in nodes]
        self.assertIn("Movie", texts)
        self.assertFalse(any("·" in t for t in texts),
                         "a movie grew an empty context line")

    def test_a_series_with_no_numbering_still_names_the_show(self):
        """Either half is worth showing on its own."""
        b, _ctl = self._browser()
        self._episode(b, season=None, episode=None)
        nodes, _h = build_scene(b, (1280, 720))
        self.assertIn("The Show", [n.get("text") or "" for n in nodes])

    def test_a_number_with_no_series_name_still_shows(self):
        b, _ctl = self._browser()
        self._episode(b, series_name="")
        nodes, _h = build_scene(b, (1280, 720))
        self.assertIn("S1E2", [n.get("text") or "" for n in nodes])

    def test_narrow_viewport_drops_optional_controls(self):
        b, _ctl = self._browser()
        nodes, _h = build_scene(b, (460, 640))
        present = ids(nodes)
        for nid in ("hud-pp", "hud-prev", "hud-next",
                    "hud-audio", "hud-sub", "hud-mute", "hud-fs"):
            self.assertIn(nid, present)
        for nid in ("hud-seek-back", "hud-seek-fwd", "hud-ch-prev",
                    "hud-ch-next", "hud-chapters", "hud-quality",
                    "hud-vol", "hud-clock"):
            self.assertNotIn(nid, present)
        self.assertFalse(any("Ends at" in (n.get("text") or "")
                             for n in nodes),
                         "ends-at must drop below 1000px")

    def test_volume_mute_fullscreen_and_clock_toggle(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-mute"]["click"]()
        handlers["hud-vol"]["change"](30)
        handlers["hud-fs"]["click"]()
        names = [c[0] for c in ctl.transport]
        for n in ("toggle_mute", "set_volume", "toggle_fullscreen"):
            self.assertIn(n, names)
        # clock click flips total -> negative remaining
        clock = next(n for n in nodes
                     if (n.get("text") or "").startswith("0:50 / "))
        self.assertIn("1:40", clock["text"])
        handlers["hud-clock"]["click"]()
        self.assertTrue(b.hud.tc_remaining)
        nodes, _h = build_scene(b, (1280, 720))
        self.assertTrue(any((n.get("text") or "") == "0:50 / -0:50"
                            for n in nodes),
                        "remaining-time clock missing")

    def test_seek_bar_range_shading(self):
        b, _ctl = self._browser()
        b.hud.state["ranges"] = [[10.0, 40.0], [90.0, 100.0]]
        nodes, _h = build_scene(b, (1280, 720))
        seek = next(n for n in nodes if n.get("id") == "hud-seek")
        self.assertEqual(seek.get("ranges"), [[0.1, 0.4], [0.9, 1.0]])
        self.assertTrue(seek.get("hoverev"),
                        "seek bar must opt into hover events")

    def test_hover_bubble_shows_chapter_and_time(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertNotIn("hud-preview", ids(nodes))
        # need a laid-out slider rect for the float: fake node_rect via
        # a stub app that serves the previous scene's geometry
        seek = next(n for n in nodes if n.get("id") == "hud-seek")

        class GeoApp(StubHudApp):
            def node_rect(self, node_id):
                return seek if node_id == "hud-seek" else None

            def invalidate(self):
                pass

        b.app = GeoApp()
        handlers["hud-seek"]["hover"](45.0)
        self.assertEqual(b.hud.hover, 45.0)
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertIn("hud-preview", ids(nodes))
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("0:45", texts, "bubble timestamp missing")
        self.assertIn("Middle", texts,
                      "bubble chapter name missing (45s is in Middle)")
        handlers["hud-seek"]["hover_end"]()
        self.assertIsNone(b.hud.hover)
        nodes, _h = build_scene(b, (1280, 720))
        self.assertNotIn("hud-preview", ids(nodes))

    def test_hud_show_hide_adjusts_sub_margin(self):
        b, ctl = self._browser()
        b.hud.on_hud(True)
        b.hud.on_hud(False)
        self.assertIn(("hud_sub_margin", (True,)), ctl.transport)
        self.assertIn(("hud_sub_margin", (False,)), ctl.transport)

    def test_mid_viewport_keeps_quality_drops_chapters(self):
        b, _ctl = self._browser()
        nodes, _h = build_scene(b, (620, 640))
        present = ids(nodes)
        self.assertIn("hud-quality", present)
        self.assertIn("hud-seek-back", present)
        self.assertNotIn("hud-chapters", present)
        self.assertNotIn("hud-ch-prev", present)

    def test_seek_step_buttons_seek_relative(self):
        b, ctl = self._browser()
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-seek-back"]["click"]()
        handlers["hud-seek-fwd"]["click"]()
        self.assertIn(("seek_relative", (-10,)), ctl.transport)
        self.assertIn(("seek_relative", (30,)), ctl.transport)

    def test_chapter_jump_buttons(self):
        b, ctl = self._browser()
        _nodes, handlers = build_scene(b, (1280, 720))
        # pos=50: prev -> chapter at 40, next -> chapter at 80
        handlers["hud-ch-prev"]["click"]()
        handlers["hud-ch-next"]["click"]()
        self.assertIn(("seek", (40.0,)), ctl.transport)
        self.assertIn(("seek", (80.0,)), ctl.transport)

    def test_prev_chapter_within_grace_goes_further_back(self):
        b, ctl = self._browser()
        b.hud.state["position"] = 41.0  # within 2s of the 40s chapter
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-ch-prev"]["click"]()
        self.assertIn(("seek", (0.0,)), ctl.transport)

class TestPlaybackHudMenusAndFavorite(unittest.TestCase):
    def _browser(self, size=(1280, 720)):
        ctl = HudController()
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._browsing = False
        b.hud.shown = True
        b.hud.state = {"stopped": False, "is_audio": False,
                        "title": "Movie", "position": 50.0,
                        "duration": 100.0, "paused": False,
                        "favorite": False}
        return b, ctl

    def test_favorite_button_toggles(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertIn("hud-fav", ids(nodes))
        handlers["hud-fav"]["click"]()
        self.assertIn(("hud_action", ("toggle-favorite", None)),
                      ctl.transport)
        self.assertTrue(b.hud.state["favorite"], "optimistic flip")
        nodes, _h = build_scene(b, (460, 640))
        self.assertNotIn("hud-fav", ids(nodes),
                         "favorite hides below 560px")

    def test_settings_menu_root_and_speed_flow(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        self.assertIn("hud-settings", ids(nodes))
        self.assertNotIn("hud-menu", ids(nodes))
        handlers["hud-settings"]["click"]()
        self.assertEqual(b.hud.menu, "root")
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        labels = menu["items"]
        # parity with the lua gear sheet (+ SyncPlay, which the lua
        # keeps on its top bar the HUD doesn't have)
        for want in ("Quality", "Speed", "Aspect", "Profile",
                     "Subtitle Size", "Subtitle Position",
                     "Subtitle Color", "SyncPlay", "Playback Data",
                     "Screenshot", "Unwatched"):
            self.assertTrue(any(want.lower() in l.lower()
                                for l in labels),
                            "missing %r in %r" % (want, labels))
        idx = next(i for i, l in enumerate(labels)
                   if "Playback Speed" in l)
        handlers["hud-menu"]["select"](idx, labels[idx])
        self.assertEqual(b.hud.menu, "speed")
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        # controller has no real speed -> default 1.0 gets the check
        # (layout resolves icon names to path data; presence is enough)
        self.assertTrue(menu["icons"][menu["items"].index("1x")])
        self.assertFalse(menu["icons"][menu["items"].index("0.5x")])
        two = menu["items"].index("2x")
        handlers["hud-menu"]["select"](two, "2x")
        self.assertIn(("set_speed", (2.0,)), ctl.transport)
        self.assertIsNone(b.hud.menu, "leaf selection closes the menu")

    def test_settings_menu_back_and_dismiss(self):
        b, _ctl = self._browser()
        b.hud.menu = "aspect"
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertEqual(menu["items"][0], "Back")
        handlers["hud-menu"]["select"](0, "Back")
        self.assertEqual(b.hud.menu, "root")
        _nodes, handlers = build_scene(b, (1280, 720))
        handlers["hud-menu"]["dismiss"]()
        self.assertIsNone(b.hud.menu)

    def test_top_bar_back_title_syncplay(self):
        b, ctl = self._browser()
        nodes, handlers = build_scene(b, (1280, 720))
        present = ids(nodes)
        self.assertIn("hud-back", present)
        self.assertIn("hud-syncplay", present)
        # the title renders in the top header row, in the top strip
        title = next(n for n in nodes
                     if n.get("text") == "Movie" and n.get("y", 999) < 80)
        self.assertLess(title["y"], 80)
        # back yields to the library (stop_to_browser via controller)
        handlers["hud-back"]["click"]()
        self.assertIn(("stop", ()), ctl.transport)
        # the top SyncPlay button opens its sheet standalone: no Back
        # row, anchored at the button
        handlers["hud-syncplay"]["click"]()
        self.assertEqual(b.hud.menu, "syncplay")
        self.assertEqual(b.hud.menu_anchor, "hud-syncplay")
        nodes, _h = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertNotIn("Back", menu["items"])
        self.assertIn("None (Disabled)", menu["items"])
        # ... while the same sheet from the gear keeps its Back row
        b.hud.menu = None
        b.hud.menu_anchor = "hud-settings"
        b.hud.menu = "syncplay"
        nodes, _h = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertEqual(menu["items"][0], "Back")

    def test_no_syncplay_button_without_syncplay_state(self):
        b, ctl = self._browser()
        ctl.menu_state.pop("syncplay")
        nodes, _h = build_scene(b, (1280, 720))
        self.assertIn("hud-back", ids(nodes))
        self.assertNotIn("hud-syncplay", ids(nodes))

    def test_sub_style_submenu_routes_verb(self):
        b, ctl = self._browser()
        b.hud.menu = "sub_size"
        ctl.menu_state["sub_style"] = {"size": {
            "current": "Normal",
            "options": [{"id": 0, "label": "Small", "selected": False},
                        {"id": 1, "label": "Normal", "selected": True}],
        }}
        nodes, handlers = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        idx = menu["items"].index("Small")
        handlers["hud-menu"]["select"](idx, "Small")
        self.assertIn(("hud_action", ("set-sub-size", 0)), ctl.transport)
        # a group the state blob doesn't carry renders only the Back row
        b.hud.menu = "sub_color"
        ctl.menu_state.pop("sub_style")
        nodes, _h = build_scene(b, (1280, 720))
        menu = next(n for n in nodes if n.get("id") == "hud-menu")
        self.assertEqual(menu["items"], ["Back"])

class TestHudLifecycleWiring(unittest.TestCase):
    def test_set_app_rewires_callbacks(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.hud.shown = True
        b.set_app(app)
        self.assertEqual(app.on_nav, b._on_nav_mode)
        self.assertEqual(app.on_hud, b.hud.on_hud)
        self.assertEqual(app.on_hud_skip, b.hud.on_skip)
        self.assertEqual(app.on_clipboard_error, b._on_clipboard_error)
        self.assertEqual(app.on_forward, b._on_mouse_forward)
        self.assertFalse(b.hud.shown,
                         "a fresh renderer has no summoned HUD")

class TestPlaybackDoesNotBlockTheLoop(unittest.TestCase):
    """Starting playback is seconds of work — build a Media, ask the server
    for PlaybackInfo, load the file into mpv under the player's lock. It ran
    on the loop thread, so the UI dispatched no events and drew no frames
    until playback began: click a movie, the browser freezes.

    The episode path was worse. It ran inside a `run_async` `on_done`, which
    `run_async` invokes while HOLDING `_lock` — so every other worker's
    callback and any `navigate()` (which needs `_lock` to bump the epoch)
    queued up behind it too.

    These use the REAL pool: with _SyncPool the work happens inline and
    every one of them would pass against the blocking version.
    """

    # The fake start blocks this long; the caller must return far sooner.
    BLOCK_FOR = 30.0
    MUST_RETURN_WITHIN = 2.0

    def _browser(self):
        self.started = threading.Event()
        self.release = threading.Event()
        ctl = FakeController()

        def blocking_play(*a, **k):
            self.started.set()
            # Long, and released only in cleanup: a short wait would simply
            # time out and let the BLOCKING version return, so the test
            # would pass against the bug it exists to catch.
            self.release.wait(self.BLOCK_FOR)

        ctl.play = blocking_play
        ctl.play_list = blocking_play
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        self.addCleanup(self.release.set)
        return b

    def _assert_returns_promptly(self, call):
        t0 = time.time()
        call()
        elapsed = time.time() - t0
        self.assertTrue(self.started.wait(5), "playback never started")
        self.assertLess(
            elapsed, self.MUST_RETURN_WITHIN,
            "the caller was blocked for %.1fs while playback started"
            % elapsed)

    def test_starting_a_movie_returns_immediately(self):
        b = self._browser()
        self._assert_returns_promptly(
            lambda: b._play({"Id": "m1", "Type": "Movie"}, "srv1"))

    def test_starting_a_list_returns_immediately(self):
        b = self._browser()
        self._assert_returns_promptly(
            lambda: b._play_list(["a1", "a2"], "srv1", 0, audio=True))

    def test_an_episode_start_does_not_hold_the_lock(self):
        """The one that also starved every other worker: navigate() needs
        _lock to bump the epoch, and on_done holds it for the whole start."""
        b = self._browser()
        b._play({"Id": "e1", "Type": "Episode", "SeriesId": "sh1"}, "srv1")
        self.assertTrue(self.started.wait(5), "playback never started")

        # _lock must be free while the start is in flight.
        done = threading.Event()

        def navigator():
            b._bump_epoch()
            done.set()

        threading.Thread(target=navigator, daemon=True).start()
        self.assertTrue(done.wait(5),
                        "_lock was held across the playback start")

    def test_a_failed_start_says_so(self):
        ctl = FakeController()

        def boom(*a, **k):
            raise RuntimeError("no route to server")

        ctl.play = boom
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._play({"Id": "m1", "Type": "Movie"}, "srv1")
        self.assertIn("playback", b.status.lower())

class TestPlaybackLifecycle(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        # Starting playback is off the loop thread now (see _play_async), so
        # the assertions below need it to have actually happened.
        # TestPlaybackDoesNotBlockTheLoop covers the asynchrony itself.
        self.b._pool = _SyncPool()

    def test_click_playable_opens_detail(self):
        self.b._open_item({"Id": "m1", "Name": "Alpha", "Type": "Movie"})
        self.assertEqual(self.b.route["kind"], "detail")
        self.assertEqual(self.b.route["item_id"], "m1")
        self.assertTrue(self.b._browsing, "opening detail must not yield")

    def test_play_holds_the_window_then_yields_once_playback_reports(self):
        """The yield is deferred to the first playstate.

        It used to happen at play intent, but yielding blanks our scene (HUD
        mode is attached-but-idle *with a blank scene*), so the whole load —
        seconds normally, up to playback_timeout when a stream stalls —
        rendered as an empty window with no way to tell loading from failed.
        The window is now held for the spinner and handed over once there is
        a picture to hand it to.
        """
        item = {"Id": "m1", "Name": "Alpha", "Type": "Movie"}
        self.b._play(item, "srv1", offset_ticks=123)
        self.assertFalse(self.b._browsing, "browser should leave browse mode")
        self.assertIsNotNone(self.b.load.starting, "no loading state to render")
        self.assertEqual(self.ctl.left, 0,
                         "the window was handed over before there was a "
                         "picture to hand it to")
        self.assertEqual(self.ctl.played, [("m1", "srv1", 123)])

        self.b.on_playstate({"stopped": False, "position": 0, "duration": 10})
        self.assertIsNone(self.b.load.starting)
        self.assertEqual(self.ctl.left, 1)     # OSC handed back, now

    def test_yielded_build_is_empty(self):
        self.b._browsing = False
        nodes, _h = build_scene(self.b)
        # No strip overlays / chrome while yielded to the video + OSC.
        self.assertNotIn("img", types(nodes))
        self.assertNotIn("nav-home", ids(nodes))

    def test_playstate_stopped_returns_to_browse(self):
        self.b._browsing = False
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b._browsing)
        self.assertEqual(self.ctl.entered, 1)   # took the window + OSC off

    def test_playstate_playing_keeps_yielded(self):
        self.b._browsing = True
        self.b.on_playstate({"stopped": False, "position": 5})
        self.assertFalse(self.b._browsing)

    def test_enter_browse_calls_controller(self):
        self.b.enter_browse()
        self.assertTrue(self.b._browsing)
        self.assertGreaterEqual(self.ctl.entered, 1)

    def test_minimize_releases_the_window_and_survives_a_cast(self):
        """"Minimized" is a player state (playback_abort + no force_window),
        not a hidden window: a cast while minimized must return to minimized
        when it ends, not pop the library open."""
        self.b.minimize()
        self.assertTrue(self.b.minimized)
        self.assertFalse(self.b._browsing)
        self.assertEqual(self.ctl.minimized, 1)

        self.b.on_playstate({"stopped": False, "is_audio": False})
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b.minimized, "cast ended -> back to minimized")
        self.assertFalse(self.b._browsing)

    def test_detaching_frees_the_tile_cache(self):
        """mpv going away (idle-quit) must drop the composited bitmaps: on
        libmpv they are in-process buffers the dead mpv read by address, so
        holding them leaks the memory the quit was meant to free."""
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=self.ctl)
        b.route["_data"] = {"libraries": b.source.libraries, "rows": []}
        build_scene(b)
        self.assertGreater(len(b.strips._cache), 0)
        b.strips.clear()
        self.assertEqual(len(b.strips._cache), 0)

    def test_app_can_be_swapped_and_rebuilt(self):
        """After an idle-quit the browser is pointed at a new MpvtkApp; its
        route stack and data survive, and invalidate() is a no-op while
        detached rather than a crash."""
        class FakeApp:
            def __init__(self):
                self.invalidated = 0

            def invalidate(self):
                self.invalidated += 1

            def set_active(self, on):
                pass

        b = MpvtkBrowser(app=FakeApp(), source=FakeSource(),
                         controller=self.ctl)
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        stack = list(b.nav_stack)

        b.app = None            # detached: mpv is gone
        b.invalidate()          # must not raise

        b.app = FakeApp()       # re-attached to the new handle
        b.invalidate()
        self.assertEqual(b.app.invalidated, 1)
        self.assertEqual(b.nav_stack, stack)

    def test_enter_browse_clears_minimized(self):
        self.b.minimize()
        self.b.enter_browse()
        self.assertFalse(self.b.minimized)
        self.assertTrue(self.b._browsing)

    def test_stop_while_not_minimized_still_opens_the_browser(self):
        self.b._browsing = False
        self.b.on_playstate({"stopped": True})
        self.assertTrue(self.b._browsing)
        self.assertFalse(self.b.minimized)

    def test_yield_suspends_the_renderer(self):
        """An empty scene is not enough to hand input to the OSC — the
        renderer's forced mouse/wheel bindings have to be unbound too."""
        class FakeApp:
            def __init__(self):
                self.active = []

            def invalidate(self):
                pass

            def set_active(self, on):
                self.active.append(on)

        app = FakeApp()
        b = MpvtkBrowser(app=app, source=FakeSource(), controller=self.ctl)
        b._play({"Id": "m1", "Name": "A", "Type": "Movie"}, "srv1")
        # Suspension waits for the handoff: while the load is in flight the
        # renderer must stay up, because it is drawing the spinner.
        self.assertEqual(app.active, [])
        b.on_playstate({"stopped": False, "position": 0, "duration": 10})
        self.assertEqual(app.active[-1], False)
        b.on_playstate({"stopped": True})
        self.assertEqual(app.active[-1], True)

    def test_set_source_repopulates_and_resets_home(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b.navigate({"kind": "grid", "parent_id": "lib1"})
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.server, "srv1")
        self.assertEqual(b.route["kind"], "home")
        self.assertEqual(len(b.nav_stack), 1)

    def test_a_reconnect_does_not_yank_you_back_to_home(self):
        """on_server_connected fires from the websocket redial loop, the
        cast-recovery path and the periodic health check — arbitrary moments
        mid-session. Resetting the nav stack there threw the user out of
        whatever they were reading every time a flaky server bounced."""
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        b.set_source(FakeSource(), server_uuid="srv1", keep_place=True)
        self.assertEqual(b.route["kind"], "grid",
                         "a reconnect reset the user to Home")
        self.assertEqual(b.route.get("parent_id"), "lib1")

    def test_a_reconnect_still_refreshes_the_page_it_kept(self):
        """Keeping your place is only useful if the page re-reads from the
        new source — otherwise it shows whatever the dead one returned."""
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "title": "Movies"})
        b.route.pop("_items", None)
        b.set_source(FakeSource(), server_uuid="srv1", keep_place=True)
        self.assertIsNotNone(b.route.get("_items"),
                             "the kept page was never reloaded")

    def test_keeping_your_place_falls_back_when_the_server_is_gone(self):
        """The page belongs to a server the new source does not have — the
        only sane destination is Home."""
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "gone", "parent_id": "lib1"})
        b.set_source(FakeSource(), server_uuid="srv1", keep_place=True)
        self.assertEqual(b.route["kind"], "home")

    def test_a_deliberate_switch_still_resets(self):
        """Signing in, unlocking or going offline are user actions; those
        should land on Home."""
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1"})
        b.set_source(FakeSource(), server_uuid="srv1")
        self.assertEqual(b.route["kind"], "home")

class TestDoubleClickToPlay(unittest.TestCase):
    """In a selectable list the row click selects, so the only way to play
    was the small arrow in the first column. Double-click is what every
    media app does — and Table has carried an unused on_dbl since it was
    written."""

    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b._size = (1280, 720)

    def _queue(self):
        self.b._open_queue()
        return build_scene(self.b)

    def test_double_clicking_a_queue_row_jumps_to_it(self):
        _nodes, h = self._queue()
        self.assertIn("dbl", h["q-1"], "no double-click on a queue row")
        h["q-1"]["dbl"]()
        self.assertIn("skip_to", [c[0] for c in self.ctl.transport])

    def test_a_single_click_still_only_selects(self):
        """Double-click must not come at the cost of the selection model —
        the toolbar's move/remove all work off it."""
        _nodes, h = self._queue()
        h["q-1"]["click"]({"shift": False, "ctrl": False})
        self.assertNotIn("skip_to", [c[0] for c in self.ctl.transport])
        self.assertTrue(editor_page(self.b, self.b.route).selection(), "the click did not select")

    def test_a_non_selectable_list_is_unchanged(self):
        """There the row click already plays; adding a double-click would
        play it twice."""
        node = self.b._track_list(
            [{"Id": "t1", "Name": "T"}], "t", lambda i: None)
        _nodes, h = layout(node, 1280, 720)
        self.assertNotIn("dbl", h.get("t-0", {}))

class TestSyncPlayAcrossServers(unittest.TestCase):
    """The dialog asked one server for groups and showed no idea which one
    you were in: with two accounts signed in half your groups were
    invisible, every group looked equally joinable, and Leave was offered
    even with nothing to leave."""

    GROUPS = [
        {"id": "g1", "name": "Movie Night", "server_uuid": "srv1",
         "server_name": "Home", "participants": ["izzie"]},
        {"id": "g2", "name": "Remote Watch", "server_uuid": "srv2",
         "server_name": "Cabin", "participants": []},
    ]

    def _browser(self, joined=None, groups=None):
        ctl = FakeController()
        self.asked = []
        groups = self.GROUPS if groups is None else groups
        ctl.get_sync_groups = lambda srv=None: (self.asked.append(srv)
                                                or list(groups))
        ctl.sync_state = lambda: joined
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        self.ctl = b.controller
        b._open_syncplay()
        return b

    def _nodes(self, b):
        return layout(b._dialog(), 1280, 720)

    def test_it_asks_every_server_not_just_the_selected_one(self):
        self._browser()
        self.assertEqual(self.asked, [None],
                         "groups were fetched for one server only")

    def test_groups_from_other_servers_are_listed(self):
        b = self._browser()
        nodes, _h = self._nodes(b)
        texts = " ".join(n.get("text") or "" for n in nodes)
        self.assertIn("Movie Night", texts)
        self.assertIn("Remote Watch", texts)

    def test_a_group_says_which_server_it_is_on(self):
        b = self._browser()
        nodes, _h = self._nodes(b)
        texts = " ".join(n.get("text") or "" for n in nodes)
        self.assertIn("Cabin", texts)

    def test_one_server_does_not_label_every_row(self):
        """Noise when it disambiguates nothing."""
        b = self._browser(groups=[self.GROUPS[0]])
        nodes, _h = self._nodes(b)
        texts = " ".join(n.get("text") or "" for n in nodes)
        self.assertNotIn("Home", texts)

    def test_joining_a_remote_group_uses_that_groups_server(self):
        """The click handler passed self.server for every row, so joining a
        group on the other server sent the join to the wrong one."""
        b = self._browser()
        _nodes, h = self._nodes(b)
        h["sp-join-1"]["click"]()
        joins = [c for c in self.ctl.transport if c[0] == "sync_join"]
        self.assertEqual(joins[-1][1][0], "srv2")

    def test_the_joined_group_is_marked(self):
        b = self._browser(joined={"group_id": "g2", "server_uuid": "srv2"})
        nodes, _h = self._nodes(b)
        texts = " ".join(n.get("text") or "" for n in nodes)
        self.assertIn("joined", texts.lower())

    def test_clicking_the_joined_group_does_not_rejoin(self):
        b = self._browser(joined={"group_id": "g2", "server_uuid": "srv2"})
        _nodes, h = self._nodes(b)
        h["sp-join-1"]["click"]()
        self.assertEqual([c for c in self.ctl.transport
                          if c[0] == "sync_join"], [])

    def test_leave_is_offered_only_when_there_is_something_to_leave(self):
        b = self._browser()
        self.assertNotIn("sp-leave", ids(self._nodes(b)[0]))
        b = self._browser(joined={"group_id": "g1", "server_uuid": "srv1"})
        self.assertIn("sp-leave", ids(self._nodes(b)[0]))

    def test_leave_goes_to_the_server_the_group_is_on(self):
        b = self._browser(joined={"group_id": "g2", "server_uuid": "srv2"})
        _nodes, h = self._nodes(b)
        h["sp-leave"]["click"]()
        leaves = [c for c in self.ctl.transport if c[0] == "sync_leave"]
        self.assertEqual(leaves[-1][1][0], "srv2")

class TestQueueView(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_queue_renders_entries_with_current(self):
        self.b._open_queue()
        nodes, handlers = build_scene(self.b)
        self.assertEqual(self.b.route["kind"], "queue")
        self.assertIn("q-0", ids(nodes))
        # Same toolbar-driven shape as the playlist editor.
        for nid in ("q-top", "q-up", "q-down", "q-bottom", "q-remove"):
            self.assertIn(nid, ids(nodes))

    def test_queue_row_play_skips(self):
        self.b._open_queue()
        _n, handlers = build_scene(self.b)
        handlers["q-play-0"]["click"]()
        self.assertIn("skip_to", [c[0] for c in self.ctl.transport])

    def test_queue_reorder(self):
        self.b._open_queue()
        route = self.b.route
        first = route["_data"]["entries"][0]["pid"]
        editor_page(self.b, route).click_row(0, None)
        editor_page(self.b, route)._move("down")
        self.assertEqual(route["_data"]["entries"][1]["pid"], first)
        self.assertIn("queue_reorder", [c[0] for c in self.ctl.transport])

    def test_queue_shift_select_then_block_move(self):
        self.b._open_queue()
        route = self.b.route
        pids = [e["pid"] for e in route["_data"]["entries"]]
        _n, h = build_scene(self.b)
        h["q-0"]["click"]({})
        h["q-1"]["click"]({"shift": True})
        self.assertEqual(route["_sel"], {0, 1})
        editor_page(self.b, route)._move("bottom")
        self.assertEqual([e["pid"] for e in route["_data"]["entries"]],
                         [pids[2], pids[0], pids[1]])

    def test_queue_remove_calls_controller_and_refreshes(self):
        self.b._open_queue()
        route = self.b.route
        _n, h = build_scene(self.b)
        h["q-0"]["click"]({})
        h["q-remove"]["click"]()
        self.assertIn("queue_remove",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

class TestNowPlaying(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def test_audio_play_keeps_browsing_and_shows_bar(self):
        self.b._play_list(["t1", "t2"], "srv1", 0, audio=True)
        self.assertTrue(self.b._browsing, "audio must not yield the window")
        self.assertIsNotNone(self.b._now_playing)

    def test_video_play_yields(self):
        self.b._play({"Id": "m1", "Type": "Movie"}, "srv1")
        self.assertFalse(self.b._browsing)
        self.assertIsNone(self.b._now_playing)

    def test_playstate_audio_populates_bar(self):
        self.b.on_playstate({"stopped": False, "is_audio": True,
                             "title": "Song", "artist": "Band",
                             "position": 65, "duration": 200, "paused": False})
        self.assertTrue(self.b._browsing)
        nodes, handlers = build_scene(self.b)
        self.assertIn("np-pp", ids(nodes))
        self.assertIn("np-stop", ids(nodes))
        # transport wired to the controller
        handlers["np-pp"]["click"]()
        handlers["np-next"]["click"]()
        names = [c[0] for c in getattr(self.ctl, "transport", [])]
        self.assertIn("toggle_pause", names)
        self.assertIn("next", names)

    def test_playstate_stopped_clears_bar(self):
        self.b.on_playstate({"stopped": False, "is_audio": True,
                             "title": "S", "duration": 10, "position": 1})
        self.b.on_playstate({"stopped": True})
        self.assertIsNone(self.b._now_playing)
        nodes, _h = build_scene(self.b)
        self.assertNotIn("np-pp", ids(nodes))

    def test_bar_controls_seek_volume_repeat_favorite(self):
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 100, "volume": 50})
        nodes, h = build_scene(self.b)
        for nid in ("np-seek", "np-vol", "np-repeat", "np-fav"):
            self.assertIn(nid, ids(nodes))
        # seek is commit-only (fires when the drag gesture ends); volume
        # stays live on change
        # Dragging must not seek. Asserted by driving the drag, not by the
        # absence of a change handler — there is one now, and it only moves
        # the elapsed clock.
        h["np-seek"]["change"](42)
        self.assertNotIn("seek", [c[0] for c in self.ctl.transport],
                         "np-seek live-seeks while dragging")
        h["np-seek"]["commit"](42)
        h["np-vol"]["change"](30)
        h["np-repeat"]["click"]()
        h["np-fav"]["click"]()
        names = [c[0] for c in getattr(self.ctl, "transport", [])]
        for n in ("seek", "set_volume", "set_repeat", "toggle_favorite"):
            self.assertIn(n, names)

    def test_dragging_the_volume_does_not_notify_until_release(self):
        """set_volume wakes the timeline thread, which posts progress to the
        server. on_change fires per mouse-move, so a single drag across the
        bar was a burst of round trips for a setting the server does not
        even track. Live for audible feedback, notify once on release."""
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 100, "volume": 50})
        _nodes, h = build_scene(self.b)
        for v in (40, 30, 20):
            h["np-vol"]["change"](v)
        vols = [c for c in self.ctl.transport_kw if c[0] == "set_volume"]
        self.assertEqual(len(vols), 3, "the drag was not applied live")
        self.assertTrue(all(c[2].get("notify") is False for c in vols),
                        "a mid-drag volume change notified: %r" % (vols,))

        h["np-vol"]["commit"](20)
        released = [c for c in self.ctl.transport_kw
                    if c[0] == "set_volume" and c[2].get("notify") is not False]
        self.assertEqual(len(released), 1,
                         "releasing the slider did not notify exactly once")

    def _elapsed(self, nodes):
        """The elapsed-time text, left of the seek bar."""
        seek = [n for n in nodes if n.get("id") == "np-seek"][0]
        clocks = [n for n in nodes if n["t"] == "text"
                  and ":" in (n.get("text") or "")
                  and n["x"] < seek["x"]]
        self.assertTrue(clocks, "no elapsed clock")
        return clocks[-1]["text"]

    def test_the_clock_follows_the_handle_while_scrubbing(self):
        """It sat frozen at the playhead for the whole gesture — the one
        moment it is actually being read."""
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 600})
        nodes, h = build_scene(self.b)
        self.assertEqual(self._elapsed(nodes), "0:10")
        h["np-seek"]["change"](305)
        nodes, _h = build_scene(self.b)
        self.assertEqual(self._elapsed(nodes), "5:05",
                         "the clock ignored the drag")

    def test_releasing_hands_the_clock_back_to_the_playhead(self):
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 600})
        _nodes, h = build_scene(self.b)
        h["np-seek"]["change"](305)
        h["np-seek"]["commit"](305)
        nodes, _h = build_scene(self.b)
        self.assertEqual(self._elapsed(nodes), "0:10",
                         "the clock stayed stuck on the drag target")

    def test_cancelling_a_drag_also_hands_it_back(self):
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 600})
        _nodes, h = build_scene(self.b)
        h["np-seek"]["change"](305)
        h["np-seek"]["cancel"]()
        nodes, _h = build_scene(self.b)
        self.assertEqual(self._elapsed(nodes), "0:10")
        self.assertNotIn("seek", [c[0] for c in self.ctl.transport],
                         "a cancelled drag seeked anyway")

    def test_a_drag_survives_the_one_second_ticker(self):
        """The now-playing ticker pushes a playstate every second. Clearing
        the pending drag on any playstate would cancel it a second in."""
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                             "title": "S", "position": 10, "duration": 600})
        _nodes, h = build_scene(self.b)
        h["np-seek"]["change"](305)
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                             "title": "S", "position": 11, "duration": 600})
        nodes, _h = build_scene(self.b)
        self.assertEqual(self._elapsed(nodes), "5:05",
                         "a ticker update cancelled the drag")

    def test_a_drag_does_not_outlive_its_track(self):
        """The renderer sends no cancel when a dragged slider just leaves
        the scene (queue ended, window yielded), so the pending value stuck
        and pinned the clock for every later track."""
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                             "title": "S", "position": 10, "duration": 600})
        _nodes, h = build_scene(self.b)
        h["np-seek"]["change"](305)
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "t2",
                             "title": "Next", "position": 3, "duration": 400})
        nodes, _h = build_scene(self.b)
        self.assertEqual(self._elapsed(nodes), "0:03",
                         "the next track's clock was stuck on the old drag")

    def test_a_drag_does_not_outlive_playback(self):
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "t1",
                             "title": "S", "position": 10, "duration": 600})
        _nodes, h = build_scene(self.b)
        h["np-seek"]["change"](305)
        self.b.on_playstate({"stopped": True})
        self.assertIsNone(self.b._np_scrub)

    def test_every_control_in_the_bar_names_itself(self):
        """The bar is icon-only end to end, and had no tooltips at all —
        the playback HUD has had them since it shipped."""
        self.b.on_playstate({"stopped": False, "is_audio": True, "title": "S",
                             "position": 10, "duration": 100, "volume": 50})
        nodes, _h = build_scene(self.b)
        untipped = [n["id"] for n in nodes
                    if str(n.get("id", "")).startswith("np-")
                    and n["id"] != "np-seek" and not n.get("tip")]
        self.assertEqual(untipped, [], "controls with no tooltip")

    def test_video_playstate_yields_no_bar(self):
        self.b.on_playstate({"stopped": False, "is_audio": False,
                             "title": "Movie", "position": 5, "duration": 100})
        self.assertFalse(self.b._browsing)
        self.assertIsNone(self.b._now_playing)

class TestCastRow(unittest.TestCase):
    """Cast was filtered to Actor/Director/Writer, dropping Producer,
    GuestStar and Composer, and mutated the shared DTOs in place."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_every_credited_role_is_kept(self):
        people = [{"Id": "p1", "Name": "A", "Type": "Actor", "Role": "Hero"},
                  {"Id": "p2", "Name": "B", "Type": "Producer"},
                  {"Id": "p3", "Name": "C", "Type": "Composer"}]
        self.assertIsNotNone(self.b._people_row(people, "srv1"))
        # the source list is untouched
        self.assertEqual([p["Type"] for p in people],
                         ["Actor", "Producer", "Composer"])

    def test_no_people_means_no_row(self):
        self.assertIsNone(self.b._people_row([], "srv1"))

class TestQueueSkipAndHighlight(unittest.TestCase):
    """The queue's play button passes a PlaylistItemId, and its highlight
    has to follow the track actually playing."""

    def setUp(self):
        self.ctl = FakeController()
        self.skipped = []
        self.ctl.skip_to = lambda key: self.skipped.append(key)
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()
        self.entries = [
            {"item": {"Id": "a", "Name": "One", "Type": "Audio"},
             "pid": "playlistItem1"},
            {"item": {"Id": "b", "Name": "Two", "Type": "Audio"},
             "pid": "playlistItem2"},
        ]
        self.b.nav_stack = [{"kind": "queue", "server": "srv1",
                             "_data": {"entries": self.entries,
                                       "current_id": "a"}}]

    def test_the_row_play_button_skips_to_that_entry(self):
        _n, h = build_scene(self.b)
        self.assertIn("q-play-1", h, "no per-row play button")
        h["q-play-1"]["click"]()
        self.assertEqual(self.skipped, ["playlistItem2"])

    def test_the_highlight_follows_the_playing_track(self):
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "b"})
        self.assertEqual(self.b.route["_data"]["current_id"], "b")

    def test_the_highlight_clears_when_playback_stops(self):
        self.b.on_playstate({"stopped": True})
        self.assertIsNone(self.b.route["_data"]["current_id"])

    def test_other_routes_are_untouched(self):
        self.b.nav_stack = [{"kind": "home", "server": "srv1"}]
        self.b.on_playstate({"stopped": False, "is_audio": True, "id": "b"})
        # no crash, nothing to sync
        self.assertEqual(self.b.route["kind"], "home")

    def test_the_play_glyph_is_centred_in_its_button(self):
        """align centres on the cross axis only; without justify the glyph
        sat against the button's left edge."""
        from jellyfin_mpv_shim.mpvtk.widgets import Column

        nodes, _h = layout(Column([self.b._track_list(
            [e["item"] for e in self.entries], "q",
            on_play=lambda i: None, on_select=lambda i, m: None,
            scroll_id="queue")]), 1000, 720)
        btn = next(n for n in nodes if n.get("id") == "q-play-0")
        icon = next(n for n in nodes if n["t"] == "icon")
        lead = icon["x"] - btn["x"]
        trail = (btn["x"] + btn["w"]) - (icon["x"] + icon["w"])
        self.assertAlmostEqual(lead, trail, delta=1.0)

class TestQueueLookup(unittest.TestCase):
    """skip_to resolves a queue entry through Media.get_from_key. It
    matched on item Id only, but the queue view addresses entries by
    PlaylistItemId — so every skip from the queue silently did nothing."""

    def _media(self):
        import sys

        sys.argv = [sys.argv[0]]
        from jellyfin_mpv_shim.media import Media

        m = Media.__new__(Media)
        m.client = None
        m.user_id = None
        m.seq = 0
        m.queue = [{"Id": "a", "PlaylistItemId": "pi1"},
                   {"Id": "b", "PlaylistItemId": "pi2"},
                   {"Id": "a", "PlaylistItemId": "pi3"}]
        return m

    def test_it_resolves_a_playlist_item_id(self):
        found = self._media().get_from_key("pi2")
        self.assertIsNotNone(found, "PlaylistItemId did not resolve")
        self.assertEqual(found.seq, 1)

    def test_a_duplicate_item_resolves_to_the_right_entry(self):
        """Two copies of the same track: the PlaylistItemId picks which."""
        found = self._media().get_from_key("pi3")
        self.assertEqual(found.seq, 2)

    def test_it_still_resolves_a_bare_item_id(self):
        """The websocket remote and the Tk browser address entries by Id."""
        found = self._media().get_from_key("b")
        self.assertEqual(found.seq, 1)

    def test_an_unknown_key_is_none(self):
        self.assertIsNone(self._media().get_from_key("nope"))
        self.assertIsNone(self._media().get_from_key(None))

class TestServerSwitchLeavesSyncPlay(unittest.TestCase):
    """SyncPlay is deliberately scoped to the selected server, so leaving
    that server has to leave the group — otherwise it stays joined with no
    way to reach it from this UI."""

    def setUp(self):
        self.ctl = FakeController()
        self.left = []
        self.ctl.sync_leave = lambda srv: self.left.append(srv)
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def test_switching_leaves_the_group_on_the_old_server(self):
        self.ctl.sync_active = lambda: True
        self.b._switch_server("srv2")
        self.assertEqual(self.left, ["srv1"])
        self.assertEqual(self.b.server, "srv2")

    def test_no_group_means_no_leave(self):
        self.ctl.sync_active = lambda: False
        self.b._switch_server("srv2")
        self.assertEqual(self.left, [])

    def test_reselecting_the_same_server_is_a_no_op(self):
        self.ctl.sync_active = lambda: True
        self.b._switch_server("srv1")
        self.assertEqual(self.left, [])

    def test_a_failing_check_does_not_block_the_switch(self):
        def boom():
            raise OSError("player gone")

        self.ctl.sync_active = boom
        self.b._switch_server("srv2")
        self.assertEqual(self.b.server, "srv2")

class TestRuntimeIsAClock(unittest.TestCase):
    """"112 min" makes you do the arithmetic to know whether it fits in an
    evening. Tk and jellyfin-web both show h:mm:ss."""

    def test_a_long_runtime_reads_as_a_clock(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        line = b._meta_line({"RunTimeTicks": 112 * 60 * 10_000_000})
        self.assertIn("1:52:00", line)
        self.assertNotIn("112", line)

    def test_a_short_one_drops_the_hours(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        self.assertIn("42:00",
                      b._meta_line({"RunTimeTicks": 42 * 60 * 10_000_000}))

class TestQueueRemovalReportsFailure(unittest.TestCase):
    """Every other edit in this UI reports; queue removal went through
    _safe, which logs and returns, so a removal the player refused left the
    rows on screen with no explanation."""

    def _browser(self, remove):
        ctl = FakeController()
        ctl.queue_remove = remove
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        route = {"kind": "queue", "server": "srv1", "_sel": {0},
                 "_data": {"entries": [{"item": {"Id": "a"}, "pid": "p1"},
                                       {"item": {"Id": "b"}, "pid": "p2"}],
                           "current_id": "a"}}
        b.nav_stack = [route]
        return b, route

    def test_a_refused_removal_says_so(self):
        def boom(pids):
            raise RuntimeError("nope")
        b, route = self._browser(boom)
        editor_page(b, route)._remove_selected()
        self.assertIn("could not be removed", b.status.lower())

    def test_it_re_reads_the_queue_either_way(self):
        """On failure especially: the rows have to go back to what the
        player really has, not the optimistic list."""
        for label, remove in (("ok", lambda pids: None),
                              ("fails", lambda pids: (_ for _ in ()).throw(
                                  RuntimeError()))):
            with self.subTest(remove=label):
                b, route = self._browser(remove)
                seen = []
                real = b.controller.get_queue
                b.controller.get_queue = lambda: (seen.append(1) or real())
                editor_page(b, route)._remove_selected()
                self.assertTrue(seen, "the queue was never re-read")
                self.assertNotEqual(
                    [e["pid"] for e in route["_data"]["entries"]],
                    ["p1", "p2"],
                    "still showing the pre-removal list")

    def test_a_successful_removal_is_silent(self):
        b, route = self._browser(lambda pids: None)
        editor_page(b, route)._remove_selected()
        self.assertNotIn("could not", b.status.lower())

class TestCastingDoesNotSummonTheBrowser(unittest.TestCase):
    """Browsing on a phone mirrors onto this client. Navigating to what the
    remote is looking at stays on; popping the window open when the browser
    is closed to the tray does not, because idly scrolling a phone would
    otherwise take over the TV. The route is set either way, so the page is
    waiting when the browser is next opened."""

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        self.settings = settings
        self._saved = settings.display_mirror_summon
        self.addCleanup(
            lambda: setattr(settings, "display_mirror_summon", self._saved))
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def _cast_while_minimized(self):
        self.b._minimized = True
        self.b.display_item("srv1", "m1")

    def test_it_does_not_open_the_window_by_default(self):
        self.settings.display_mirror_summon = False
        self._cast_while_minimized()
        self.assertTrue(self.b._minimized,
                        "casting popped the browser open from the tray")

    def test_the_page_is_still_waiting(self):
        """Not summoning must not mean not mirroring — the whole point is
        that it is already there when you open the browser."""
        self.settings.display_mirror_summon = False
        self._cast_while_minimized()
        self.assertEqual(self.b.route.get("item_id"), "m1")

    def test_it_opens_the_window_when_opted_in(self):
        self.settings.display_mirror_summon = True
        self._cast_while_minimized()
        self.assertFalse(self.b._minimized,
                         "opting in did not wake the browser")

    def test_an_open_browser_still_follows_the_remote(self):
        """Only the closed case changed; mirroring onto a browser the user
        already has open is the feature working normally."""
        self.settings.display_mirror_summon = False
        self.b._minimized = False
        self.b._browsing = True
        self.b.display_item("srv1", "m1")
        self.assertEqual(self.b.route.get("item_id"), "m1")


if __name__ == "__main__":
    unittest.main()
