"""Carousel page-button arithmetic — jellyfin-web's ``scrollerItemSlideIntoView``.

A page advances by whole tiles, and a tile left half-cut at the trailing edge
leads the next page rather than being skipped. The old behaviour was "scroll by
90% of the viewport", done in Lua, which landed mid-poster and could not answer
"is there anywhere left to go" — which is what the disabled state needs.

The maths lives in Python precisely so it can be tested here; the renderer only
gets told an absolute offset.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import unittest

from jellyfin_mpv_shim.mpvtk_browser.scroll_state import ScrollState
from jellyfin_mpv_shim.mpvtk_browser.strips import TileGeom
from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import (
    ROW_LEAD, page_geometry, page_target)

# tile_w 200, gap 20 -> a 220px pitch, so the numbers below stay readable.
GEOM = TileGeom(tile_w=200, tile_h=300, caption_h=40, gap=20)
PITCH = 220


def view_for(n):
    """Viewport width that fits exactly ``n`` tiles, margins included.

    A strip is full width and pads itself to ``ROW_LEAD`` on both sides, so
    the margins are part of the scrollable content and belong in the sum.
    """
    return 2 * ROW_LEAD + n * PITCH - GEOM.gap


class PageGeometryTest(unittest.TestCase):
    def test_per_page_is_the_number_of_fully_visible_tiles(self):
        for n in (1, 2, 5, 9):
            with self.subTest(n=n):
                _pitch, per, _max = page_geometry(view_for(n), 50, GEOM)
                self.assertEqual(per, n)

    def test_a_partial_tile_does_not_count_towards_a_page(self):
        """Half a tile more viewport is still five tiles per page — paging by
        the partial one is what makes rows drift out of alignment."""
        _pitch, per, _max = page_geometry(view_for(5) + PITCH // 2, 50, GEOM)
        self.assertEqual(per, 5)

    def test_per_page_never_drops_below_one(self):
        """A viewport narrower than a single tile still has to advance, or the
        button is live and does nothing."""
        _pitch, per, _max = page_geometry(50, 50, GEOM)
        self.assertGreaterEqual(per, 1)

    def test_max_offset_puts_the_last_tile_against_the_trailing_edge(self):
        view = view_for(5)
        _pitch, _per, max_offset = page_geometry(view, 10, GEOM)
        # Content is 10 tiles wide plus the margin at both ends.
        total = 2 * ROW_LEAD + 10 * PITCH - GEOM.gap
        self.assertAlmostEqual(max_offset, total - view)

    def test_a_row_that_fits_has_no_room_to_scroll(self):
        _pitch, _per, max_offset = page_geometry(view_for(5), 5, GEOM)
        self.assertEqual(max_offset, 0)


class PageTargetTest(unittest.TestCase):
    def setUp(self):
        self.view = view_for(5)          # five tiles visible
        self.count = 20
        _p, _per, self.max_offset = page_geometry(self.view, self.count, GEOM)

    def target(self, offset, direction):
        return page_target(offset, direction, self.view, self.count, GEOM)

    def test_forward_from_the_start_advances_a_full_page(self):
        self.assertAlmostEqual(self.target(0, 1), 5 * PITCH)

    def test_back_from_the_start_is_an_end_stop(self):
        self.assertIsNone(self.target(0, -1))

    def test_forward_at_the_end_is_an_end_stop(self):
        self.assertIsNone(self.target(self.max_offset, 1))

    def test_the_end_stop_tolerates_a_rounding_round_trip(self):
        """The offset comes back from the renderer through a physical/logical
        conversion, so it is never exactly ``max_offset``."""
        self.assertIsNone(self.target(self.max_offset - 0.4, 1))

    def test_the_last_page_is_short_rather_than_overshooting(self):
        """A full page from near the end would run past the content; it has to
        clamp, leaving the final tiles flush against the trailing edge."""
        near_end = self.max_offset - 100
        got = self.target(near_end, 1)
        self.assertGreater(got, near_end)
        self.assertAlmostEqual(got, self.max_offset)

    def test_a_half_cut_tile_leads_the_next_page(self):
        """The defining jellyfin-web behaviour. With a viewport half a tile
        wider than five tiles, tile 5 is partly visible — paging forward must
        bring *it* to the front, not skip to tile 6."""
        view = view_for(5) + PITCH // 2
        self.assertAlmostEqual(page_target(0, 1, view, self.count, GEOM),
                               5 * PITCH)

    def test_a_page_lands_on_a_tile_boundary_from_an_unaligned_offset(self):
        """Wheel scrolling leaves the row mid-tile; a page click re-aligns it
        rather than preserving the misalignment forever."""
        for stray in (1, PITCH // 3, PITCH - 1):
            with self.subTest(stray=stray):
                got = self.target(2 * PITCH + stray, 1)
                self.assertAlmostEqual(got % PITCH, 0)

    def test_forward_then_back_returns_to_where_it_started(self):
        first = self.target(0, 1)
        self.assertAlmostEqual(self.target(first, -1), 0)

    def test_back_from_the_middle_moves_a_full_page(self):
        self.assertAlmostEqual(self.target(9 * PITCH, -1), 4 * PITCH)

    def test_back_clamps_at_the_start(self):
        self.assertAlmostEqual(self.target(2 * PITCH, -1), 0)


class EndStopRepaintTest(unittest.TestCase):
    """The buttons' disabled state is offset-derived, so reaching an end has
    to invalidate even though it is a tiny move — see ScrollState.on_scroll."""

    def setUp(self):
        self.hits = []
        self.st = ScrollState(lambda: self.hits.append(1))

    def test_leaving_an_end_stop_repaints(self):
        self.st.on_scroll("row", 0, 1000, edges_only=True)   # first, always
        self.hits.clear()
        self.st.on_scroll("row", 5, 1000, edges_only=True)
        self.assertEqual(len(self.hits), 1)

    def test_arriving_at_an_end_stop_repaints_however_short_the_move(self):
        self.st.on_scroll("row", 500, 1000, edges_only=True)
        self.hits.clear()
        self.st.on_scroll("row", 1000, 1000, edges_only=True)
        self.assertEqual(len(self.hits), 1)

    def test_moving_between_the_ends_does_not_repaint(self):
        """A carousel virtualizes nothing, so a mid-row repaint would
        recomposite a screenful of poster strips to change nothing."""
        self.st.on_scroll("row", 100, 5000, edges_only=True)
        self.hits.clear()
        for offset in (400, 900, 1500, 2600):
            self.st.on_scroll("row", offset, 5000, edges_only=True)
        self.assertEqual(self.hits, [])

    def test_paging_from_one_end_straight_to_the_other_repaints(self):
        """The regression the tri-state edge exists for. A row one page longer
        than its viewport goes start-stop -> end-stop in a single click, which
        reverses BOTH buttons -- and a boolean "is against an end" reads the
        same on both sides, so the one move that changed everything was the one
        move that repainted nothing. The row sat at its end with Next lit."""
        self.st.on_scroll("row", 0, 300, edges_only=True)   # first, always
        self.hits.clear()
        self.st.on_scroll("row", 300, 300, edges_only=True)
        self.assertEqual(len(self.hits), 1)

    def test_and_back_again(self):
        self.st.on_scroll("row", 300, 300, edges_only=True)
        self.hits.clear()
        self.st.on_scroll("row", 0, 300, edges_only=True)
        self.assertEqual(len(self.hits), 1)

    def test_a_move_within_the_same_end_stop_still_does_not_repaint(self):
        """The slack is there to absorb the physical/logical rounding the
        offset makes on its way back from the renderer, not to invite a
        repaint per frame of a drag against the stop. Both ends: the far one
        is where the rounding actually bites, because ``maximum`` is itself a
        computed fractional."""
        for maximum, base, then in ((5000, 0, 0.4), (5000, 5000, 4999.6)):
            with self.subTest(base=base):
                self.st = ScrollState(lambda: self.hits.append(1))
                self.st.on_scroll("row", base, maximum, edges_only=True)
                self.hits.clear()
                self.st.on_scroll("row", then, maximum, edges_only=True)
                self.assertEqual(self.hits, [])

    def test_the_distance_rule_still_applies_without_edges_only(self):
        """Virtualized containers keep the old behaviour: a window's worth of
        movement rebuilds, wherever it happens."""
        self.st.on_scroll("grid", 100, 5000)
        self.hits.clear()
        self.st.on_scroll("grid", 100 + ScrollState.STEP, 5000)
        self.assertEqual(len(self.hits), 1)


class OffsetPrecedenceTest(unittest.TestCase):
    """Who answers "where is this container", and in what order.

    Three sources, and the order between them is the whole of two separate
    blank-screen bugs — see ``ScrollState.offset``.
    """

    class _App:
        def __init__(self, offsets):
            self.offsets = offsets

        def scroll_offsets(self):
            return self.offsets

    def _state(self, live=None, route=None):
        st = ScrollState(lambda: None)
        st.refresh(self._App(live) if live is not None else None, route)
        return st

    def _parked(self, **offsets):
        return {ScrollState.PARK_KEY: dict(offsets)}

    def test_the_renderer_is_the_authority(self):
        st = self._state(live={"grid": 900}, route=self._parked(grid=1500))
        self.assertEqual(st.offset("grid"), 900)

    def test_a_live_zero_still_outranks_a_parked_offset(self):
        """The container is on screen and at the top because the user put it
        there. Preferring the parked value here is the Paginated-toggle bug
        wearing a new hat: a window built around an offset nothing has."""
        st = self._state(live={"grid": 0}, route=self._parked(grid=1500))
        self.assertEqual(st.offset("grid"), 0)

    def test_a_container_the_renderer_has_not_met_takes_the_parked_offset(self):
        """It has just entered the scene, and the scene carries off0 to put
        it back — so that is where it is about to be."""
        st = self._state(live={"detail": 40}, route=self._parked(grid=1500))
        self.assertEqual(st.offset("grid"), 1500)

    def test_a_container_with_nothing_parked_is_at_the_top(self):
        st = self._state(live={}, route={})
        self.assertEqual(st.offset("grid"), 0)

    def test_pending_is_what_a_component_restores_a_container_with(self):
        """``offset`` answers for an unmet container with the parked value
        *because the scene is about to command it*. ``pending`` is the other
        half of that bargain -- the value a shared component passes as off0 so
        the claim is true."""
        st = self._state(live={}, route=self._parked(**{"row-latest-0": 640}))
        self.assertEqual(st.pending("row-latest-0"), 640)
        self.assertEqual(st.offset("row-latest-0"), 640)

    def test_nothing_parked_restores_nothing(self):
        st = self._state(live={}, route={})
        self.assertIsNone(st.pending("row-latest-0"))

    def test_a_parked_zero_is_not_worth_restoring(self):
        """off0 for 0 is what a fresh container does anyway, and passing it
        would keep a scene node alive for every row that was never scrolled."""
        st = self._state(live={}, route=self._parked(**{"row-latest-0": 0}))
        self.assertIsNone(st.pending("row-latest-0"))

    def test_a_confirmed_container_is_no_longer_offered_a_restore(self):
        """A restore happens once. Once the renderer has answered for an id,
        that container's position is its own — see ``pending``."""
        st = self._state(live={"grid": 300}, route=self._parked(grid=1500))
        self.assertIsNone(st.pending("grid"))

    def test_the_next_screen_may_restore_the_same_id_again(self):
        """Container ids are per-VIEW, not per route, so the same id turns up
        on the next screen with a different offset parked for it. What the
        renderer confirmed about the last screen says nothing about this one,
        which is why ``reset()`` clears it."""
        st = self._state(live={"grid": 300}, route=self._parked(grid=1500))
        self.assertIsNone(st.pending("grid"))
        st.reset()                                   # a route change
        st.refresh(None, self._parked(grid=1500))
        self.assertEqual(st.pending("grid"), 1500)

    def test_parking_does_not_disturb_a_frame_in_progress(self):
        """``park`` runs on whatever thread called the navigation — the
        websocket thread delivering a DisplayContent, a remote sending GoHome
        — while ``build()`` is mid-frame on the loop thread. A torn ``_live``
        read is the one-frame glitch the browser tolerates by design; a
        ``_pending`` emptied mid-frame is not, because ``off0`` is applied to
        a container exactly once and the renderer has already seeded it at 0
        by the time the next frame could correct it."""
        st = self._state(live={"detail": 10}, route=self._parked(grid=1500))
        self.assertEqual(st.pending("grid"), 1500)
        st.park({}, self._App({"detail": 10}))       # the other thread
        self.assertEqual(st.pending("grid"), 1500,
                         "a park cleared the frame's parked offsets")

    def test_the_recorded_copy_is_still_a_whole_snapshot_fallback(self):
        """mpv < 0.36 has no live snapshot at all. It does NOT get consulted
        per-id to fill gaps in one that answered."""
        st = self._state(live={"other": 5}, route={})
        st._recorded["grid"] = 1500
        self.assertEqual(st.offset("grid"), 0)
        st.refresh(None, {})                  # no live snapshot at all
        self.assertEqual(st.offset("grid"), 1500)


if __name__ == "__main__":
    unittest.main()
