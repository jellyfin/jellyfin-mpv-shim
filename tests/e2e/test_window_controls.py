"""The title bar we draw when the desktop draws none — whole chain.

Real server, real MPV, real renderer, real composited scene, both backends.

``tests/integration/test_window_controls.py`` asks whether MPV will *answer*
about this window's decorations, and whether a change reaches the callback.
This asks the next question: when the answer is "no title bar", do three
buttons actually reach the screen — through ``refresh_window_controls``,
``chrome_bar``, ``layout()`` and the Lua renderer — and do they go away again
when the answer changes back.

Why the whole chain is worth a leg of its own: every link is conditional and
none of them raise. The snapshot may not be re-taken, the bar may not be
rebuilt, and the marker that makes the bar draggable only produces a node
because ``window_drag`` forces one. Any of those failing leaves a window with
no way to close it and *no error anywhere* — on GNOME Wayland, which almost
nobody developing this client is running. That is exactly the kind of thing
that rots between releases.

**Read back out of the renderer, not out of build().** ``build()`` renders a
correct tree whenever it is asked, so asserting on it would pass even if no
scene carrying the controls had ever been pushed to MPV. The ``hover`` debug
hook resolves a node id against the renderer's OWN scene (``center_of``), so
it answers the question that matters: is this button on screen.

**Driven by ``--border``, which X11 honours.** On GNOME Wayland the
compositor decides and MPV writes the answer back into the same property; the
shim reads that property either way, so driving it here exercises the same
code the compositor would. Mutter's half is what no test harness can cover.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

import _harness as h  # noqa: E402,F401
from test_window_resize import _spawn_handle  # noqa: E402

CONTROLS = ("win-min", "win-max", "win-close")
#: A node that is on the top bar in every state, so "the hover did not move"
#: can be told apart from "it was already there".
ANCHOR = "nav-home"


@_e2e.require_server_and_mpv
class WindowControlsTest(unittest.TestCase):

    def setUp(self):
        from jellyfin_mpv_shim.conf import settings
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        prior = settings.window_controls
        self.addCleanup(setattr, settings, "window_controls", prior)
        settings.window_controls = "auto"

        self.session = _e2e.Session()
        self.addCleanup(self.session.stop)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-csd-")))
        self.controller = _Controller(self.handle)
        self.browser = MpvtkBrowser(self.app, self.source,
                                    server_uuid=_e2e.SOURCE_UUID,
                                    strips=strips, controller=self.controller)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.addCleanup(self._teardown)
        self.assertTrue(self.app.ready.wait(20), "renderer never came up")
        self.browser.navigate({"kind": "home", "server": _e2e.SOURCE_UUID})
        self._settle()
        self.assertTrue(
            self._renderer_has(ANCHOR),
            "the top bar never reached the renderer, so there is nothing to "
            "put window controls on")

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

    def _settle(self, timeout=20.0, quiet=0.8):
        deadline = time.time() + timeout
        last, since = None, None
        while time.time() < deadline:
            now = self._state().get("overlays") or 0
            if now and now == last:
                since = since or time.time()
                if time.time() - since >= quiet:
                    return
            else:
                since = None
            last = now
            time.sleep(0.2)

    def _hover(self, node_id, timeout=5.0):
        """Point the renderer at ``node_id`` and report where it landed.

        ``hover`` resolves the id against the renderer's own scene, so an id
        that is not on screen moves nothing at all -- which is why the caller
        parks the pointer somewhere known first.
        """
        self.app.debug(cmd="hover", id=node_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self._state().get("hover") == node_id:
                return node_id
            time.sleep(0.1)
        return self._state().get("hover")

    def _renderer_has(self, node_id, timeout=5.0):
        # Park on the anchor first: without it, "hover is not node_id" could
        # equally mean the pointer never moved because it was already there.
        if node_id != ANCHOR:
            self._hover(ANCHOR)
        return self._hover(node_id, timeout) == node_id

    def _set_border(self, value, timeout=10.0):
        self.handle.command("set", "border", "yes" if value else "no")
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if bool(self.handle.border) is value:
                    return True
            except Exception:
                pass
            time.sleep(0.1)
        return False

    def _apply(self, border):
        """Change the decorations and let the UI react, as the observer would.

        ``refresh_window_controls`` is exactly what
        ``playerManager.on_decorations_changed`` calls; that the observer
        fires at all is the integration leg's question, against the real
        singleton on both backends.
        """
        self.assertTrue(self._set_border(border),
                        "MPV would not take border=%s" % border)
        self.browser.refresh_window_controls()
        self._settle()

    # -- the tests ---------------------------------------------------------

    def test_a_decorated_window_draws_no_title_bar_of_its_own(self):
        self._apply(True)
        for node_id in CONTROLS:
            self.assertFalse(
                self._renderer_has(node_id, timeout=2.0),
                "a window with a real title bar was given a second one (%s)"
                % node_id)

    def test_losing_the_title_bar_puts_the_controls_on_screen(self):
        """The GNOME Wayland case, end to end."""
        self._apply(False)
        for node_id in CONTROLS:
            self.assertTrue(
                self._renderer_has(node_id),
                "MPV reports no title bar and %s never reached the screen — "
                "the window cannot be closed" % node_id)

    def test_they_go_away_again(self):
        self._apply(False)
        self.assertTrue(self._renderer_has("win-close"))
        self._apply(True)
        self.assertFalse(
            self._renderer_has("win-close", timeout=2.0),
            "the window got its title bar back and kept ours as well")

    def test_the_bar_is_draggable_exactly_when_it_is_the_title_bar(self):
        """``window_drag`` is what makes a press start a window move, and it
        is also what conjures a hit rect for a bar that has no fill of its own
        under a gradient theme. A node FLAG rather than a node, so it is read
        off the tree the renderer was handed."""
        from jellyfin_mpv_shim.mpvtk.layout import layout

        def draggable():
            nodes, _hand = layout(self.browser.build((1280, 720)), 1280, 720)
            return [n for n in nodes if n.get("wdrag")]

        self._apply(True)
        self.assertFalse(draggable(),
                         "the top bar drags a window that has a title bar")
        self._apply(False)
        self.assertTrue(draggable(),
                        "the window has no title bar and no way to move it")

    def test_never_holds_against_a_real_undecorated_window(self):
        from jellyfin_mpv_shim.conf import settings

        settings.window_controls = "never"
        self._apply(False)
        self.assertFalse(self._renderer_has("win-close", timeout=2.0))

    def test_always_holds_against_a_real_decorated_one(self):
        from jellyfin_mpv_shim.conf import settings

        settings.window_controls = "always"
        self._apply(True)
        self.assertTrue(self._renderer_has("win-close"))

    def test_clicking_close_reaches_the_window_close_path(self):
        """Clicked through the RENDERER, by id, so this covers the button
        being hittable where it is drawn -- not just that a handler exists.

        It must reach ``close_window`` and not some private shortcut:
        close_to_tray and the no-tray safeguard have to decide, once, in one
        place (mpvtk_browser.ui.on_window_closed)."""
        self._apply(False)
        self.assertTrue(self._renderer_has("win-close"))
        self.app.debug(cmd="click", id="win-close")
        deadline = time.time() + 5.0
        while time.time() < deadline and not self.controller.closed:
            time.sleep(0.1)
        self.assertEqual(self.controller.closed, 1,
                         "the close button does not reach the window-close "
                         "path, so close_to_tray is never consulted")

    def test_clicking_maximize_reaches_the_player(self):
        self._apply(False)
        self.assertTrue(self._renderer_has("win-max"))
        self.app.debug(cmd="click", id="win-max")
        deadline = time.time() + 5.0
        while time.time() < deadline and not self.controller.maximized:
            time.sleep(0.1)
        self.assertEqual(self.controller.maximized, 1)


class _Controller:
    """The window half of PlayerGateway, over the handle this test owns.

    ``window_chrome_state`` is the REAL ``WindowMixin`` method, driven by the
    real MPV: the decision under test must not be reimplemented here or the
    leg would only be checking its own arithmetic. The three actions are
    recorded instead of performed, because performing them would tear the
    window down halfway through the test.
    """

    def __init__(self, handle):
        from jellyfin_mpv_shim.player_window import WindowMixin

        class _Window(WindowMixin):
            def __init__(self, player):
                self._player = player
                self._mpv_alive = True
                self.on_decorations_changed = None

        self._window = _Window(handle)
        self.closed = 0
        self.minimized = 0
        self.maximized = 0

    def window_chrome_state(self):
        return self._window.window_chrome_state()

    def close_window(self):
        self.closed += 1

    def minimize_window(self):
        self.minimized += 1

    def toggle_window_maximized(self):
        self.maximized += 1


if __name__ == "__main__":
    unittest.main()
