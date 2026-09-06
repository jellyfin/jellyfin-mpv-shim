"""Scrolling a thousand-item library hard, in a real window.

The most-repeated hand-test complaint in this repo's history — four rounds
across two different UI implementations, always found by a human, never by a
test:

* 2026-06-27 (Tk): "about 2/3 through the scroll it stops scrolling despite
  being visibly not at the end."
* 2026-07-19 (mpvtk): "When scrolling far in infinite scroll, I get blank
  tiles when scrolling back up."
* 2026-07-23: "when I scroll down to the bottom and then scroll up about 3
  detents per second I get missing tiles and I have to scroll up a few more
  rows for it to recover" — alongside `mpv: main: Too many events queued`.
* 2026-08-02: "we should look at pagination/virtual scroll since the last work
  on that shipped a regression."

`tests/e2e/test_paging.py` covers the loader's arithmetic — which slot holds
which item — and passes. This is the other half and a different failure: the
data is there and the *picture* is not. It is a rate bug in the compositing
pipeline (mpv overlay churn, Pillow decode on a worker pool, image fetches
over the wire), so it needs a real window, a real server and real wheel
events at speed. None of the three fakes reproduce it: a synchronous pool
delivers every page instantly and in order, and no fake composites anything.

The assertion is the symptom as reported: **come back to a place you have
already been and the tiles must be there.** Overlays composited at the top
before the journey are the baseline; after scrolling to the bottom and
racing back, the same place must composite at least as much. "I have to
scroll up a few more rows for it to recover" is precisely a count that comes
back short and then catches up, so the check happens after a bounded settle
and not before.
"""

import os
import sys
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

import _harness as h  # noqa: E402
from test_mpvtk_browser import _spawn_handle  # noqa: E402

LIBRARY = "Bulk Movies"
GRID = "grid"


@_e2e.require_server_and_mpv
class ScrollRecoveryTest(unittest.TestCase):

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
            self.skipTest("%r is not on this server — this test is about "
                          "scale and there is none" % LIBRARY)
        self.library = match[0]

        self.handle, ext = _spawn_handle()
        self.app = MpvtkApp.attach(self.handle, ext=ext)
        strips = (StripStore(mem_store=MemoryStore()) if self.app.in_process
                  else StripStore(cache_dir=cache_dir("mpvtk-scroll-")))
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
        self._settle("the grid never composited a tile")
        self._point_at_the_grid()
        state = self._state()
        self.assertGreaterEqual(
            state.get("h") or 0, 200,
            "the window came back %sx%s — too short to scroll. Run under "
            "xvfb." % (state.get("w"), state.get("h")))

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

    def _overlays(self):
        return (self._state().get("overlays") or 0)

    def _offset(self):
        return ((self._state().get("scroll") or {}).get(GRID) or 0)

    def _point_at_the_grid(self):
        """Put the pointer over the grid before wheeling.

        The renderer routes a wheel event to the container *under the
        pointer*, and under xvfb nothing has ever moved a mouse — `mouse-pos`
        reports no hover and the coordinates sit at (-1, -1), so every wheel
        event is delivered and discarded. The first version of this test
        scrolled nothing: one assertion passed because the "before" and
        "after" were the same untouched frame, and the other failed as though
        the app stopped scrolling two thirds of the way through when in fact
        it had never started.
        """
        state = self._state()
        width, height = state.get("w") or 0, state.get("h") or 0
        self.assertTrue(width and height, "no render size to point at")
        self.handle.command("mouse", int(width * 0.5), int(height * 0.6))
        deadline = time.time() + 5
        while time.time() < deadline:
            if self._state().get("hover"):
                return
            time.sleep(0.2)
        self.fail("the pointer never came to rest over a tile, so wheel "
                  "events have nothing to scroll")

    def _wheel(self, key, count, delay):
        for _ in range(count):
            self.handle.command("keypress", key)
            if delay:
                time.sleep(delay)

    def _scroll_to_bottom(self, bursts=40, per_burst=25):
        """Wheel down until the offset stops rising. Returns the offset."""
        last = -1.0
        for _ in range(bursts):
            self._wheel("WHEEL_DOWN", per_burst, 0.015)
            time.sleep(0.5)
            now = self._offset()
            if now <= last + 1.0:
                return now
            last = now
        return self._offset()

    def _race_to_top(self, bursts=40, per_burst=60):
        """Wheel up as fast as the input layer will take it, until the top.

        Bounded by the offset rather than a wheel count: a thousand items is
        a very tall container and a fixed number of detents stops somewhere
        arbitrary in the middle, which is not the reported gesture and makes
        the before/after comparison meaningless.

        No delay inside a burst — the rate is the whole point. Every new
        window asks the thumbnail workers for two or three screens of
        artwork, and that churn is what produced "Too many events queued".
        """
        last = None
        for _ in range(bursts):
            self._wheel("WHEEL_UP", per_burst, 0.0)
            time.sleep(0.35)
            now = self._offset()
            if now <= 5.0:
                return now
            if last is not None and now >= last - 1.0:
                return now          # stopped moving; let the caller judge
            last = now
        return self._offset()

    def _settle(self, why, timeout=25.0, quiet=1.2):
        """Wait until compositing stops changing, then a beat more.

        The reported bug recovers if you keep scrolling, so a check taken
        mid-flight would pass for the wrong reason and one taken too early
        would fail for it. Settle on a stable overlay count.
        """
        deadline = time.time() + timeout
        last, stable_since = None, None
        while time.time() < deadline:
            now = self._overlays()
            if now and now == last:
                stable_since = stable_since or time.time()
                if time.time() - stable_since >= quiet:
                    return now
            else:
                stable_since = None
            last = now
            time.sleep(0.2)
        if not self._overlays():
            self.fail(why)
        return self._overlays()

    def _loaded_head(self, count=24):
        items = self.browser.route.get("_items") or []
        head = items[:count]
        return sum(1 for i in head if i is not None), len(head)

    # -- the tests ---------------------------------------------------------

    def test_tiles_come_back_after_scrolling_to_the_bottom_and_racing_up(self):
        """The 2026-07-23 report, as directly as it can be staged."""
        baseline = self._settle("nothing composited at the top to begin with")
        head_loaded, head_len = self._loaded_head()
        self.assertEqual(
            head_loaded, head_len,
            "the first screenful was not fully loaded before scrolling, so "
            "this test could not tell a regression from a slow start")

        # Down to the bottom, unhurried.
        bottom = self._scroll_to_bottom()
        self.assertGreater(
            bottom, 1000,
            "the grid barely moved (offset %s), so nothing below was ever "
            "visited and racing back up proves nothing" % bottom)
        self._settle("nothing composited at the bottom")

        # And back up as fast as the input layer will take it. This is the
        # part that breaks: every new window asks the thumbnail workers for
        # two or three screens of artwork, and the overlay churn is what
        # produced "Too many events queued".
        self._race_to_top()

        after = self._settle("nothing composited after racing back to the top")
        self.assertLessEqual(
            self._offset(), 5.0,
            "the scroll did not return to the top, so the comparison below "
            "is against a different part of the library (offset %s)"
            % self._offset())
        self.assertGreaterEqual(
            after, baseline,
            "back at the top after a fast scroll up, %d strips are "
            "composited where %d were before — these are the blank tiles, "
            "and they are what 'I have to scroll up a few more rows for it "
            "to recover' looks like" % (after, baseline))

        loaded, total = self._loaded_head()
        self.assertEqual(
            loaded, total,
            "%d of the first %d slots are empty after scrolling back, so the "
            "window dropped items it had already fetched" % (total - loaded,
                                                             total))

    def test_scrolling_reaches_the_end_of_a_thousand_items(self):
        """The 2026-06-27 report: "about 2/3 through the scroll it stops
        scrolling despite being visibly not at the end."

        Different UI, and the kind of thing that comes back. Keep wheeling
        and the offset must keep rising until it genuinely runs out.
        """
        total = self.browser.route.get("_total") or 0
        self.assertGreater(total, 500, "not a big enough library to matter")

        self._scroll_to_bottom()
        loaded = [i for i, x in enumerate(self.browser.route.get("_items") or [])
                  if x is not None]
        self.assertTrue(loaded, "nothing loaded at all")
        self.assertGreater(
            max(loaded), total * 0.75,
            "scrolling stopped %d items into %d and would not go further — "
            "the reported symptom is that it stops about two thirds through "
            "while visibly not at the end" % (max(loaded), total))


    def _bar(self):
        """The grid's scrollbar thumb geometry, as the renderer drew it."""
        bars = self._state().get("bars") or {}
        bar = bars.get(GRID)
        self.assertTrue(
            bar and bar.get("h"),
            "the grid drew no scrollbar, so there is no thumb to drag "
            "(bars=%r)" % (list(bars),))
        return bar

    def _thumb_fraction(self):
        """How far down the track the thumb sits, 0..1.

        Read from the thumb rather than from the offset because it is what
        the user aimed at, and because it needs no second opinion about the
        scroll range.
        """
        bar = self._bar()
        travel = bar["h"] - bar["thumb_h"]
        if travel <= 0:
            return 0.0
        return (bar["thumb_y"] - bar["y"]) / travel

    def test_dragging_the_thumb_to_the_middle_loads_that_neighbourhood(self):
        """`docs/E2E_PLAN.md` item 19, and the last of the mouse gestures
        with no test: `state.drag` -- the scrollbar branch -- is exercised
        only against a dropdown's popup in the toolkit suite, never against
        a page of a thousand real items.

        The assertion is deliberately about the ITEMS, not about the offset.
        A scroll offset agreeing with a thumb position only says the
        renderer is self-consistent; what #617 is about is whether the
        window that gets FETCHED follows the thumb, or whether the grid
        walks from the top and leaves the middle empty. So this drags to the
        middle and asks the browser what it loaded there.
        """
        self._settle("nothing composited at the top to begin with")
        self.assertLess(self._thumb_fraction(), 0.05,
                        "the thumb did not start at the top, so a drag to "
                        "the middle would not be a drag to the middle")

        bar = self._bar()
        x = int(bar["x"] + bar["w"] / 2)
        grab = int(bar["thumb_y"] + bar["thumb_h"] / 2)
        target = int(bar["y"] + bar["h"] / 2)

        self.handle.command("mouse", x, grab)
        time.sleep(0.3)
        self.handle.command("keydown", "MBTN_LEFT")
        # In steps, as a hand moves: one jump is a track click, and a track
        # click is a different branch of the renderer from a drag.
        for frac in (0.25, 0.5, 0.75, 1.0):
            self.handle.command("mouse", x,
                                int(grab + (target - grab) * frac))
            time.sleep(0.12)
        self.handle.command("keyup", "MBTN_LEFT")

        landed = self._thumb_fraction()
        self.assertGreater(landed, 0.25,
                           "dragging the thumb to the middle of the track "
                           "left it at %.3f -- the drag never moved the "
                           "grid" % landed)
        self.assertLess(landed, 0.75,
                        "the thumb ended at %.3f, which is not the middle: "
                        "the drag was read as a jump to the end" % landed)
        self._settle("nothing composited after dragging to the middle")

        items = self.browser.route.get("_items") or []
        self.assertGreater(len(items), 500,
                           "only %d items, so this is not the bulk library "
                           "this test is about" % len(items))
        idx = int(landed * len(items))
        window = items[max(0, idx - 6):idx + 18]
        loaded = sum(1 for i in window if i is not None)
        self.assertGreaterEqual(
            loaded, max(1, len(window) // 2),
            "the thumb is %.0f%% down %d items, so the screen is showing "
            "around index %d -- and only %d of the %d slots there are "
            "loaded. The grid followed the thumb on screen and fetched "
            "somewhere else."
            % (landed * 100, len(items), idx, loaded, len(window)))


if __name__ == "__main__":
    unittest.main()
