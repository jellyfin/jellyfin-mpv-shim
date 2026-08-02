"""Window chrome, dialogs and the shared visual invariants.
"""

import unittest
from jellyfin_mpv_shim.mpvtk_browser import components
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeConfig,
    FakeController,
    FakeSource,
    HudController,
    MultiServerSource,
    StubHudApp,
    _SyncPool,
    build_scene,
    ids,
)


class TestClipboardNotice(unittest.TestCase):
    """MPV gained an X11 clipboard backend only in 0.41, so on an older MPV
    under X11 copy and paste do nothing at all. The renderer falls back to
    xclip/xsel/wl-copy; when even those are missing it says so, once."""

    def _browser(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        shown = []
        b._message = lambda text, title=None: shown.append((text, title))
        return b, shown

    def test_it_names_the_package_to_install(self):
        b, shown = self._browser()
        b._on_clipboard_error("paste", "wl-clipboard")
        self.assertEqual(len(shown), 1)
        text = shown[0][0]
        self.assertIn("wl-clipboard", text)
        self.assertIn("apt install wl-clipboard", text)

    def test_it_says_which_operation_failed(self):
        b, shown = self._browser()
        b._on_clipboard_error("copy", "xclip")
        self.assertIn("Copying", shown[0][0])
        b._on_clipboard_error("paste", "xclip")
        self.assertIn("Pasting", shown[1][0])

    def test_with_no_package_to_suggest_it_still_explains(self):
        """A session we have no helper for at all -- the only remedy left is
        a newer MPV, and saying nothing reads as a broken text field."""
        b, shown = self._browser()
        b._on_clipboard_error("paste", None)
        self.assertIn("0.41", shown[0][0])

    def test_reassert_window_state(self):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.set_app(app)
        b._browsing = True
        b.reassert_window_state()
        self.assertEqual(app.calls[-1], ("active", True))
        b._browsing = False
        b.hud.state = {"stopped": False}
        b.reassert_window_state()
        self.assertEqual(app.calls[-1], ("hud", True),
                         "video in flight re-enters HUD mode")
        b.hud.state = None
        b.reassert_window_state()
        self.assertEqual(app.calls[-1], ("active", False))

    def test_video_playstate_engages_hud_when_already_yielded(self):
        """Playback that starts while minimized/yielded (cast, crash
        recovery) must still enter HUD mode — _yield only runs on the
        browsing -> video transition."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=HudController())
        app = StubHudApp()
        b.set_app(app)
        b._browsing = False
        b.on_playstate({"stopped": False, "is_audio": False,
                        "title": "M", "position": 1.0,
                        "duration": 100.0, "paused": False,
                        "skip_label": "Skip Intro"})
        self.assertIn(("hud", True), app.calls)
        self.assertIn(("skip", "Skip Intro"), app.calls)

class TestDialogs(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _dialog_nodes(self):
        return build_scene(self.b)

    def test_message_dialog(self):
        self.b._message("Hello there")
        nodes, handlers = self._dialog_nodes()
        self.assertTrue(any(n["t"] == "layer" and n.get("kind") == "modal"
                            for n in nodes) or "dlg-ok" in ids(nodes))
        handlers["dlg-ok"]["click"]()
        _n, _h = self._dialog_nodes()
        self.assertIsNone(self.b._dialog)

    def test_confirm_runs_callback_on_ok(self):
        done = []
        self.b._confirm("Sure?", lambda: done.append(1))
        _n, handlers = self._dialog_nodes()
        handlers["dlg-ok"]["click"]()
        self.assertEqual(done, [1])
        self.assertIsNone(self.b._dialog)

    def test_confirm_cancel_does_not_run(self):
        done = []
        self.b._confirm("Sure?", lambda: done.append(1))
        _n, handlers = self._dialog_nodes()
        handlers["dlg-cancel"]["click"]()
        self.assertEqual(done, [])
        self.assertIsNone(self.b._dialog)

    def test_syncplay_dialog_lists_and_joins(self):
        self.b._open_syncplay()      # sync pool -> groups fetched, dialog shown
        nodes, handlers = self._dialog_nodes()
        self.assertIn("sp-join-0", ids(nodes))
        self.assertIn("sp-new", ids(nodes))
        handlers["sp-join-0"]["click"]()
        self.assertIn("sync_join",
                      [c[0] for c in getattr(self.ctl, "transport", [])])
        self.assertIsNone(self.b._dialog)   # closes on join

class TestBanners(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)

    def test_update_banner_shows_and_dismisses(self):
        self.b.notify_update("2.5.0", "http://example/rel")
        nodes, handlers = build_scene(self.b)
        self.assertIn("banner-open", ids(nodes))
        self.assertIn("banner-dismiss", ids(nodes))
        handlers["banner-dismiss"]["click"]()
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-dismiss", ids(nodes))

    def test_update_open_calls_controller(self):
        self.b.notify_update("2.5.0", "http://example/rel")
        _n, handlers = build_scene(self.b)
        handlers["banner-open"]["click"]()
        self.assertIn("open_url",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_offline_banner_toggles(self):
        self.b.set_offline(True)
        nodes, _h = build_scene(self.b)
        self.assertIn("banner-retry", ids(nodes))
        self.b.set_offline(False)
        nodes, _h = build_scene(self.b)
        self.assertNotIn("banner-retry", ids(nodes))

class TestChromePolish(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=MultiServerSource())
        self.b._pool = _SyncPool()

    def _labels(self, size):
        nodes, _h = build_scene(self.b, size=size)
        return {n["text"] for n in nodes if n["t"] == "text"}

    def test_wide_top_bar_is_labelled(self):
        labels = self._labels((1920, 900))
        self.assertIn("Settings", labels)
        self.assertIn("SyncPlay", labels)

    def test_narrow_top_bar_drops_to_icons(self):
        """The bar collapses when the labelled version genuinely doesn't fit
        (measured, not a width threshold)."""
        labels = self._labels((760, 900))
        self.assertNotIn("SyncPlay", labels)
        self.assertNotIn("Settings", labels)
        nodes, _h = build_scene(self.b, size=(760, 900))
        # The buttons are still there, just icon-only.
        self.assertIn("nav-settings", ids(nodes))
        self.assertIn("nav-syncplay", ids(nodes))

    def test_collapse_depends_on_what_is_in_the_bar(self):
        """The bar collapses when its contents don't fit, not at a fixed
        width: adding the user switcher pushes it over sooner. A width
        constant can't express that."""
        class Users(FakeController):
            def list_users(self):
                return [{"id": "u1", "name": "Izzie", "locked": False,
                         "active": True},
                        {"id": "u2", "name": "Guest", "locked": True,
                         "active": False}]

        # The width has to be one where the two cases actually differ, and
        # it moves whenever the bar gains a button -- it was 1160 before the
        # Favorites entry, which now collapses on its own at that size. That
        # is the behaviour under test, not a problem with it.
        at = 1240, 900
        self.assertIn("Settings", self._labels(at))       # no switcher

        self.b = MpvtkBrowser(app=None, source=MultiServerSource(),
                              controller=Users())
        self.b._pool = _SyncPool()
        self.assertNotIn("Settings", self._labels(at))    # switcher present

    def test_the_chrome_queries_each_list_once_per_frame(self):
        """The bar is built twice — a fit probe, then the real one — and
        each build asked the source and the controller for the server and
        user lists. Two round trips per frame on the loop thread, for data
        that cannot change between the two calls."""
        calls = []
        ctl = FakeController()
        ctl.list_users = lambda: (calls.append("users") or [])
        b = MpvtkBrowser(app=None, source=MultiServerSource(), controller=ctl)
        b._pool = _SyncPool()
        real_servers = b.source.servers
        b.source.servers = lambda: (calls.append("servers") or real_servers())
        build_scene(b, size=(1280, 720))
        self.assertEqual(calls.count("servers"), 1,
                         "servers() called %d times" % calls.count("servers"))
        self.assertEqual(calls.count("users"), 1,
                         "list_users() called %d times" % calls.count("users"))

    def test_a_collapsed_button_still_says_what_it_is(self):
        """Compact mode is exactly when a button stops carrying its label,
        and it was the one state with neither a label nor a tooltip — the
        icons are all the user has to go on."""
        nodes, _h = build_scene(self.b, size=(760, 900))
        for nid, tip in (("nav-settings", "Settings"),
                         ("nav-syncplay", "SyncPlay"),
                         ("nav-home", "Home")):
            node = [n for n in nodes if n.get("id") == nid][0]
            self.assertEqual(node.get("tip"), tip, nid)

    def test_a_labelled_button_carries_no_tooltip(self):
        """A tooltip that repeats a label the user is already reading is
        noise, and it covers the thing underneath it."""
        nodes, _h = build_scene(self.b, size=(1920, 900))
        node = [n for n in nodes if n.get("id") == "nav-settings"][0]
        self.assertIsNone(node.get("tip"))

    def test_the_icon_only_search_button_is_tipped_at_every_width(self):
        """It never has a label to lose, so the tooltip is not conditional."""
        for w in (760, 1920):
            nodes, _h = build_scene(self.b, size=(w, 900))
            node = [n for n in nodes if n.get("id") == "nav-search-go"][0]
            self.assertTrue(node.get("tip"), "untipped at %dpx" % w)

    def test_top_bar_never_overflows_the_window(self):
        for w in (900, 1100, 1279, 1280, 1920):
            nodes, _h = build_scene(self.b, size=(w, 800))
            bar = [n for n in nodes if n.get("id") == "nav-settings"][0]
            self.assertLessEqual(bar["x"] + bar["w"], w,
                                 "top bar overflows at %dpx" % w)

class TestBackButtonHistoryMenu(unittest.TestCase):
    """Right-clicking Back lists the page stack and jumps anywhere in it.

    This is the only place the forward stack is visible: the mouse's
    forward button has no on-screen counterpart, by design. Driven through
    the scene rather than by calling the opener, because the wiring is the
    part that breaks — a Button that never got the on_context through
    leaves the whole feature unreachable while every unit below it passes.
    """

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()
        for i in range(1, 3):
            self.b.navigate({"kind": "grid", "server": "srv1",
                             "parent_id": "lib%d" % i, "title": "L%d" % i,
                             "_data": []})

    def _scene(self):
        return build_scene(self.b, size=(1280, 720))

    def test_the_back_button_accepts_a_right_click(self):
        _nodes, handlers = self._scene()
        self.assertIn("context", handlers.get("nav-back", {}),
                      "right-clicking Back does nothing")

    def test_it_lists_the_stack_and_jumps(self):
        _nodes, handlers = self._scene()
        handlers["nav-back"]["context"](40, 60)
        nodes, handlers = self._scene()
        menu = next(n for n in nodes if n.get("id") == "historymenu")
        self.assertEqual(menu["items"], ["Home", "L1", "L2"])
        # Pick the root; the menu closes and the stack rewinds to it.
        handlers["historymenu"]["select"](0, "Home")
        self.assertIsNone(self.b._menu)
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1)

    def test_the_pages_it_rewound_past_are_then_ahead_of_you(self):
        _nodes, handlers = self._scene()
        handlers["nav-back"]["context"](40, 60)
        _nodes, handlers = self._scene()
        handlers["historymenu"]["select"](0, "Home")
        _nodes, handlers = self._scene()
        self.assertNotIn("nav-back", handlers, "at the root, no Back button")
        self.b.go_forward()
        self.b.go_forward()
        self.assertEqual(self.b.route["parent_id"], "lib2")

    def test_at_the_root_home_carries_the_menu_instead(self):
        """The gap Back cannot cover: backing all the way out is how you get
        a forward stack, and it is also what takes the Back button away."""
        self.b.go_back()
        self.b.go_back()
        _nodes, handlers = self._scene()
        self.assertNotIn("nav-back", handlers, "still at a sub-page")
        handlers["nav-home"]["context"](20, 60)
        nodes, handlers = self._scene()
        menu = next(n for n in nodes if n.get("id") == "historymenu")
        self.assertEqual(menu["items"], ["Home", "L1", "L2"])
        handlers["historymenu"]["select"](2, "L2")
        self.assertEqual(self.b.route["parent_id"], "lib2")

    def test_home_offers_no_menu_with_no_history_either_way(self):
        """An empty menu listing only the page you are on is the same
        "nothing on offer" a tile with no actions declines to open."""
        self.b.navigate({"kind": "home", "server": "srv1"}, reset=True)
        _nodes, handlers = self._scene()
        self.assertNotIn("nav-back", handlers)
        self.assertNotIn("context", handlers.get("nav-home", {}))

    def test_home_leaves_the_menu_to_back_when_there_is_one(self):
        """Two identical menus one button apart is worse than one."""
        _nodes, handlers = self._scene()
        self.assertIn("context", handlers["nav-back"])
        self.assertNotIn("context", handlers.get("nav-home", {}))


class TestButtonColors(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def _accent_button_texts(self, nodes):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        accent = {n["id"] for n in nodes
                  if n["t"] == "rect" and n.get("fill") == theme.ACCENT}
        out = []
        for n in nodes:
            if n["t"] != "text":
                continue
            for a in accent:
                rect = next(r for r in nodes if r.get("id") == a)
                if (rect["x"] <= n["x"] <= rect["x"] + rect["w"]
                        and rect["y"] <= n["y"] <= rect["y"] + rect["h"]):
                    out.append(n)
        return out

    def test_accent_buttons_use_white_text(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        self.b.navigate({"kind": "series", "server": "srv1",
                         "item_id": "sh1", "title": "Show"})
        nodes, _h = build_scene(self.b)
        texts = self._accent_button_texts(nodes)
        self.assertTrue(texts, "expected at least one accent button")
        for n in texts:
            self.assertEqual(n["c"], theme.ACCENT_FG,
                             "%r should be white on blue" % n["text"])

    def test_next_up_is_a_primary_action(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        self.b.navigate({"kind": "series", "server": "srv1",
                         "item_id": "sh1", "title": "Show"})
        nodes, _h = build_scene(self.b)
        btn = [n for n in nodes if n.get("id") == "sa-nextup"][0]
        self.assertEqual(btn.get("fill"), theme.ACCENT)

class TestBanner(unittest.TestCase):
    def test_banner_is_two_thirds_of_a_16_9_box(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        bw, bh = b._banner_box(1280)
        self.assertAlmostEqual(bh / bw, 9 / 16 * 2 / 3, places=3)

    def test_heading_is_baked_into_the_banner(self):
        """Text over artwork has to be part of the bitmap — ASS would render
        underneath it."""
        from PIL import Image as PILImage
        b = MpvtkBrowser(app=None, source=FakeSource())
        art = PILImage.new("RGB", (800, 800), (40, 40, 40))
        plain = components.compose_banner(art, (600, 225))
        titled = components.compose_banner(art, (600, 225), title="The Show",
                                   meta="2020 · 45 min")
        self.assertEqual(titled.size, (600, 225))
        self.assertNotEqual(plain.tobytes(), titled.tobytes())

    #: Everything compose_banner draws sits inside this much margin.
    def _margin(self, w):
        from jellyfin_mpv_shim.mpvtk.scaling import px
        return max(px(18), w // 40)

    def _ink_columns(self, img, x_from):
        """Columns at or right of ``x_from`` carrying anything brighter than
        the backdrop. The art is black and the gradient only darkens it, so
        every bright pixel in a banner is text."""
        w, h = img.size
        return [x for x in range(x_from, w)
                if any(max(img.getpixel((x, y))[:3]) > 100
                       for y in range(h))]

    def test_a_long_meta_line_stays_inside_the_backdrop(self):
        """The meta line ends in the genres, and it used to be drawn with a
        raw draw.text -- no wrap, no ellipsis. A film carrying a handful of
        them ran off the right edge of the artwork, cut mid-letter by the
        canvas. Nothing downstream can clip a baked bitmap back."""
        from PIL import Image as PILImage
        art = PILImage.new("RGB", (800, 800), (0, 0, 0))
        meta = ("1998   ·   2:22:15   ·   R   ·   ★ 8.1   ·   "
                "Drama, Romance, Thriller, Science Fiction, Adventure, "
                "Mystery, Crime, Fantasy")
        img = components.compose_banner(art, (600, 225), title="The Film",
                                        meta=meta)
        spill = self._ink_columns(img, 600 - self._margin(600))
        self.assertEqual(spill, [], "text drawn into the right margin")

    def test_a_long_title_and_context_stay_inside_too(self):
        """The other two lines wrap and ellipsize already — pinned so the
        margin is a property of the banner rather than of one line."""
        from PIL import Image as PILImage
        art = PILImage.new("RGB", (800, 800), (0, 0, 0))
        img = components.compose_banner(
            art, (600, 225),
            title="A Deliberately Overlong Episode Title That Cannot Fit",
            meta="2020",
            context="Some Series With A Long Name · S01E01 · Pilot Episode")
        spill = self._ink_columns(img, 600 - self._margin(600))
        self.assertEqual(spill, [], "text drawn into the right margin")


class TestWrapRow(unittest.TestCase):
    """``chrome.wrap_row``: a Row that breaks onto more lines rather than
    running off the window. The screens that use it are checked end to end
    by tests/test_dpi_matrix.py; this pins the contract."""

    def _row(self, avail, n=6, **kw):
        from jellyfin_mpv_shim.mpvtk.widgets import Button
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome
        items = [Button("Button %d" % i, id="b%d" % i) for i in range(n)]
        return chrome.wrap_row(items, avail, **kw), items

    def test_a_row_that_fits_is_the_row_it_always_was(self):
        """Returning a Column unconditionally would move every screen's
        layout at every width; the snapshots would all need regenerating for
        a change that is meant to be invisible until it is needed."""
        from jellyfin_mpv_shim.mpvtk.widgets import Row
        out, items = self._row(4000)
        self.assertIsInstance(out, Row)
        self.assertEqual(out.children, items)

    def test_a_row_that_does_not_fit_is_broken_up(self):
        from jellyfin_mpv_shim.mpvtk.widgets import Column, Row
        out, items = self._row(260)
        self.assertIsInstance(out, Column)
        self.assertGreater(len(out.children), 1)
        for row in out.children:
            self.assertIsInstance(row, Row)
        drawn = [c for row in out.children for c in row.children]
        self.assertEqual(drawn, items, "wrapping dropped or reordered items")

    def test_an_unmeasurable_width_leaves_it_alone(self):
        """A page that does not know its width yet passes 0. One row is the
        honest answer there — better than guessing at a break."""
        from jellyfin_mpv_shim.mpvtk.widgets import Row
        self.assertIsInstance(self._row(0)[0], Row)
        self.assertIsInstance(self._row(-32)[0], Row)

    def test_a_flexible_spacer_is_dropped_only_when_it_wraps(self):
        """The Spacer means 'push these apart', which is what pins a
        trailing group to the right edge — and so is what puts those buttons
        off the window. It has nothing to say once the row is full."""
        from jellyfin_mpv_shim.mpvtk.widgets import Button, Spacer
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome
        items = [Button("One", id="b1"), Spacer(),
                 Button("Two", id="b2"), Button("Three", id="b3")]
        wide = chrome.wrap_row(items, 4000)
        self.assertIn(items[1], wide.children)
        narrow = chrome.wrap_row(items, 90)
        drawn = [c for row in narrow.children for c in row.children]
        self.assertNotIn(items[1], drawn)
        self.assertEqual([c for c in drawn],
                         [items[0], items[2], items[3]])

    def test_an_item_wider_than_the_space_gets_its_own_line(self):
        """Rather than an empty line before it, or an endless loop."""
        from jellyfin_mpv_shim.mpvtk.widgets import Column
        out, items = self._row(10, n=3)
        self.assertIsInstance(out, Column)
        self.assertEqual([len(r.children) for r in out.children], [1, 1, 1])


class TestMetaLine(unittest.TestCase):
    """``meta_line`` is the year · runtime · rating · genres line, shared by
    the detail, series and music headers."""

    def _line(self, **item):
        from jellyfin_mpv_shim.mpvtk_browser.components import detail
        return detail.meta_line(item)

    def test_genres_are_capped_at_three(self):
        """The quick read on what a thing is, not the full tag list. Eight
        genres pushed the year and rating off the visible line."""
        line = self._line(ProductionYear=1998,
                          Genres=["Drama", "Romance", "Thriller",
                                  "Science Fiction", "Adventure"])
        self.assertIn("Drama, Romance, Thriller", line)
        self.assertNotIn("Adventure", line)
        self.assertNotIn("Science Fiction", line)

    def test_a_short_genre_list_is_untouched(self):
        line = self._line(Genres=["Drama", "Romance"])
        self.assertEqual(line, "Drama, Romance")

    def test_no_genres_leaves_no_empty_separator(self):
        line = self._line(ProductionYear=1998, Genres=[])
        self.assertEqual(line, "1998")

class TestOneBlue(unittest.TestCase):
    """There is exactly one blue. A second, unrelated blue makes the UI look
    assembled from parts, so anything the app colours itself must come from
    the accent family."""

    ACCENT_FAMILY = None   # filled in setUp

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk_browser import theme
        self.theme = theme
        self.ACCENT_FAMILY = {theme.ACCENT, theme.ACCENT_HOVER,
                              theme.ACCENT_SOFT}
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl, config=FakeConfig())
        self.b._pool = _SyncPool()

    @staticmethod
    def _is_blue(hexstr):
        try:
            r, g, bl = (int(hexstr[i:i + 2], 16) for i in (0, 2, 4))
        except (ValueError, TypeError, IndexError):
            return False
        # Blue-dominant and not a near-grey.
        return bl > r + 25 and bl > 40 and (bl - min(r, g)) > 25

    def _blues_in(self, nodes):
        out = set()
        for n in nodes:
            for key in ("fill", "c", "bc"):
                v = n.get(key)
                if isinstance(v, str) and self._is_blue(v):
                    out.add(v)
            hov = n.get("hover") or {}
            for key in ("fill", "c", "bc"):
                v = hov.get(key)
                if isinstance(v, str) and self._is_blue(v):
                    out.add(v)
        return out

    def _check(self, label):
        nodes, _h = build_scene(self.b)
        stray = self._blues_in(nodes) - self.ACCENT_FAMILY
        self.assertEqual(stray, set(),
                         "%s uses blues outside the accent family" % label)

    def test_home_tiles_hover_ring(self):
        self.b.route["_data"] = {"libraries": self.b.source.libraries,
                                 "rows": self.b.source.home_rows}
        self._check("home")

    def test_update_banner(self):
        self.b.notify_update("1.2.3", "http://x")
        self._check("update banner")

    def test_download_status_bar(self):
        self.b.set_download_status({"pending": 2, "name": "X", "percent": 50})
        self._check("download bar")

    def test_selected_rows(self):
        self.b.navigate({"kind": "playlist_edit", "server": "srv1",
                         "item_id": "PL1", "title": "Faves"})
        self.b.route["_sel"] = {0}
        self._check("playlist editor selection")

    def test_music_tabs_and_settings_tabs(self):
        self.b.navigate({"kind": "music", "server": "srv1",
                         "parent_id": "lib1", "title": "Music"})
        self._check("music tabs")
        self.b.open_settings("general")
        self._check("settings tabs")

    def test_toolkit_widgets_take_the_app_accent(self):
        """The toolkit's own accented widgets (checkbox fill, hover ring,
        progress) follow the app palette rather than mpvtk's default."""
        from jellyfin_mpv_shim.mpvtk.layout import layout as lay
        from jellyfin_mpv_shim.mpvtk.widgets import Checkbox, Progress
        for widget in (Checkbox("x", True), Progress(0.5)):
            nodes, _h = lay(widget, 200, 50)
            self.assertEqual(
                self._blues_in(nodes) - self.ACCENT_FAMILY, set(),
                "%s used a blue outside the accent family"
                % type(widget).__name__)

    def test_checked_checkbox_in_a_real_view(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.b.route["_filters"] = {"unplayed": True, "favorite": True}
        self._check("grid filter bar with checked boxes")


class TestATransitionSurvivesARaisingController(unittest.TestCase):
    """The browse/playback transitions must complete even if the controller
    throws halfway through one.

    ``_yield`` clears ``_browsing`` and *then* engages the HUD;
    ``enter_browse`` refreshes Home and re-activates the renderer *after*
    telling the controller. An exception out of the controller therefore did
    not fail a callback, it abandoned the transition half-applied — and
    nothing puts that right until the next one.

    Not hypothetical: gateway.on_browse_leave read a setting #615 had
    retired, so every browse -> video transition raised AttributeError and
    skipped the HUD engage behind it.
    """

    def _browser(self):
        ctl = FakeController()
        for name in ("on_browse_leave", "on_browse_enter", "on_minimize"):
            def boom(*_a, **_k):
                raise RuntimeError("controller said no")
            setattr(ctl, name, boom)
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b.server = "srv1"
        return b

    def test_yield_still_engages_the_hud(self):
        """The step BEHIND the callback, which is what actually went
        missing: _browsing is cleared before the controller is told, so
        asserting on it only proves the exception did not propagate."""
        b = self._browser()
        b._browsing = True
        engaged = []
        b.hud.available = lambda: True
        b.hud.engage = lambda: engaged.append(True)
        b._yield()
        self.assertFalse(b._browsing, "the yield was abandoned")
        self.assertEqual(engaged, [True],
                         "the HUD engage behind the callback was skipped")

    def test_enter_browse_still_reactivates_the_renderer(self):
        b = self._browser()
        b.nav_stack = [{"kind": "home", "server": "srv1"}]
        b._browsing = False
        active = []
        b._set_renderer_active = lambda on: active.append(on)
        b.enter_browse()
        self.assertTrue(b._browsing, "the return to browse was abandoned")
        self.assertIn(True, active,
                      "the renderer was never re-activated")

    def test_minimize_still_minimizes(self):
        b = self._browser()
        b.nav_stack = [{"kind": "home", "server": "srv1"}]
        b.minimize()
        self.assertTrue(b.minimized, "the minimize was abandoned")


if __name__ == "__main__":
    unittest.main()
