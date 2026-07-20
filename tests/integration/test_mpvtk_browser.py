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

        def get_home_rows(self, server_uuid, libraries=None):
            return list(self.rows)

        def get_library_items(self, server_uuid, parent_id, start_index=0,
                              **kw):
            items = [{"Id": "g%d" % i, "Name": "Grid %d" % i, "Type": "Movie"}
                     for i in range(24)]
            return items[start_index:start_index + 24], len(items)

        def image_spec(self, item, image_type="Primary", width=280):
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


if __name__ == "__main__":
    unittest.main()


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
