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
        self.libraries = self.source.get_libraries(_e2e.SOURCE_UUID)

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
        found = self._find_tile(prefix)
        if found is None:
            self.fail("no on-screen %s in the pushed scene" % what)
        return found

    def _scroll_offset(self):
        """How far the page has been scrolled, in the units the pushed
        coordinates are NOT in.

        **A pushed node's `y` is its unscrolled position.** The renderer
        applies the container's offset itself (`eff`), so a page scrolled
        240px still reports every node where it would sit at rest --
        measured: wheeling a series page moved `scroll` to 240 while the
        seasons row stayed at y=702.0 in `app._nodes`. Aiming at the raw
        coordinate lands 240px off, which the hover gate in `_click`
        reports rather than clicking whatever is really there.

        Returns None when more than one container is scrolled, because then
        this cannot say which one a given node is in without the tree; the
        caller treats that as "cannot aim here".
        """
        sc = self._state().get("scroll")
        if not isinstance(sc, dict) or not sc:
            return 0
        if len(sc) > 1:
            return None
        return list(sc.values())[0]

    def _find_tile(self, prefix):
        state = self._state()
        ww, wh = state.get("w") or 0, state.get("h") or 0
        offset = self._scroll_offset()
        if offset is None:
            return None
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
            cy = int(n["y"] + n["h"] * 0.25 - offset)
            if 0 <= cx < ww and 0 <= cy < wh:
                return n["id"], cx, cy
        return None

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


    def test_the_browsers_own_resume_leaves_the_mouse_live(self):
        """Through `_yield()` / `enter_browse()` -- the browser's own round
        trip -- and **with no repaint from the test**.

        This is the one the other three in this file structurally cannot be.
        They drive `app.set_active`, which pushes no scene, so they call
        `_repaint()` themselves -- and that is exactly the call whose absence
        would break the app. A test that supplies the missing repaint cannot
        notice it is missing.

        What is asserted is the property -- a resume repaints -- and not the
        line that provides it, because **`enter_browse` provides it twice**:
        its closing `self.invalidate()`, and the `refresh_userdata(now=True)`
        above it. Measured: deleting the `invalidate()` alone changes
        nothing here, and only removing BOTH turns this red. That redundancy
        is worth knowing before "simplifying" either one, and it is why the
        assertion is written against what the user gets rather than against
        a call.

        The failure it guards is total and self-sustaining, not cosmetic.
        `_yield` leaves an EMPTY scene behind (measured: overlays 0, no tile
        in the tree), so `node_at` matches nothing and `hover` stays nil --
        and `on_mouse_move` only calls `request_render` when hover CHANGES,
        so nil-to-nil asks for nothing. No scene, no hover, no render, no
        scene. Moving the pointer never recovers it; the library is dead to
        the mouse for the rest of the session.

        Control: with both repaint sources removed from `enter_browse` this
        test fails -- on the tile wait, naming the missing repaint -- and the
        other four in this file still pass.
        """
        self.browser._yield()
        time.sleep(0.5)
        self.browser.enter_browse()
        # Wait for a tile to come BACK, rather than repainting to make one:
        # the repaint is the thing under test.
        self._wait(lambda: self._find_tile("row-") is not None,
                   "the browser's own resume never put a tile back on "
                   "screen -- nothing repainted after enter_browse")
        self._assert_a_click_activates(
            "a real left click is dead after the browser's own resume, with "
            "no repaint from the test -- enter_browse left the renderer with "
            "no node table and pointer input cannot recover it")


    # -- the skip button, and what it leaves behind (#737 / A4) ------------

    def _hud_idle(self, click_pauses):
        """HUD mode with the bar auto-hidden -- most of playback.

        Not summoned: `phud_skip_bind` returns early while the bar is
        `shown`, and the standalone Skip button only exists when it is not.
        `hide` is short so nothing here waits on a real auto-hide.
        """
        self.app.set_active(False)
        time.sleep(0.3)
        self.app.set_hud(True, {"hide": 2, "mode": "hover",
                                "click": click_pauses})
        self._wait(lambda: self._state().get("phud_mode") is True,
                   "the renderer never entered HUD mode, so neither the "
                   "skip button nor the summon bindings exist")
        self._wait(lambda: not self._state().get("phud_shown"),
                   "the HUD came up shown; the bindings under test are the "
                   "hidden bar's")

    def test_a_real_click_still_activates_after_a_skip_segment(self):
        """#737, through mpv rather than through the renderer's handlers.

        With `mouse_click_pauses` off, `phud_skip_bind` forces `mbtn_left`
        as `mpvtk_skip_click` for the few seconds the standalone Skip button
        is up -- and a forced binding outranks the browser's own
        `mpvtk_mouse` section, so if the segment ends without releasing it,
        every later click runs its else-branch (`begin-vo-dragging`) instead
        of reaching a tile. The reporter's words: "the UI becomes
        unresponsive after a few videos ... switching back to left click to
        pause this behavior never occurs".

        **This is the leg the Lua suite cannot be.** `tests/lua/fake_mp.lua`
        dispatches by binding NAME, so it can show the binding is gone and
        not that a leaked one would have preempted anything -- the fake says
        so itself. The preemption is mpv's section stack, and that needs
        mpv.

        Control: deleting `mp.remove_key_binding('mpvtk_skip_click')` from
        `phud_skip_unbind` turns this red on the click, and leaves every
        other test in this file green.
        """
        self._hud_idle(click_pauses=False)
        self.app.set_hud_skip("Skip Intro")
        # Well inside `PHUD_SKIP_S` (10s), which is the button's own
        # auto-hide: a wait that outlasts it would watch the segment arm
        # and disarm, and then report that it never armed.
        self._wait(lambda: self._state().get("phud_skip") is True,
                   "no skip segment ever armed within 5s, so "
                   "`mpvtk_skip_click` was never bound and there is nothing "
                   "to leak", timeout=5.0)
        self.app.set_hud_skip("")
        self._wait(lambda: not self._state().get("phud_skip"),
                   "the skip button never went away")

        self.app.set_hud(False)
        time.sleep(0.3)
        self.app.set_active(True)
        time.sleep(0.4)
        self._repaint()
        self._assert_a_click_activates(
            "a real left click is dead in the library after one skip "
            "segment -- a forced mbtn_left outlived the button and is "
            "dragging the window instead of reaching the tile")

    def test_the_right_button_pauses_over_a_hidden_hud(self):
        """A4, through a real MBTN_RIGHT rather than on a node.

        In mpv's modality (`mouse_click_pauses` off) the left button drags
        the window and the right one pauses. `on_rclick` does that only
        while the bar is `shown`; the bar is hidden for most of playback,
        and what used to cover the gap was mpv's own default. Our pin move
        took it away -- v0.41.0 binds MBTN_RIGHT to `cycle pause`, master to
        `script-binding select/context-menu` -- so right click over a hidden
        HUD did nothing at all.

        The two existing MBTN_RIGHT presses in this file are both on library
        tiles, which is a different question with the same button.

        Control: removing the `mpvtk_phud_rclick` binding from
        `phud_bind_summon` turns this red.
        """
        self._hud_idle(click_pauses=False)
        st = self._state()
        # Mid-window, where a hidden HUD is nothing but picture. The skip
        # button is down, so `hit_skip` cannot claim this press.
        self.handle.command("mouse", int((st.get("w") or 2) / 2),
                            int((st.get("h") or 2) / 2))
        time.sleep(0.3)
        before = bool(self.handle.pause)
        self.handle.command("keydown", "MBTN_RIGHT")
        time.sleep(0.1)
        self.handle.command("keyup", "MBTN_RIGHT")
        self._wait(
            lambda: bool(self.handle.pause) != before,
            "a real right click over a hidden HUD did not toggle pause "
            "(pause is still %r) -- nothing of ours holds MBTN_RIGHT and "
            "mpv's own default on this pin is the context menu" % before,
            timeout=5.0)

    def _navigate(self, label, route):
        """Go to a screen by route, then wait for the renderer to hold it.

        Navigation by `navigate()` rather than by clicking through: what is
        under test is the right button on the page, and reaching a season
        three real clicks deep would make an unrelated failure look like a
        dead mouse.
        """
        self.browser.navigate(dict(route))
        self.assertEqual(self.browser.route.get("kind"), route["kind"],
                         "%s did not become the current route" % label)
        self._wait(lambda: not self.browser.route.get("_loading"),
                   "%s never finished loading, so its scene is a spinner "
                   "and a right click would land on nothing" % label)
        self._repaint()

    def _scroll_to(self, prefix, label):
        """Wheel down until a `prefix` tile has an aim point on screen.

        A detail page is taller than the window -- the seasons row starts
        18px above the bottom edge on a 720px window and runs 271px down --
        so the tile is in the tree and unreachable by pointer. A user
        scrolls to it, and so does this.

        The pointer is parked first because **a wheel event goes to whatever
        is under it**, and under Xvfb nothing has ever moved a mouse: the
        position sits at (-1, -1), the notch is delivered to nobody, and the
        page never moves. See `tests/e2e/README.md`.
        """
        st = self._state()
        self.handle.command("mouse", int((st.get("w") or 2) / 2),
                            int((st.get("h") or 2) / 2))
        time.sleep(0.3)
        for _ in range(30):
            if self._find_tile(prefix) is not None:
                return
            self.handle.command("keypress", "WHEEL_DOWN")
            time.sleep(0.2)
        self.fail("wheeling never brought a %s tile on screen on %s"
                  % (prefix, label))

    def _assert_right_click_opens_a_menu(self, label, prefix):
        self._scroll_to(prefix, label)
        tile = self._find_tile(prefix)
        if tile is None:
            self.fail("no on-screen %s tile to right-click on %s"
                      % (prefix, label))
        node_id, x, y = tile
        self._click("MBTN_RIGHT", node_id, x, y)
        self._wait(lambda: self._state().get("menu_open"),
                   "a real right click on %s (%s) opened no menu"
                   % (node_id, label))
        self.browser._close_menu()

    def test_a_real_right_click_opens_a_menu_on_detail_and_music(self):
        """The two screens `tests/e2e/README.md` names as unproven, after
        the grid.

        Its `_interact` sweep fires `on_context` from the scene's handler
        map and a negative control that made `_open_tile_menu` raise "is
        caught on the home rows and NOT on the grid, detail or music
        screens". Every tile context handler in the browser does route to
        `_open_tile_menu` -- `app.py` assigns `self.tiles.on_context` once
        and nothing else assigns it -- so the gap is not that these pages
        wire something different: it is that the sweep never reaches a
        context handler on them at all. Pressing the button reaches it.

        One test over both screens, and over a series as well as an album,
        because the shared handler means the interesting variable is the
        PAGE that draws the tiles rather than the item type -- and between
        them they cover both wirings that reach it, an image_map tile and a
        Table row.
        """
        series = self.session.find_all(library="Shows", item_type="Series",
                                       Limit=1)
        if not series:
            self.skipTest("no Series to open")
        self._navigate("series", {"server": _e2e.SOURCE_UUID,
                                  "item_id": series[0]["Id"],
                                  "title": series[0].get("Name", ""),
                                  "kind": "series"})
        # A SEASON tile, not one of the `detail-people-*` cast tiles beside
        # it: `_open_tile_menu` deliberately opens nothing for a type with
        # no entries on offer, so a cast member is a legitimate no-menu and
        # would fail this as though the button were dead.
        self._assert_right_click_opens_a_menu("series", "series-seasons-")

        libs = {lib["Name"]: lib for lib in self.libraries}
        music = libs.get("Music")
        if music is None:
            self.skipTest("no Music library")
        albums = self.source.get_music_albums(
            _e2e.SOURCE_UUID, music["Id"], start_index=0, limit=1)[0]
        if not albums:
            self.skipTest("no albums")
        self._navigate("album", {"server": _e2e.SOURCE_UUID,
                                 "item_id": albums[0]["Id"],
                                 "title": albums[0].get("Name", ""),
                                 "kind": "album"})
        # A track ROW, which reaches `on_context` through Table rather than
        # through `TileRenderer.image_map`'s lambda -- a third wiring, and
        # the one an album page actually offers.
        self._assert_right_click_opens_a_menu("album", "trk-")


if __name__ == "__main__":
    unittest.main()
