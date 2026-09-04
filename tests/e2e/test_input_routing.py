"""Real keys through mpv's input layer, across every UI transition.

This is the surface with the worst recent record. **Three regressions in
48 hours (2026-08-01/02), every one found by hand**, all the same shape: a
key-binding section that was never re-enabled after a transition, so the
library came back from playback with no keyboard at all.

`f70ad1e7` says what the suite could see and what it could not:

    The fake mpv's enable/disable_key_bindings were no-ops, which is why the
    tests covering that commit could only assert which section a binding was
    DECLARED in.

Declaring a binding and *enabling its section* are different calls, and only
mpv holds the second. So the tests were green while, in the reporter's words,
"from the first video played, the library had no arrow, ENTER, TAB or MENU
navigation for the rest of the session".

The two paths that broke are both ordinary, and neither involves an error:

* `set_active(False)` then `set_active(True)` — leaving a video.
* a **summoned HUD**, which sets `active` itself, so the app's later "yes" is
  a no-op and `ui_resume` never runs. `f70ad1e7` calls this "the common one —
  clicking Back on a visible bar is how you leave a film."

So nothing here asserts on which section a binding is declared in. Every test
presses a real key and checks the UI moved.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

# _e2e puts tests/integration on the path; both of these live there.
import _harness as h  # noqa: E402
from test_mpvtk_browser import _spawn_handle  # noqa: E402


@_e2e.require_server_and_mpv
class InputRoutingTest(unittest.TestCase):
    """A real browser on a real mpv, against the real library."""

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.session = _e2e.Session()
        # `_focus_a_content_tile` focuses whatever the home screen drew, so a
        # stale layout changes which tile is under test.
        _e2e.normalise_home_layout(self.session)
        self.addCleanup(self.session.stop)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)
        self.libraries = self.source.get_libraries(_e2e.SOURCE_UUID)
        self.assertTrue(self.libraries, "no libraries to navigate")

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-input-")))
        self.browser = MpvtkBrowser(self.app, self.source,
                                    server_uuid=_e2e.SOURCE_UUID, strips=strips)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.addCleanup(self._teardown)
        self.assertTrue(self.app.ready.wait(20), "renderer never came up")
        self._wait(lambda: (self._state().get("overlays") or 0) >= 1,
                   "the browser never rendered a strip, so there is nothing "
                   "to navigate")
        # A window manager is free to ignore the requested geometry, and a
        # squashed window has no rows to move between — which would fail as
        # "the keyboard is dead" rather than as the environment problem it
        # is. Same guard, and same reason, as test_mpvtk_browser.
        state = self._state()
        self.assertGreaterEqual(
            state.get("h") or 0, 200,
            "the window came back %sx%s — too short for a tile row, so "
            "focus has nowhere to go. Run under xvfb."
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

    def _keypress(self, key):
        """A real key, through mpv's input layer — not a synthesised event.

        Which is the entire point: a synthesised event reaches the handler
        whether or not its section is enabled, so it cannot see these bugs.
        """
        self.handle.command("keypress", key)

    def _click_key(self, key):
        """Press AND release, for a binding that acts on the release.

        `mpvtk_thumb` binds mbtn_back as a complex binding whose *third*
        element runs on release (it fires ESC), so a plain `keypress` is not
        guaranteed to deliver the edge that does the work.
        """
        self.handle.command("keydown", key)
        time.sleep(0.1)
        self.handle.command("keyup", key)

    def _focus_a_content_tile(self):
        """Arrow onto any tile in any home row and return its node id.

        By steering rather than by parking a guessed id on `app.focus`: an id
        has to be in the scene for a park to land, and which rows the home
        screen has — and which of their tiles are materialised — depends on
        the user's saved layout and on what the library holds. Any `row-`
        tile navigates somewhere, which is all these tests need; insisting on
        a *library* tile made them fail on a home screen whose Libraries row
        the wander never reached.
        """
        seen = set()
        for _ in range(40):
            for key in ("DOWN", "RIGHT", "UP", "LEFT"):
                self._keypress(key)
                time.sleep(0.1)
                nav = self._nav()
                if nav and nav.startswith("row-"):
                    return nav
                if nav:
                    seen.add(nav)
        self.fail("never reached a content tile by arrowing; focus visited %r"
                  % (sorted(seen)[:8],))

    def _wait(self, pred, why, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if pred():
                return True
            time.sleep(0.15)
        self.fail(why)

    def _nav(self):
        return self._state().get("nav")

    def _nav_moves(self, keys=("DOWN", "RIGHT", "UP", "LEFT"), presses=6):
        """Does *any* arrow key move focus? Returns the node it landed on.

        Tries several directions because focus may already be against an edge
        — a single DOWN at the bottom of a screen legitimately does nothing,
        and reading that as "the keyboard is dead" would be a flaky test that
        cried wolf about the exact bug it exists to find.
        """
        start = self._nav()
        for _ in range(presses):
            for key in keys:
                self._keypress(key)
                time.sleep(0.12)
                now = self._nav()
                if now and now != start:
                    return now
        return None

    def _leave_and_return_to_browse(self):
        """The plain round trip: browse -> playback -> browse."""
        self.app.set_active(False)
        time.sleep(0.4)
        self.app.set_active(True)
        time.sleep(0.4)

    def _summoned_hud_and_back(self):
        """The common one. A summoned HUD sets `active` itself, so the app's
        later "yes" does not change it — and the early return below that used
        to skip `ui_resume` entirely."""
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

    # -- the tests ---------------------------------------------------------

    def test_arrows_navigate_from_launch(self):
        """#614's half: the keys were dead from launch and only started
        working after a playback round trip happened to hide the bar."""
        landed = self._nav_moves()
        self.assertIsNotNone(
            landed,
            "no arrow key moved focus on a freshly started browser — the nav "
            "section was never enabled at startup")

    def test_arrows_still_navigate_after_a_playback_round_trip(self):
        """`f70ad1e7`: `ui_suspend` drops the arrows for playback (there they
        are mpv's seek keys) and only `ui_resume` puts them back."""
        self.assertIsNotNone(self._nav_moves(),
                             "focus did not move before playback, so this "
                             "test could not tell the difference after")
        self._leave_and_return_to_browse()
        self.assertIsNotNone(
            self._nav_moves(),
            "arrow keys are dead after leaving playback — the library has no "
            "keyboard for the rest of the session")

    def test_arrows_still_navigate_after_a_summoned_hud(self):
        """The path `f70ad1e7` calls the common one: clicking Back on a
        visible bar is how you leave a film."""
        self.assertIsNotNone(self._nav_moves())
        self._summoned_hud_and_back()
        self.assertIsNotNone(
            self._nav_moves(),
            "arrow keys are dead after browse resumed from a summoned HUD")

    def test_enter_activates_after_a_playback_round_trip(self):
        """Focus moving is not enough — ENTER has to still reach the node."""
        self._leave_and_return_to_browse()
        self._focus_a_content_tile()
        before = self.browser.route.get("kind")
        self._keypress("ENTER")
        self._wait(lambda: self.browser.route.get("kind") != before,
                   "ENTER did not activate the focused tile after a playback "
                   "round trip")

    def test_menu_opens_and_mouse_back_dismisses_after_a_round_trip(self):
        """The MENU key and the thumb section, together.

        `#614` / `86f113e2`: "mouse Back did nothing in the library from
        launch, and started working only after a playback round trip that hid
        the bar first: intermittent." The thumb section is `mpvtk_thumb`, and
        `ui_resume` is what re-enables it.

        What mbtn_back *does* in browse is fire ESC, and in plain browse ESC
        has no binding at all — the forced ones belong to an open menu
        (`mpvtk_menu_esc`) and to the playback HUD (`mpvtk_phud_esc`). So it
        dismisses an overlay rather than paging back, and a test that
        expected it to navigate was asserting something the app has never
        done. Open the tile menu first, and both keys have a real observable.
        """
        self._leave_and_return_to_browse()
        self._focus_a_content_tile()

        self._keypress("MENU")
        self._wait(lambda: self._state().get("menu_open"),
                   "the MENU key did not open the tile menu after a playback "
                   "round trip")

        self._click_key("MBTN_BACK")
        self._wait(
            lambda: not self._state().get("menu_open"),
            "mouse Back did not dismiss the menu — the mpvtk_thumb section "
            "is not enabled in browse")

    def test_the_hud_does_not_leave_browse_holding_the_arrows(self):
        """The other direction, and the reason `ui_resume` takes `no_nav`:
        during plain playback the arrows are mpv's seek keys, so the UI must
        NOT be holding them."""
        self.assertIsNotNone(self._nav_moves(),
                             "focus did not move in browse to begin with")
        self.app.set_active(False)
        time.sleep(0.5)
        # In playback the renderer is idle; arrows belong to mpv. Focus must
        # not move, or seeking is broken instead.
        moved = self._nav_moves(presses=2)
        self.assertIsNone(
            moved,
            "the UI still moves focus on arrow keys during playback, so the "
            "arrows are not reaching mpv as seek keys (landed on %r)" % moved)


if __name__ == "__main__":
    unittest.main()
