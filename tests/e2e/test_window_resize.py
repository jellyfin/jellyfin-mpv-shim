"""The window changes size under the UI.

The browser draws *inside the player's mpv window*, so its layout is a
function of a size mpv owns and a window manager can change at any moment —
a tiling WM does it the instant the window appears, and the developer whose
i3 kept reshaping these windows is why the rest of this suite runs under
xvfb.

Everything downstream of that size is recomputed on a resize: how many tiles
fit a row, the pitch a scroll offset is measured against, and every
composited strip (a strip is baked at one tile size, so a resize invalidates
the lot). The failures are the ones you would expect from that list and none
of them raise — the UI keeps drawing at the old size, or it redraws and
throws away where you were.

A fake cannot show any of it. `layout()` is pure and will happily lay out any
size you hand it, so a test against a fake asserts that a number it chose was
used. The question here is whether the renderer *notices*, which needs a real
window that really changed.

**What is pinned, and what is not.** These assert that the renderer notices
the new size, keeps drawing, keeps focus and stays usable across it — the
four ways a resize has broken. They do *not* verify that every strip was
re-baked at the new tile size; an overlay count cannot tell a fresh bitmap
from a stale one, and the tile geometry is not in `debug_state`. A strip
drawn at the old size would still be counted here.

This is also the one module that deliberately makes the window small. The
rest of the suite guards `h >= 200` and calls a squashed window an
environment problem, because for them it is; here a window nobody can use is
the case under test.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

import _harness as h  # noqa: E402,F401
from test_mpvtk_browser import _spawn_handle  # noqa: E402

LIBRARY = "Movies"
GRID = "grid"


@_e2e.require_server_and_mpv
class WindowResizeTest(unittest.TestCase):

    def setUp(self):
        from jellyfin_mpv_shim.mpvtk.app import MpvtkApp
        from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore, cache_dir
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore

        self.session = _e2e.Session()
        self.addCleanup(self.session.stop)
        self.source = self.session.library_source()
        self.addCleanup(self.source.stop)
        libs = self.source.get_libraries(_e2e.SOURCE_UUID)
        match = [lib for lib in libs if lib["Name"] == LIBRARY]
        if not match:
            self.skipTest("no %r library" % LIBRARY)
        self.library = match[0]

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-resize-")))
        self.browser = MpvtkBrowser(self.app, self.source,
                                    server_uuid=_e2e.SOURCE_UUID, strips=strips)
        self._thread = threading.Thread(
            target=lambda: self.app.run(self.browser.build), daemon=True)
        self._thread.start()
        self.addCleanup(self._teardown)
        self.assertTrue(self.app.ready.wait(20), "renderer never came up")

        self.browser.navigate({
            "kind": GRID, "server": _e2e.SOURCE_UUID,
            "parent_id": self.library["Id"], "title": LIBRARY,
            "collection_type": self.library.get("CollectionType")})
        self.assertTrue(
            self._settle(), "the grid never composited anything to resize")
        self.start = (self._state().get("w"), self._state().get("h"))
        self.assertTrue(
            all(self.start) and self.start[1] >= 200,
            "the window came back %sx%s, so there is nothing to shrink from. "
            "Run under xvfb." % self.start)

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

    def _settle(self, timeout=25.0, quiet=1.0):
        """Wait for compositing to stop changing. Returns the overlay count."""
        deadline = time.time() + timeout
        last, since = None, None
        while time.time() < deadline:
            now = self._state().get("overlays") or 0
            if now and now == last:
                since = since or time.time()
                if time.time() - since >= quiet:
                    return now
            else:
                since = None
            last = now
            time.sleep(0.2)
        return self._state().get("overlays") or 0

    def _resize(self, width, height, timeout=15.0):
        """Change the window's size and wait for the renderer to agree.

        `geometry` rather than a WM call: there is no window manager under
        xvfb, which is the point — this isolates "the renderer noticed the
        size changed" from "a WM did something", and the app has to survive
        both.
        """
        self.handle.command("set", "geometry", "%dx%d" % (width, height))
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = self._state()
            if (state.get("w"), state.get("h")) == (width, height):
                return True
            time.sleep(0.2)
        state = self._state()
        self.fail("asked for %dx%d and the renderer is still drawing at %sx%s "
                  "— the UI is laid out for a window that no longer exists"
                  % (width, height, state.get("w"), state.get("h")))

    # -- the tests ---------------------------------------------------------

    def test_the_scene_follows_the_window_both_ways(self):
        """Growing and shrinking are not symmetric — a layout that caches a
        width can be right on the way up and stale on the way down."""
        for size in ((980, 560), (1500, 940), (760, 430)):
            with self.subTest(size=size):
                self._resize(*size)
                self.assertGreater(
                    self._settle(), 0,
                    "nothing is composited at %dx%d, so the window is blank "
                    "after a resize" % size)

    def test_a_resize_does_not_blank_the_library(self):
        """The failure that reads as a crash: the strips are all invalidated
        by the new tile size and nothing replaces them."""
        before = self._settle()
        self.assertGreater(before, 0)
        self._resize(1500, 940)
        after = self._settle()
        self.assertGreater(
            after, 0,
            "the library went blank after a resize (%d strips before, %d "
            "after)" % (before, after))

    def test_focus_survives_a_resize(self):
        """A resize re-lays out the whole scene. Node ids are built from item
        ids so they should be stable across it, and if they are not the
        focused node is gone and the keyboard lands nowhere."""
        for key in ("DOWN", "RIGHT"):
            self.handle.command("keypress", key)
            time.sleep(0.2)
        focused = self._state().get("nav")
        if not focused:
            self.skipTest("could not put focus on anything to begin with")
        self._resize(1500, 940)
        self._settle()
        self.assertEqual(
            self._state().get("nav"), focused,
            "focus moved from %r to %r across a resize" % (
                focused, self._state().get("nav")))

    def test_a_window_too_small_to_use_does_not_crash_it(self):
        """A WM is free to make the window any size it likes, including one
        with no room for a tile row. It has to come out the other side.

        Deliberately below the guard every other module in this suite treats
        as an environment failure — for them a squashed window means the
        assertions cannot be trusted; here it is the case under test.
        """
        self._resize(320, 200)
        time.sleep(1.5)
        self.assertTrue(self._thread.is_alive(),
                        "the render loop died on a tiny window")
        # And it comes back: a layout that divided by an available width
        # could leave permanent damage rather than merely drawing nothing.
        self._resize(*self.start)
        self.assertGreater(
            self._settle(), 0,
            "the library never came back after the window was squashed and "
            "restored")

    def test_navigation_still_works_at_the_new_size(self):
        """Not just pixels: the handlers are rebuilt too, and a scene that
        redrew without them is a picture of a library rather than one."""
        self._resize(1500, 940)
        self._settle()
        before = self._state().get("nav")
        moved = None
        for _attempt in range(6):
            for key in ("DOWN", "RIGHT", "UP", "LEFT"):
                self.handle.command("keypress", key)
                time.sleep(0.12)
                now = self._state().get("nav")
                if now and now != before:
                    moved = now
                    break
            if moved:
                break
        self.assertIsNotNone(
            moved,
            "no arrow key moved focus after a resize — the scene redrew but "
            "nothing in it is reachable")


if __name__ == "__main__":
    unittest.main()
