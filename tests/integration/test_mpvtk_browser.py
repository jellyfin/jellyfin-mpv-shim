"""Phase 0 exit test: the mpvtk browser shell rendered in a REAL mpv window,
via the attach path (MpvtkApp.attach -> AdoptBackend), on both backends.

This proves the whole Phase-0 stack end to end against a live mpv:
renderer.lua loads into an externally-created handle, the browser builds a
scene (chrome + strip rows via the production StripStore), it reaches the
renderer, and interaction round-trips. No player.py, no server, no network
(a fake source with placeholder tiles). Run per backend under xvfb by
run_integration.py.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402


def _make_source():
    class FakeSource:
        libraries = [
            {"Id": "lib1", "Name": "Movies", "Type": "CollectionFolder",
             "CollectionType": "movies"},
            {"Id": "lib2", "Name": "Shows", "Type": "CollectionFolder",
             "CollectionType": "tvshows"},
        ]
        rows = [{"title": "Continue Watching", "items": [
            {"Id": "m%d" % i, "Name": "Title %d" % i, "Type": "Movie",
             "ProductionYear": 2000 + i} for i in range(6)],
            "collection_type": None}]

        def servers(self):
            return [{"uuid": "srv1", "name": "Test"}]

        def get_libraries(self, server_uuid):
            return list(self.libraries)

        def get_home_prefs(self, server_uuid, refresh=False):
            # Imported here, not at module scope: this file defers every
            # jellyfin_mpv_shim import so it can be collected without mpv.
            from jellyfin_mpv_shim.mpvtk_browser import home_sections
            return list(home_sections.DEFAULT_LAYOUT), frozenset()

        def get_home_rows(self, server_uuid, libraries=None, sections=None,
                          layout=None, latest_excludes=None):
            return list(self.rows)

        def get_library_items(self, server_uuid, parent_id, start_index=0,
                              **kw):
            items = [{"Id": "g%d" % i, "Name": "Grid %d" % i, "Type": "Movie"}
                     for i in range(24)]
            return items[start_index:start_index + 24], len(items)

        def image_spec(self, item, image_type="Primary", width=280,
                       inherit=True):
            return None  # placeholder tiles -> no network

        def image_url(self, *a, **k):
            return None

    return FakeSource()


def _spawn_handle():
    """Create a raw mpv handle the way the player would, so the browser can
    attach to it (rather than mpvtk spawning its own)."""
    from jellyfin_mpv_shim.mpvtk.app import _SPAWN_OPTS

    if h.BACKEND == "jsonipc":
        import python_mpv_jsonipc
        opts = dict(_SPAWN_OPTS)
        opts["geometry"] = "1280x720"
        return python_mpv_jsonipc.MPV(start_mpv=True, **opts), True
    import mpv as libmpv
    opts = {k.replace("_", "-"): v for k, v in _SPAWN_OPTS.items()}
    opts["geometry"] = "1280x720"
    return libmpv.MPV(**opts), False


@h.require_real_mpv
class TestMpvtkBrowserOnRealMpv(unittest.TestCase):
    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        # Exercise the storage path that matches the backend, as the real
        # integration will: memory on in-process libmpv, files on jsonipc.
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-itest-")))
        self.browser = MpvtkBrowser(self.app, _make_source(), strips=strips)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            self.browser.shutdown()
            try:
                self.handle.terminate()
            except Exception:
                pass

    # The window mpv actually gave us must be big enough for a tile row to
    # exist at all. A window manager is free to ignore the requested
    # geometry — a full-suite run on a real desktop once produced 1272x55 —
    # and a squashed window then fails as "no overlays rendered", which
    # sends you looking for a rendering bug that isn't there. Run under
    # xvfb (run_integration.py does by default).
    MIN_RENDER_H = 200

    def _assert_usable_window(self, st):
        self.assertTrue(st and st.get("w", 0) > 0, "no render size: %r" % st)
        self.assertGreaterEqual(
            st.get("h", 0), self.MIN_RENDER_H,
            "the window came back %dx%d — too short for a tile row, so "
            "nothing renders. The window manager ignored the requested "
            "geometry; run under xvfb. %r"
            % (st.get("w", 0), st.get("h", 0), st))

    def test_renders_home_in_real_window(self):
        self.assertTrue(self.app.ready.wait(15),
                        "renderer never became ready in the attached mpv")
        # Let the async home load complete and repaint.
        deadline = time.time() + 6
        st = None
        while time.time() < deadline:
            st = self.app.debug_state()
            if st and st.get("overlays", 0) >= 1:
                break
            time.sleep(0.3)
        self._assert_usable_window(st)
        self.assertGreaterEqual(
            st.get("overlays", 0), 1,
            "expected at least one strip overlay on the home screen: %r" % st)

    def test_click_navigates_into_a_library(self):
        self.assertTrue(self.app.ready.wait(15))
        # Wait for the home rows to actually RENDER (a strip overlay present
        # means the post-load re-render registered the tile hit-handlers) —
        # not just for the data to load, or the click races the render.
        deadline = time.time() + 6
        st = None
        while time.time() < deadline:
            st = self.app.debug_state()
            if (st and st.get("overlays", 0) >= 1
                    and "_data" in self.browser.route):
                break
            time.sleep(0.2)
        self._assert_usable_window(st)
        self.app.debug(cmd="click", id="row-libs-lib1")
        deadline = time.time() + 4
        while time.time() < deadline and self.browser.route["kind"] != "grid":
            time.sleep(0.2)
        self.assertEqual(self.browser.route["kind"], "grid")
        self.assertEqual(self.browser.route["parent_id"], "lib1")

    def test_the_ui_takes_mpvs_own_window_dragging(self):
        """The renderer's half of the client-side title bar, against a real
        mpv -- which is the only place it can be checked.

        mpv refuses every VO drag while the pointer is inside an input
        section enabled without ``allow-vo-dragging``, and every section
        covers the whole screen unless given a mouse area. Ours are not, so
        the title bar's ``begin-vo-dragging`` succeeded and moved nothing;
        no error, no log, on every platform at once. The flag fixes that and
        re-arms mpv's *built-in* dragging as a side effect, which would move
        the window from a press-and-move over any scrollbar in the browser --
        so the renderer turns that off for as long as the UI is up.

        Only the second half is observable from out here (a section's enable
        flags are not a property), and it is the load-bearing one: without
        it the flag must not be passed at all. ``tests/lua/test_renderer.lua``
        pins the flag itself; this pins that mpv accepts the trade.
        """
        self.assertTrue(self.app.ready.wait(15))
        deadline = time.time() + 5
        while time.time() < deadline:
            if self.handle.input_builtin_dragging is False:
                break
            time.sleep(0.2)
        self.assertIs(
            self.handle.input_builtin_dragging, False,
            "mpv's built-in dragging is still on while the browser owns the "
            "pointer: dragging a scrollbar will move the window")




@h.require_real_mpv
class TestLongDropdownScroll(unittest.TestCase):
    """A picker with more entries than the window is tall (a year filter on
    a big library) drew its overflow past the bottom edge, unreachable.
    The popup now shows a scrollable window into the list."""

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.widgets import (Button, Column,
                                                     Dropdown, Spacer)

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        self.picked = []
        # 80 entries at ~34px each is far taller than a 720px window
        self.items = ["Item %d" % i for i in range(80)]
        self.dd = Dropdown("long-dd", self.items, selected=0, w=220,
                           on_select=lambda i, v: self.picked.append((i, v)))
        # something hoverable low on the page, under where the popup opens
        self.under = Button("Under", id="under-btn", on_click=lambda: None)
        self._thread = threading.Thread(
            target=lambda: self.app.run(
                lambda size: Column([self.dd, Spacer(h=300), self.under])),
            daemon=True)
        self._thread.start()

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.handle.terminate()
            except Exception:
                pass

    def _open(self):
        self.assertTrue(self.app.ready.wait(15), "renderer never ready")
        time.sleep(0.5)
        self.app.debug(cmd="click", id="long-dd")
        time.sleep(0.5)

    def test_the_popup_is_clamped_to_the_window(self):
        """The drawn popup must fit on screen. Unclamped, 80 entries drew
        ~2700px of list into a 720px window — everything past the fold was
        painted off the bottom edge and could never be seen or hovered."""
        self._open()
        st = self.app.debug_state()
        self.assertTrue(st, "no debug state")
        self.assertTrue(st.get("dd_open"), "popup did not open")
        g = st.get("dd_geo")
        self.assertTrue(g, "no popup geometry reported")
        self.assertEqual(g["count"], len(self.items))
        self.assertLess(g["n"], g["count"],
                        "popup was not clipped at all")
        bottom = g["y"] + g["n"] * g["ih"]
        self.assertLessEqual(bottom, st["h"],
                             "popup draws past the bottom of the window")

    def test_an_item_past_the_fold_can_be_selected(self):
        """Item 60 is well below the window; selecting it is the whole
        point of the scroll window."""
        self._open()
        self.app.debug(cmd="popup", index=60)
        deadline = time.time() + 4
        while time.time() < deadline and not self.picked:
            time.sleep(0.2)
        self.assertEqual(self.picked, [(60, "Item 60")],
                         "could not reach an item past the fold")

    def test_a_visible_item_still_selects(self):
        self._open()
        self.app.debug(cmd="popup", index=1)
        deadline = time.time() + 4
        while time.time() < deadline and not self.picked:
            time.sleep(0.2)
        self.assertEqual(self.picked, [(1, "Item 1")])

    def _geo(self):
        st = self.app.debug_state()
        self.assertTrue(st and st.get("dd_geo"), "no popup geometry")
        return st, st["dd_geo"]

    def test_the_scrollbar_thumb_can_be_dragged(self):
        self._open()
        st, g = self._geo()
        self.assertEqual(g["off"], 0, "expected to start at the top")
        # thumb geometry mirrors popup_thumb(): right edge, proportional
        track_y, track_h = g["y"] + 4, g["n"] * g["ih"] - 8
        th = max(18, track_h * g["n"] / g["count"])
        x = g["x"] + g["w"] - 6      # popup_thumb(): x + w - 8, width 5
        # grab the thumb and drag it to the bottom of the track
        self.app.debug(cmd="down", x=x, y=track_y + th / 2)
        self.app.debug(cmd="moveto", x=x, y=track_y + track_h)
        time.sleep(0.4)
        _st2, g2 = self._geo()
        self.app.debug(cmd="up", x=x, y=track_y + track_h)
        self.assertGreater(g2["off"], 0, "dragging the thumb did not scroll")
        self.assertEqual(self.picked, [],
                         "releasing the thumb selected a row")

    def test_hover_is_blocked_under_an_open_popup(self):
        """A popup floats over the page and eats the click, so the page
        must not light up under it either."""
        # the button hovers normally with no popup open
        self.assertTrue(self.app.ready.wait(15))
        time.sleep(0.5)
        self.app.debug(cmd="moveto", id="under-btn")
        time.sleep(0.3)
        self.assertEqual(self.app.debug_state().get("hover"), "under-btn",
                         "fixture is wrong: the button never hovers")
        # ...and stops once a popup is over the page
        self.app.debug(cmd="click", id="long-dd")
        time.sleep(0.4)
        self.app.debug(cmd="moveto", id="under-btn")
        time.sleep(0.3)
        self.assertIsNone(self.app.debug_state().get("hover"),
                          "page hovered through an open popup")


@h.require_real_mpv
class TestTextBoxCommitsOnBlur(unittest.TestCase):
    """Leaving a text field must save it.

    Settings wired on_submit only, and renderer.lua's blur() emitted nothing,
    so ENTER was the sole way to save — type a value, click the next row, and
    it was silently gone. Across 65 settings rows, with no toast and no dirty
    marker. This has to be an integration test: the commit is generated by the
    renderer, so a headless layout test would assert on a handler nothing
    fires.
    """

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.widgets import Column, Spacer, TextBox

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        self.events = []          # (kind, id, value)
        self.boxes = [
            TextBox("tb-a", text="alpha", w=300,
                    on_submit=lambda v: self.events.append(("submit", "a", v)),
                    on_commit=lambda v: self.events.append(("commit", "a", v))),
            TextBox("tb-b", text="beta", w=300,
                    on_submit=lambda v: self.events.append(("submit", "b", v)),
                    on_commit=lambda v: self.events.append(("commit", "b", v))),
        ]
        self._thread = threading.Thread(
            target=lambda: self.app.run(
                lambda size: Column([self.boxes[0], Spacer(h=40),
                                     self.boxes[1]])),
            daemon=True)
        self._thread.start()
        self.assertTrue(self.app.ready.wait(15), "renderer never ready")
        time.sleep(0.5)

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.handle.terminate()
            except Exception:
                pass

    def _settle(self):
        time.sleep(0.6)

    def test_clicking_away_commits_the_edit(self):
        self.app.debug(cmd="click", id="tb-a")
        self._settle()
        self.app.debug(cmd="text", s="X")
        self._settle()
        self.app.debug(cmd="click", id="tb-b")       # focus moves -> blur A
        self._settle()
        self.assertIn(("commit", "a", "alphaX"), self.events,
                      "leaving the field threw the edit away: %r" % self.events)

    def test_an_untouched_field_stays_silent(self):
        self.app.debug(cmd="click", id="tb-a")
        self._settle()
        self.app.debug(cmd="click", id="tb-b")
        self._settle()
        self.assertEqual([e for e in self.events if e[0] == "commit"], [],
                         "committed a value nobody changed")

    def test_enter_submits_once_and_does_not_also_commit(self):
        self.app.debug(cmd="click", id="tb-a")
        self._settle()
        self.app.debug(cmd="text", s="Y")
        self._settle()
        self.app.debug(cmd="key", name="ENTER")
        self._settle()
        self.app.debug(cmd="click", id="tb-b")
        self._settle()
        self.assertIn(("submit", "a", "alphaY"), self.events)
        self.assertEqual([e for e in self.events if e[0] == "commit"], [],
                         "ENTER saved it and blur saved it again: %r"
                         % self.events)

    def test_escape_cancels_instead_of_committing(self):
        self.app.debug(cmd="click", id="tb-a")
        self._settle()
        self.app.debug(cmd="text", s="Z")
        self._settle()
        self.app.debug(cmd="key", name="ESC")
        self._settle()
        self.assertEqual([e for e in self.events if e[0] == "commit"], [],
                         "ESC committed the edit it was meant to cancel")


@h.require_real_mpv
class TestTableRowContextMenu(unittest.TestCase):
    """Right-clicking a table row must reach its handler on a real renderer.

    ImageMap regions have always carried on_context; a Box/Row could not, so
    a Table row could not either — every music playlist lost the track menu.
    node_at() returns the topmost node under the cursor, which for a row is
    the *cell* rather than the row, so whether the row's ctx flag is actually
    found is a renderer question a headless layout test cannot answer.
    """

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.widgets import Table

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        self.events = []
        rows = [{"id": "row-%d" % i,
                 "cells": ["%d" % i, "Track %d" % i, "An Artist"],
                 "on_click": (lambda i=i: self.events.append(("click", i))),
                 "on_context": (lambda x, y, i=i:
                                self.events.append(("context", i)))}
                for i in range(6)]
        cols = [{"label": "#", "w": 40}, {"label": "Title", "flex": 1},
                {"label": "Artist", "flex": 1}]
        self.table = Table(cols, rows, row_h=34)
        self._thread = threading.Thread(
            target=lambda: self.app.run(lambda size: self.table), daemon=True)
        self._thread.start()
        self.assertTrue(self.app.ready.wait(15), "renderer never ready")
        time.sleep(0.5)

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.handle.terminate()
            except Exception:
                pass

    def test_right_click_reaches_the_row(self):
        self.app.debug(cmd="rclick", id="row-2")
        time.sleep(0.6)
        self.assertIn(("context", 2), self.events,
                      "right-click never reached the row: %r" % self.events)

    def test_left_click_still_activates(self):
        self.app.debug(cmd="click", id="row-3")
        time.sleep(0.6)
        self.assertIn(("click", 3), self.events)
        self.assertNotIn(("context", 3), self.events)


@h.require_real_mpv
class TestRealMousePosPath(unittest.TestCase):
    """The pointer as MPV reports it, not as the debug hook simulates it.

    Every other mouse test here goes through ``mpvtk-debug``, which calls
    ``on_mouse_move`` directly. Nothing covered the property the renderer
    actually listens to -- and that is where #700 lived: mpv sets
    ``mouse-pos.hover`` only from MOUSE_ENTER/MOUSE_LEAVE and ignores every
    X11 crossing whose mode is not NotifyNormal, so a WM that grabs the
    pointer, maximizes the window under it and ungrabs leaves hover false
    with the pointer sitting in the middle of the window. The renderer then
    hit-tested every click at -1,-1 and the whole UI stopped responding.

    **The stranded state itself cannot be reached from out here**, and the
    reason is the same rule the fix uses: mpv's own ``mouse`` command decides
    hover from the window bounds (command.c synthesizes MOUSE_ENTER for an
    in-bounds artificial move), so it repairs the flag before the renderer
    ever sees it. That half is pinned in ``tests/lua/test_renderer.lua``,
    against the real observer. What only a real mpv can settle is the rest of
    the path: that a leave is still a leave, that an out-of-window position
    is not taken for a hover, and that the property reaches the renderer at
    all on both backends.
    """

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.widgets import Button, Column, Spacer

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        self.clicks = []
        self.btn = Button("Press me", id="target-btn", w=240,
                          on_click=lambda: self.clicks.append(1))
        self._thread = threading.Thread(
            target=lambda: self.app.run(
                lambda size: Column([Spacer(h=60), self.btn])),
            daemon=True)
        self._thread.start()
        self.assertTrue(self.app.ready.wait(15), "renderer never ready")
        time.sleep(0.6)

    def tearDown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.handle.terminate()
            except Exception:
                pass

    def _center(self):
        """The button's centre in WINDOW pixels, which is what mpv's `mouse`
        command speaks. Read from the pushed scene rather than node_rect(),
        which converts back to logical coordinates for widget code."""
        node = next((n for n in (self.app._nodes or [])
                     if n.get("id") == "target-btn"), None)
        self.assertIsNotNone(node, "the button never reached the renderer")
        return int(node["x"] + node["w"] / 2), int(node["y"] + node["h"] / 2)

    def _mouse(self, x, y):
        self.handle.command("mouse", int(x), int(y))
        time.sleep(0.4)

    def _mouse_state(self):
        st = self.app.debug_state()
        self.assertIsNotNone(st, "no debug state from renderer")
        return st

    def test_the_real_pointer_hovers_leaves_and_comes_back(self):
        cx, cy = self._center()
        self._mouse(cx, cy)
        st = self._mouse_state()
        self.assertEqual(st.get("hover"), "target-btn",
                         "mpv's own mouse-pos never reached the renderer: %r"
                         % (st.get("mouse"),))
        self.assertTrue(st["mouse"]["hover"])

        # A leave is still a leave: the position is forgotten, so nothing is
        # left hovered and a click cannot land on a control the pointer is
        # no longer over.
        self.handle.command("keypress", "MOUSE_LEAVE")
        time.sleep(0.4)
        st = self._mouse_state()
        self.assertIsNone(st.get("hover"), "the leave left the button hovered")
        self.assertEqual(st["mouse"]["x"], -1,
                         "the leave kept the last in-window position")

        # An out-of-window position must not be read as a hover. It only
        # moves at all while a button is held (X keeps delivering to the
        # grab), and believing it would light up controls under a pointer
        # that is somewhere else entirely.
        self._mouse(st["w"] + 50, st["h"] + 50)
        st = self._mouse_state()
        self.assertFalse(st["mouse"]["hover"],
                         "a position outside the window was taken as a hover")
        self.assertIsNone(st.get("hover"))

        # ...and coming back does come back.
        self._mouse(cx, cy)
        self.assertEqual(self._mouse_state().get("hover"), "target-btn",
                         "the pointer never got back into the window")

    def test_a_real_click_reaches_the_button(self):
        """A press and a release through mpv's own input stack, section
        stack included -- which the Lua fake cannot model.

        `keydown`/`keyup`, not `mouse x y 0`: that form delivers the button
        with neither state bit, which mpv reports as a *press* and which the
        two-function bindings `mp.set_key_bindings` installs answer to
        neither half of.
        """
        cx, cy = self._center()
        self._mouse(cx, cy)
        self.handle.command("keydown", "MBTN_LEFT")
        time.sleep(0.2)
        self.handle.command("keyup", "MBTN_LEFT")
        deadline = time.time() + 4
        while time.time() < deadline and not self.clicks:
            time.sleep(0.2)
        self.assertTrue(self.clicks,
                        "a real left click never reached the button")


if __name__ == "__main__":
    unittest.main()
