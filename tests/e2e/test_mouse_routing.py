"""Real mouse buttons through mpv's input layer, across every UI transition.

The mouse twin of `test_input_routing`, and it exists for the same reason.
Almost every mouse test in this tree drives `app.debug(cmd="click"/"down"/
"moveto"/"up")`, which `renderer.lua`'s `mpvtk-debug` handler dispatches
straight into `on_mouse_move` / `on_mouse_down` / `on_mouse_up`. That covers
the gesture state machine and skips the layer above it -- mpv's section
stack, which decides whether the binding fires at all. #724 (a classic OSC
swallowed both buttons) and #726 (built-in dragging never handed back) both
lived in the skipped layer.

**"Real" here means real mpv input routing, not a real pointer.** mpv's
`mouse` command lands in `set_mouse_pos` (`input/input.c`), which is the
same static function every VO reaches through `mp_input_set_mouse_pos` --
`x11_common.c`, `w32_common.c` and `wayland_common.c` alike -- so
`update_mouse_section` and the `MP_KEY_MOUSE_MOVE` lookup run exactly as
they do for a hand on a mouse. What it does NOT reproduce is the OS event
translation below that join, which is mpv's code and not ours. One
consequence worth naming: a wrong `hover` flag cannot be injected here, so
#700's stranded-hover repair is not reachable from this file -- that lives
in `tests/lua/test_renderer.lua`, against a faked `mouse-pos`.
"""

import os
import sys
import time
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

# _e2e puts tests/integration on the path; both of these live there.
import _harness as h  # noqa: E402
from test_mpvtk_browser import _spawn_handle  # noqa: E402


@_e2e.require_server_and_mpv
class MouseRoutingTest(unittest.TestCase):
    """A real browser on a real mpv, against the real library."""

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.session = _e2e.Session()
        _e2e.normalise_home_layout(self.session)
        self.addCleanup(self.session.stop)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-mouse-")))
        self.browser = MpvtkBrowser(self.app, self.source,
                                    server_uuid=_e2e.SOURCE_UUID, strips=strips)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.addCleanup(self._teardown)
        self.assertTrue(self.app.ready.wait(20), "renderer never came up")
        self._wait(lambda: (self._state().get("overlays") or 0) >= 1,
                   "the browser never rendered a strip, so there is nothing "
                   "to click")
        state = self._state()
        self.assertGreaterEqual(
            state.get("h") or 0, 200,
            "the window came back %sx%s -- too short to hold a tile row, so "
            "a click has nothing to land on. Run under xvfb."
            % (state.get("w"), state.get("h")))

    def _teardown(self):
        try:
            self.app.quit()
            self._thread.join(timeout=5)
        finally:
            try:
                self.browser.shutdown(free_bitmaps=False)
            except Exception:
                pass
            try:
                self.handle.terminate()
            except Exception:
                pass

    # -- driving -----------------------------------------------------------

    def _state(self):
        return self.app.debug_state() or {}

    def _wait(self, pred, why, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.15)
        self.fail(why)

    def _content_tile(self):
        """A content tile carrying a click handler, with an aim point in
        WINDOW pixels -- which is what mpv's `mouse` command speaks. Read
        from the pushed scene rather than from `layout()`, which answers in
        logical coordinates.

        Two filters, and both were paid for. **`ctx` is what tells an item
        tile from the row's own chrome**: a row's page-left/page-right arrows
        are `row-`-prefixed and carry `click` too, so "the first clickable
        `row-` node" picks a 42x42 arrow whose whole job is to scroll the
        strip -- it activates nothing, and the failure reads exactly like a
        dead mouse. A tile carries a context handler and an arrow does not.
        (Carrying one is not the same as *opening* a menu -- a Libraries
        tile has `ctx` and opens nothing. See the right-click test.) **And
        the aim point has to be on screen**: a strip is far wider than the
        window (4812px against 1280 here), so most of its tiles are scrolled
        out and a pointer sent to one never arrives.
        """
        return self._tile("row-", "content tile on the home screen")

    def _grid_tile(self):
        """A tile on a library grid. `grid-` rather than "anything with a
        menu": `nav-back` is chrome, carries `ctx`, and its menu opens --
        so a looser filter silently tests the back button's menu instead of
        an item's, which is the hole this file is here to close."""
        return self._tile("grid-", "tile on the library grid")

    def _tile(self, prefix, what):
        state = self._state()
        ww, wh = state.get("w") or 0, state.get("h") or 0
        for n in (self.app._nodes or []):
            if not str(n.get("id") or "").startswith(prefix):
                continue
            if not (n.get("click") and n.get("ctx")):
                continue
            if not (n.get("w") and n.get("h")):
                continue
            # The UPPER QUARTER, not the centre: hovering a tile floats a
            # play chip over its middle, so a centred aim reports
            # `<tile>-play` as the hovered node and a click lands on the
            # chip -- a different control with a different job. Aiming at
            # the poster is also what a user does.
            cx = int(n["x"] + n["w"] / 2)
            cy = int(n["y"] + n["h"] * 0.25)
            if 0 <= cx < ww and 0 <= cy < wh:
                return n["id"], cx, cy
        self.fail("no on-screen %s in the pushed scene" % what)

    def _click(self, button, node_id, x, y):
        """Position, confirm the pointer really landed, then press.

        The confirm is not ceremony. On a real desktop -- the Windows VM
        runs these in console session 1, with no Xvfb -- the physical
        pointer shares this exact input path, so a stray real move can land
        between positioning and the press and take the click somewhere else.
        Under Xvfb nothing ever moves a mouse and this always passes first
        try; on Windows it is the difference between a failure that names
        the cause and one that reads as "the click did nothing".
        """
        # **mpv drops a move to the position it is already at**
        # (`set_mouse_pos`: `if (ictx->mouse_raw_x == x && ...) return;`), so
        # clicking the same tile twice in one test -- which is exactly what
        # "still works after a round trip" does -- delivers no second event
        # and leaves `hover` wherever the repaint left it. Nudge first, so
        # the move to the target is always a change mpv will deliver.
        self.handle.command("mouse", x + 8, y + 8)
        time.sleep(0.15)
        self.handle.command("mouse", x, y)
        self._wait(
            lambda: self._state().get("hover") == node_id,
            "the pointer never came to rest on %s (hover=%r) -- mpv did not "
            "deliver the position, so the click below would prove nothing"
            % (node_id, self._state().get("hover")),
            timeout=5.0)
        self.handle.command("keydown", button)
        time.sleep(0.1)
        self.handle.command("keyup", button)

    # -- transitions -------------------------------------------------------

    def _repaint(self):
        """Re-push the scene, and wait for it.

        **The renderer hit-tests against the scene it last received, and
        `set_active(True)` does not itself produce one.** Measured: after a
        resume the pointer sits at a tile's exact centre, mpv reports that
        position, the tile is in the tree -- and `hover` stays None until
        something repaints, after which it lands within 250ms.

        This is the harness standing in for the browser, which rebuilds when
        it takes the window back; these tests drive `app.set_active` directly
        and get no rebuild for free. It compensates for nothing under test:
        a repaint pushes a scene, it does not enable a key binding, and the
        negative control confirms the transition assertions still fail with
        `ui_resume`'s `enable_key_bindings` removed.
        """
        self.app.invalidate()
        time.sleep(0.8)

    def _leave_and_return_to_browse(self):
        """The plain round trip: browse -> playback -> browse."""
        self.app.set_active(False)
        time.sleep(0.4)
        self.app.set_active(True)
        time.sleep(0.4)
        self._repaint()

    def _summoned_hud_and_back(self):
        """The common one. A summoned HUD sets `active` itself, so the app's
        later "yes" does not change it -- which is how `ui_resume` came to be
        skipped entirely for the keyboard (`f70ad1e7`)."""
        self.app.set_active(False)
        time.sleep(0.3)
        self.app.set_hud(True)
        time.sleep(0.3)
        self.app.summon_hud()
        time.sleep(0.3)
        self.app.set_hud(False)
        time.sleep(0.3)
        self.app.set_active(True)
        time.sleep(0.4)
        self._repaint()

    def _assert_a_click_activates(self, why):
        node_id, x, y = self._content_tile()
        before = dict(self.browser.route)
        self._click("MBTN_LEFT", node_id, x, y)
        self._wait(lambda: self.browser.route != before,
                   "%s (clicked %s; the route is still %r)"
                   % (why, node_id, before.get("kind")))

    # -- the tests ---------------------------------------------------------

    def test_a_real_click_activates_a_tile(self):
        """The premise. Without it the transition tests below prove nothing,
        because a mouse that never worked is trivially still not working.

        Deliberately overlapping `test_mpvtk_browser`'s
        `test_a_real_click_reaches_the_button`, which asserts the same thing
        against a synthetic Button in a toolkit-only scene. Measured: a
        control that defined `mpvtk_mouse` and never enabled it fails both.
        """
        self._assert_a_click_activates(
            "a real left click did not activate a tile on a freshly started "
            "browser -- the mouse section was never enabled at startup")

    def test_a_real_click_still_activates_after_a_playback_round_trip(self):
        """The premise is the sibling test above rather than a click here
        first: **a click NAVIGATES**, so a "does it work before?" click
        leaves a grid page whose tiles are not `row-`-prefixed and the real
        assertion then has nothing to land on. The keyboard file can assert
        both halves in one test because moving focus changes no page.

        `ui_suspend` drops the mouse for playback -- there the buttons are
        mpv's own -- and only `ui_resume` puts it back. This is the half no
        startup test can see: the library comes up with a working mouse and
        loses it the moment anything plays.
        

        **Overlaps `test_mpvtk_browser`'s
        `ClassicOscReleasesTheMouseTest.test_browse_takes_it_all_back_afterwards`,
        deliberately.** Measured: a control removing `ui_resume`'s
        `enable_key_bindings('mpvtk_mouse', ...)` fails both. That one asserts
        on `input-bindings` metadata -- our sections are back at priority >= 0
        -- against an empty scene with no browser and no tiles. This one
        asserts the effect a user gets, so it is downstream of the scene, the
        hit test, the handler wiring and the repaint as well. Keep both: when
        they disagree, the pair says which half moved.
        """
        self._leave_and_return_to_browse()
        self._assert_a_click_activates(
            "a real left click is dead after leaving playback -- the library "
            "has no mouse for the rest of the session")

    def test_a_real_click_still_activates_after_a_summoned_hud(self):
        """The path `f70ad1e7` calls the common one: clicking Back on a
        visible bar is how you leave a film. A summoned HUD sets `active`
        itself, so the app's later "yes" is a no-op and `ui_resume` never
        runs.

        **Overlaps `test_mpvtk_browser`'s
        `ClassicOscReleasesTheMouseTest.test_browse_takes_it_all_back_afterwards`,
        deliberately.** Measured: a control removing `ui_resume`'s
        `enable_key_bindings('mpvtk_mouse', ...)` fails both. That one asserts
        on `input-bindings` metadata -- our sections are back at priority >= 0
        -- against an empty scene with no browser and no tiles. This one
        asserts the effect a user gets, so it is downstream of the scene, the
        hit test, the handler wiring and the repaint as well. Keep both: when
        they disagree, the pair says which half moved.
        """
        self._summoned_hud_and_back()
        self._assert_a_click_activates(
            "a real left click is dead after browse resumed from a summoned "
            "HUD")

    def test_a_real_right_click_opens_a_grid_tiles_menu(self):
        """Right-click, on the screen where it is least covered.

        `test_route_walk`'s interaction sweep calls `on_context` directly,
        and its own negative control "is caught on the home rows and NOT on
        the grid, detail or music screens: they build the same
        `TileRenderer.image_map` lambda but wire `on_context` to something
        else". So a grid tile's menu is asserted by nothing proven able to
        fail -- through the handler OR through the button.

        Getting to the grid by a real click rather than `navigate()` is not
        ceremony either: it makes the left button's route the premise of the
        right button's, so a failure here cannot be read as "the click broke
        and the menu is fine".

        **A Libraries tile is not an item**: measured, `row-libs-*` carries
        `ctx` and opens no menu on either path, so this cannot be written
        against the home screen's first menu-bearing tile.
        """
        node_id, x, y = self._content_tile()
        before = dict(self.browser.route)
        self._click("MBTN_LEFT", node_id, x, y)
        self._wait(lambda: self.browser.route.get("kind") == "grid",
                   "a real click on %s did not open a library grid (route "
                   "is %r)" % (node_id, before.get("kind")))
        self._repaint()

        tile, tx, ty = self._grid_tile()
        self._click("MBTN_RIGHT", tile, tx, ty)
        self._wait(lambda: self._state().get("menu_open"),
                   "a real right click on grid tile %s opened no menu" % tile)


if __name__ == "__main__":
    unittest.main()
