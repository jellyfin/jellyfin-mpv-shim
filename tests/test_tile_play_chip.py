"""The play button that appears on a hovered tile.

jellyfin-web's ``overlayPlayButton``: the tile itself opens the page, and a
chip in the middle of the artwork starts playback without going through it.

Three things make it work in an mpv window rather than a browser, and each is
a way it could silently stop working:

* it is a BITMAP, because mpv composites overlay bitmaps above all script ASS
  -- anything drawn as a node would be *under* the poster strip it sits on;
* the renderer has to tell Python which tile is hovered, which nothing else
  in the library needs, so only tiles that would do something with it opt in;
* the chip sits inside the tile, so the pointer moving onto the chip is the
  pointer leaving the tile. Both ids resolve back to the same tile, or the
  chip takes itself away the instant it is touched.
"""

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool, build_scene, ids)


class ChipTest(unittest.TestCase):
    def _browser(self, items=None):
        src = FakeSource()
        src.grid_items = items or [
            {"Id": "g0", "Name": "Film", "Type": "Movie",
             "PrimaryImageAspectRatio": 2 / 3},
            {"Id": "g1", "Name": "Film 2", "Type": "Movie",
             "PrimaryImageAspectRatio": 2 / 3},
        ]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        return b, src

    def _tile_id(self, b, suffix="g0"):
        for nid in ids(build_scene(b)[0]):
            if nid.startswith("grid-") and nid.endswith(suffix):
                return nid
        self.fail("no tile node for %s" % suffix)

    def _hover(self, b, node_id):
        _nodes, handlers = build_scene(b)
        handlers[node_id]["hover"]("")
        return build_scene(b)

    # -- appearing ---------------------------------------------------------

    def test_a_tile_reports_its_hover(self):
        """Nothing else in the library asks the renderer for this, so the
        opt-in flag on the hit region is the whole feature's foundation."""
        b, _src = self._browser()
        nodes, _h = build_scene(b)
        tile = [n for n in nodes if n.get("id") == self._tile_id(b)][0]
        self.assertTrue(tile.get("hev"))

    def test_hovering_draws_a_chip_over_the_tile(self):
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, _h = self._hover(b, tid)
        chip = [n for n in nodes if n.get("id") == tid + "-play"]
        self.assertEqual(len(chip), 1, "no chip on the hovered tile")
        self.assertEqual(chip[0]["t"], "img",
                         "an ASS node would draw UNDER the strip")

    def test_the_chip_sits_on_the_artwork(self):
        """Centred on the picture, not on the tile: the hit region includes
        the caption block, and a chip centred on that sits visibly low."""
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, _h = self._hover(b, tid)
        tile = [n for n in nodes if n.get("id") == tid][0]
        chip = [n for n in nodes if n.get("id") == tid + "-play"][0]
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        art_mid = tile["y"] + POSTER_GEOM.tile_h / 2
        self.assertAlmostEqual(chip["y"] + chip["h"] / 2, art_mid, delta=2)
        self.assertAlmostEqual(chip["x"] + chip["w"] / 2,
                               tile["x"] + tile["w"] / 2, delta=2)

    def test_only_the_hovered_tile_has_one(self):
        b, _src = self._browser()
        nodes, _h = self._hover(b, self._tile_id(b, "g0"))
        chips = [n for n in nodes if str(n.get("id", "")).endswith("-play")]
        self.assertEqual(len(chips), 1)

    def test_leaving_takes_it_away(self):
        b, _src = self._browser()
        tid = self._tile_id(b)
        _nodes, handlers = self._hover(b, tid)
        handlers[tid]["hover_end"]()
        self.assertNotIn(tid + "-play", ids(build_scene(b)[0]))

    def test_the_chip_holds_itself_open(self):
        """The pointer moving onto the chip leaves the tile. If that were
        taken at face value the chip would vanish on contact and the click
        would land on the tile underneath -- opening the page, which is
        exactly what the chip exists to skip."""
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, handlers = self._hover(b, tid)
        handlers[tid + "-play"]["hover"]("")
        handlers[tid]["hover_end"]()
        self.assertIn(tid + "-play", ids(build_scene(b)[0]))

    def test_the_chip_lights_up_under_the_pointer(self):
        """Every other control in the app answers the pointer; a bitmap has
        no style for the renderer to change, so the lit state is a second
        bitmap and the app has to notice the difference itself."""
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, handlers = self._hover(b, tid)
        cold = [n for n in nodes if n.get("id") == tid + "-play"][0]
        handlers[tid + "-play"]["hover"]("")
        nodes, _h = build_scene(b)
        hot = [n for n in nodes if n.get("id") == tid + "-play"][0]
        self.assertGreater(hot["w"], cold["w"], "the chip did not grow")
        self.assertNotEqual(hot["src"], cold["src"],
                            "the same bitmap means the same colour")

    def test_growing_keeps_the_pointer_inside(self):
        """It grows around its own centre. Growing downwards would slide out
        from under the pointer, un-hover, shrink, and oscillate."""
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, handlers = self._hover(b, tid)
        cold = [n for n in nodes if n.get("id") == tid + "-play"][0]
        handlers[tid + "-play"]["hover"]("")
        nodes, _h = build_scene(b)
        hot = [n for n in nodes if n.get("id") == tid + "-play"][0]
        for axis, span in (("x", "w"), ("y", "h")):
            self.assertAlmostEqual(hot[axis] + hot[span] / 2,
                                   cold[axis] + cold[span] / 2, delta=1)

    def test_crossing_to_the_next_tile_keeps_up(self):
        """Enter and leave cross: sweeping from one tile to the next
        delivers the new tile's enter before the old one's leave."""
        b, _src = self._browser()
        first, second = self._tile_id(b, "g0"), self._tile_id(b, "g1")
        _n, handlers = build_scene(b)
        handlers[first]["hover"]("")
        _n, handlers = build_scene(b)
        handlers[second]["hover"]("")
        _n, handlers = build_scene(b)
        handlers[first]["hover_end"]()
        got = ids(build_scene(b)[0])
        self.assertIn(second + "-play", got)
        self.assertNotIn(first + "-play", got)

    # -- what it costs to show one -----------------------------------------

    def _strip_id(self, nodes, y):
        """The id of the row bitmap the tile at ``y`` is drawn in."""
        for n in nodes:
            if (n["t"] == "img" and not str(n["id"]).endswith("-play")
                    and n["y"] <= y < n["y"] + n["h"]):
                return n["id"]
        self.fail("no strip node covering y=%s" % y)

    def test_the_chip_does_not_rename_the_row_it_lands_on(self):
        """The renderer keys its overlay slots by node id, and a node with
        no id of its own is identified by its PATH in the tree -- so the
        Stack that floats the chip renamed the row underneath it.

        To the renderer that is one bitmap leaving and an unrelated one
        arriving, so the whole row is re-issued into a different slot on
        every hover in and out. Driven through the real renderer with the
        ids the app actually sends, 334 of 400 pointer moves between rows
        re-issued a whole strip -- both rows, the one left and the one
        entered -- against 0 with the id held still. A strip is up to 31 MiB
        at 4K, and the change on screen is a chip the size of a coin.

        The Lua suite could not catch this: its ``strip_page`` fake gives
        each row a fixed id, which is the thing that was not true.
        """
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, _h = build_scene(b)
        tile = [n for n in nodes if n.get("id") == tid][0]
        before = self._strip_id(nodes, tile["y"])
        hovered, _h = self._hover(b, tid)
        self.assertEqual(self._strip_id(hovered, tile["y"]), before,
                         "hovering renamed the row's bitmap")

    def test_navigating_away_clears_it(self):
        """A click navigates without moving the pointer, so the renderer has
        no reason to report a leave."""
        b, _src = self._browser()
        self._hover(b, self._tile_id(b))
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib2",
                    "collection_type": "movies", "title": "Other"})
        self.assertEqual(
            [n for n in ids(build_scene(b)[0])
             if str(n).endswith("-play")], [])

    # -- who gets one ------------------------------------------------------

    def _playable(self, item):
        b, _src = self._browser()
        return b._tile_playable(item)

    def test_the_types_that_get_a_chip(self):
        for item in ({"Type": "Movie"}, {"Type": "Episode"},
                     {"Type": "Audio"}, {"Type": "MusicAlbum"},
                     {"Type": "Series"}, {"Type": "Playlist"},
                     {"Type": "TvChannel"}, {"Type": "BoxSet"},
                     {"Type": "Folder"}, {"Type": "PhotoAlbum"}):
            with self.subTest(item["Type"]):
                self.assertTrue(self._playable(item))

    def test_the_types_that_do_not(self):
        for item in ({"Type": "Person"}, {"Type": "Genre"},
                     {"Type": "Studio"}, {"Type": "Photo"}):
            with self.subTest(item["Type"]):
                self.assertFalse(self._playable(item))

    def test_a_library_never_gets_one(self):
        """It can answer Play All -- the grid header offers exactly that
        once you are inside -- and it is still the wrong thing to put under
        the pointer. A library tile is a door, the gesture on it is "take me
        in", and a play button there is a whole-library queue one slip away
        from what someone was actually doing."""
        for ctype in ("movies", "tvshows", "music", "homevideos", "livetv",
                      "boxsets"):
            for t in ("CollectionFolder", "UserView", "Folder"):
                with self.subTest("%s/%s" % (t, ctype)):
                    self.assertFalse(self._playable(
                        {"Type": t, "CollectionType": ctype}))

    def test_a_tile_with_no_chip_does_not_report_hovers(self):
        """A row of cast members should not cost a scene rebuild per face
        the pointer crosses."""
        b, _src = self._browser([{"Id": "p1", "Name": "Actor",
                                  "Type": "Person"}])
        nodes, _h = build_scene(b)
        tiles = [n for n in nodes
                 if str(n.get("id", "")).startswith("grid-0-p1")]
        self.assertTrue(tiles)
        self.assertFalse(any(n.get("hev") for n in tiles))

    # -- what it does ------------------------------------------------------

    def test_clicking_it_plays_without_opening_the_page(self):
        b, _src = self._browser()
        tid = self._tile_id(b)
        _nodes, handlers = self._hover(b, tid)
        handlers[tid + "-play"]["click"]()
        self.assertEqual([p[0] for p in b.controller.played], ["g0"])
        self.assertEqual(b.route.get("parent_id"), "lib1",
                         "the chip navigated instead of playing")

    def test_right_clicking_it_opens_the_tile_menu(self):
        """A hit test answers with ONE node, and a node with no context menu
        is a no-op rather than a fall-through to whatever is underneath. The
        chip covers the middle of the artwork, which is where a pointer that
        has settled on a tile is -- so without its own copy of the menu,
        right-clicking a poster did nothing most of the time."""
        b, _src = self._browser()
        tid = self._tile_id(b)
        nodes, handlers = self._hover(b, tid)
        chip = [n for n in nodes if n.get("id") == tid + "-play"][0]
        self.assertTrue(chip.get("ctx"),
                        "the renderer will not send a right-click here")
        handlers[tid + "-play"]["context"](120, 90)
        self.assertIsNotNone(b._menu, "no menu opened")
        self.assertEqual(b._menu["item"]["Id"], "g0")
        self.assertEqual((b._menu["x"], b._menu["y"]), (120, 90))

    def test_a_series_plays_next_up(self):
        """Not the whole show from episode one, which throws away where you
        had got to. What reaches the player is the series queue STARTING at
        Next Up -- ItemActions.play chains the rest of the show behind an
        episode so autoplay-next works, which is why the assertion is about
        where the queue was cut rather than about a single id."""
        b, src = self._browser([{"Id": "sh1", "Name": "Show",
                                 "Type": "Series"}])
        seen = {}
        real = src.get_series_queue

        def spy(server, series_id, start_item_id=None, limit=100):
            seen["start"] = start_item_id
            return real(server, series_id, start_item_id, limit)

        src.get_series_queue = spy
        tid = self._tile_id(b, "sh1")
        _nodes, handlers = self._hover(b, tid)
        handlers[tid + "-play"]["click"]()
        # FakeSource.get_next_up answers "nu1" for any series.
        self.assertEqual(seen.get("start"), "nu1")
        self.assertTrue(b.controller.played, "nothing was played")

    def test_a_collection_plays_its_contents_in_grid_order(self):
        b, _src = self._browser([{"Id": "bs1", "Name": "Trilogy",
                                  "Type": "BoxSet"}])
        tid = self._tile_id(b, "bs1")
        _nodes, handlers = self._hover(b, tid)
        handlers[tid + "-play"]["click"]()
        # FakeSource.get_play_all_ids echoes a fixed queue.
        self.assertEqual([p[0] for p in b.controller.played],
                         [["g0", "g1", "g2"]])


class ChipRasterCostTest(unittest.TestCase):
    """The chip is re-derived on every build of the strip it sits on, and
    there are only ever two of them per size (lit and not). Rasterizing to
    hand the answer to a cache that already has it is the whole cost of a
    hover repaint -- and it is a 3x supersampled disc, on the loop thread."""

    def _hovered(self):
        src = FakeSource()
        src.grid_items = [{"Id": "g0", "Name": "Film", "Type": "Movie",
                           "PrimaryImageAspectRatio": 2 / 3}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        tid = [n for n in ids(build_scene(b)[0])
               if n.startswith("grid-") and n.endswith("g0")][0]
        _nodes, handlers = build_scene(b)
        handlers[tid]["hover"]("")
        return b, tid

    def test_repainting_a_hovered_tile_does_not_re_raster_the_chip(self):
        from jellyfin_mpv_shim.mpvtk_browser import tile_renderer

        b, tid = self._hovered()
        build_scene(b)                      # first build: one miss, cached
        calls = []
        real = tile_renderer._play_chip_bitmap

        def counted(size, hot=False):
            calls.append((size, hot))
            return real(size, hot=hot)

        tile_renderer._play_chip_bitmap = counted
        try:
            for _ in range(5):
                build_scene(b)
        finally:
            tile_renderer._play_chip_bitmap = real
        self.assertEqual(calls, [], "the chip was rasterized %d times for a "
                                    "bitmap the store already had"
                                    % len(calls))
        self.assertIn(tid + "-play", ids(build_scene(b)[0]),
                      "and it stopped being drawn")

    def test_a_state_it_has_not_seen_is_still_rastered(self):
        """Lazy, not skipped: the lit chip is a different bitmap under its
        own key and has to be made the first time the pointer reaches it."""
        from jellyfin_mpv_shim.mpvtk_browser import tile_renderer

        b, tid = self._hovered()
        build_scene(b)
        calls = []
        real = tile_renderer._play_chip_bitmap

        def counted(size, hot=False):
            calls.append((size, hot))
            return real(size, hot=hot)

        tile_renderer._play_chip_bitmap = counted
        try:
            _n, handlers = build_scene(b)
            handlers[tid + "-play"]["hover"]("")
            build_scene(b)
        finally:
            tile_renderer._play_chip_bitmap = real
        self.assertEqual([hot for _s, hot in calls], [True])


if __name__ == "__main__":
    unittest.main()
