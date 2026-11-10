"""The Scenes row follows the SELECTED version of a movie.

Each version is an item of its own on the server, with its own chapter list,
chapter images and start times; the page's item carries only the primary's.
The row used to be built from the primary whatever the version picker said --
so another cut's chapters, and a click that sought the primary's times in the
version that was actually playing.

Covered here: the primary path unchanged; an alternate's chapters fetched
lazily and cached by item id for the visit; hidden (never the primary's) while
unknown, failed, empty or offline; late and stale results; a reload clearing
the cache without being repopulated by a request from before it; and what the
tiles, images, shape and click are keyed on. The disposable-Jellyfin facts the
design rests on are in tools/jf_disposable/README.md's chapter notes.
"""

# Run as a script, this is what puts the repo root on sys.path.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import logging
import re
import sys
import unittest
from unittest import mock

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
from jellyfin_mpv_shim.mpvtk_browser.pages.detail import _clean_chapters
from tests._shell_harness import (
    FakeController, FakeSource, _DeferredPool, _SyncPool, build_scene,
    detail_page, ids,
)

TICKS = 10 ** 7


def chapters(prefix, starts, tagged=True):
    return [{"Name": "%s%d" % (prefix, i), "StartPositionTicks": s * TICKS,
             **({"ImageTag": "%s-tag%d" % (prefix, i)} if tagged else {})}
            for i, s in enumerate(starts)]


def stream(w=1920, h=1080):
    return [{"Type": "Video", "Width": w, "Height": h},
            {"Type": "Audio", "Index": 1}]


class VersionSource(FakeSource):
    """FakeSource plus what the Scenes row asks of a real source."""

    def __init__(self):
        super().__init__()
        #: item id -> list, an Exception to raise, or None (offline: unknown)
        self.chapters = {}
        self.chapter_calls = []
        self.image_asks = []

    def get_chapters(self, server_uuid, item_id):
        self.chapter_calls.append(item_id)
        answer = self.chapters.get(item_id, [])
        if isinstance(answer, Exception):
            raise answer
        return answer

    def chapter_image_url(self, server_uuid, item_id, chapter_index, chapter,
                          width=320):
        self.image_asks.append((item_id, chapter_index, width))
        tag = (chapter or {}).get("ImageTag")
        return ("http://img/%s/%d/%s" % (item_id, chapter_index, tag)
                if tag else None)


def movie(primary_chapters, sources):
    return {"Id": "m1", "Name": "Film", "Type": "Movie", "ServerId": "SRVID",
            "Chapters": primary_chapters, "MediaSources": sources}


def two_versions(primary_streams=None, alt_streams=None):
    return [{"Id": "m1", "Name": "Theatrical",
             "MediaStreams": primary_streams or stream()},
            {"Id": "v2", "Name": "Directors Cut",
             "MediaStreams": alt_streams or stream()}]


class Rig(unittest.TestCase):
    pool = _SyncPool

    def setUp(self):
        self.src = VersionSource()
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.pool_obj = self.pool()
        self.b._pool = self.pool_obj
        # A failing fetch is logged as a warning with a traceback; the tests
        # that provoke one expect it, and ERROR would still show a real fault.
        runner_log = logging.getLogger("mpvtk_browser.async_runner")
        old_level = runner_log.level
        runner_log.setLevel(logging.ERROR)
        self.addCleanup(runner_log.setLevel, old_level)

    def open(self, item, deferred_land=True):
        self.src.items["m1"] = item
        self.b.navigate({"kind": "detail", "server": "srv1",
                         "item_id": "m1", "title": "Film"})
        if isinstance(self.pool_obj, _DeferredPool) and deferred_land:
            self.pool_obj.drain()          # the page's own data lands
        return build_scene(self.b)

    @property
    def page(self):
        return detail_page(self.b, self.b.route)

    def build(self):
        return build_scene(self.b)

    def pick(self, index):
        _n, h = self.build()
        h["dt-version"]["select"](index, "v%d" % index)

    @staticmethod
    def tile_ids(nodes, owner="m1"):
        """Scene tile node ids for the version `owner` (row id, then the
        tile's own ``<owner>#ch<i>``), in order."""
        pat = re.compile(r"^detail-scenes(-[^-#]+)?-%s#ch(\d+)$"
                         % re.escape(owner))
        hits = [(int(m.group(2)), i) for i in ids(nodes)
                if isinstance(i, str) for m in [pat.match(i)] if m]
        return [i for _n, i in sorted(hits)]

    def scene_tiles(self):
        """The tile dicts and row id the page hands the tile row."""
        seen = {}

        def capture(title, items, row_id, **kw):
            seen["items"], seen["row"], seen["kw"] = items, row_id, kw
            return None
        with mock.patch.object(self.page.ctx.art.tiles, "tile_row",
                               side_effect=capture):
            self.page._scenes_row(self.b.route["_data"]["item"], "srv1")
        return seen


class PrimaryUnchanged(Rig):
    def setUp(self):
        super().setUp()
        self.prim = chapters("T", [0, 6, 12, 18, 24])
        self.item = movie(self.prim, two_versions())

    def test_the_primary_shows_its_own_chapters_and_asks_for_nothing(self):
        nodes, _h = self.open(self.item)
        self.assertEqual(self.tile_ids(nodes),
                         ["detail-scenes-m1#ch%d" % i for i in range(5)])
        self.assertIn("detail-scenes", ids(nodes))
        self.assertEqual(self.src.chapter_calls, [])
        self.assertEqual([a[0] for a in self.src.image_asks], ["m1"] * 5)
        self.assertEqual([a[1] for a in self.src.image_asks], list(range(5)))

    def test_a_single_version_item_is_the_old_path(self):
        item = movie(self.prim, [two_versions()[0]])
        nodes, _h = self.open(item)
        self.assertEqual(len(self.tile_ids(nodes)), 5)
        self.assertEqual(self.src.chapter_calls, [])

    def test_a_lone_source_is_the_old_path_whatever_its_id(self):
        """Nothing to choose between, so nothing to follow: the item's own
        chapters, even where a source id differs from the item's (as a
        fixture or an odd server may have it)."""
        item = movie(self.prim, [{"Id": "src-other", "Name": "Only",
                                  "MediaStreams": stream()}])
        nodes, _h = self.open(item)
        self.assertEqual(len(self.tile_ids(nodes)), 5)
        self.assertEqual(self.src.chapter_calls, [])

    def test_an_item_with_no_media_sources_is_the_old_path(self):
        nodes, _h = self.open(movie(self.prim, []))
        self.assertEqual(len(self.tile_ids(nodes)), 5)
        self.assertEqual(self.src.chapter_calls, [])

    def test_a_primary_click_seeks_its_own_start_with_its_own_source(self):
        _n, h = self.open(self.item)
        h["detail-scenes-m1#ch2"]["click"]()
        self.assertEqual(self.ctl.played[-1], ("m1", "srv1", 12 * TICKS))
        self.assertEqual(self.ctl.tracks[-1]["srcid"], "m1")

    def test_the_primary_row_id_is_the_one_it_always_had(self):
        self.open(self.item)
        self.assertEqual(self.scene_tiles()["row"], "detail-scenes")

    def test_one_chapter_is_no_row_as_before(self):
        item = movie(chapters("T", [0]), two_versions())
        nodes, _h = self.open(item)
        self.assertEqual(self.tile_ids(nodes), [])


class AlternateVersion(Rig):
    def setUp(self):
        super().setUp()
        self.prim = chapters("T", [0, 6, 12, 18, 24])
        self.alt = chapters("D", [0, 14, 20])
        self.src.chapters["v2"] = self.alt
        self.item = movie(self.prim, two_versions())

    def test_selecting_it_fetches_its_chapters_once_and_shows_them(self):
        self.open(self.item)
        self.pick(1)
        self.build()                      # the repaint the fetch asks for
        nodes, _h = self.build()
        self.assertEqual(self.src.chapter_calls, ["v2"])
        self.assertEqual(self.tile_ids(nodes, "v2"),
                         ["detail-scenes-v2-v2#ch%d" % i for i in range(3)])
        # and nothing of the primary's row is left on screen
        self.assertEqual(self.tile_ids(nodes), [])

    def test_tiles_identity_images_and_starts_belong_to_the_version(self):
        self.open(self.item)
        self.pick(1)
        self.build()
        self.src.image_asks.clear()
        seen = self.scene_tiles()
        self.assertEqual(seen["row"], "detail-scenes-v2")
        tiles = seen["items"]
        self.assertEqual([t["Id"] for t in tiles],
                         ["v2#ch0", "v2#ch1", "v2#ch2"])
        self.assertEqual([t["_start_ticks"] for t in tiles],
                         [0, 14 * TICKS, 20 * TICKS])
        self.assertEqual([t["Name"] for t in tiles], ["D0", "D1", "D2"])
        self.assertEqual([t["_image_url"] for t in tiles],
                         ["http://img/v2/%d/D-tag%d" % (i, i)
                          for i in range(3)])
        self.assertEqual([t["_image_spec"] for t in tiles],
                         [("v2", "Chapter%d" % i, "D-tag%d" % i)
                          for i in range(3)])
        self.assertEqual({a[0] for a in self.src.image_asks}, {"v2"})

    def test_a_click_seeks_the_versions_time_in_the_versions_source(self):
        self.open(self.item)
        self.pick(1)
        self.build()
        _n, h = self.build()
        h["detail-scenes-v2-v2#ch1"]["click"]()
        self.assertEqual(self.ctl.played[-1], ("m1", "srv1", 14 * TICKS))
        self.assertEqual(self.ctl.tracks[-1]["srcid"], "v2")
        # the same index on the primary would have been 6 s, not 14
        self.assertNotEqual(self.prim[1]["StartPositionTicks"], 14 * TICKS)

    def test_a_click_from_a_frame_older_than_the_pick_does_nothing(self):
        self.open(self.item)
        self.pick(1)
        self.build()
        _n, h = self.build()
        stale = h["detail-scenes-v2-v2#ch1"]["click"]
        self.pick(0)                      # back to the primary, no rebuild
        stale()
        self.assertEqual(self.ctl.played, [])

    def test_chapter_counts_may_differ_between_versions(self):
        self.src.chapters["v2"] = chapters("D", [0, 5])
        self.open(self.item)
        self.pick(1)
        self.build()
        nodes, _h = self.build()
        self.assertEqual(len(self.tile_ids(nodes, "v2")), 2)
        self.pick(0)
        nodes, _h = self.build()
        self.assertEqual(len(self.tile_ids(nodes)), 5)

    def test_switching_back_uses_the_cache(self):
        self.open(self.item)
        self.pick(1); self.build(); self.build()
        self.pick(0); self.build()
        self.pick(1)
        nodes, _h = self.build()
        self.assertEqual(self.src.chapter_calls, ["v2"])
        self.assertEqual(len(self.tile_ids(nodes, "v2")), 3)

    def test_each_alternate_is_fetched_by_its_own_id(self):
        sources = two_versions() + [{"Id": "v3", "Name": "Extended",
                                     "MediaStreams": stream()}]
        self.src.chapters["v3"] = chapters("E", [0, 3, 9, 27])
        self.open(movie(self.prim, sources))
        self.pick(1); self.build()
        self.pick(2); self.build()
        nodes, _h = self.build()
        self.assertEqual(self.src.chapter_calls, ["v2", "v3"])
        self.assertEqual(len(self.tile_ids(nodes, "v3")), 4)

    def test_a_primary_without_chapters_does_not_hide_an_alternates(self):
        nodes, _h = self.open(movie([], two_versions()))
        self.assertEqual(self.tile_ids(nodes), [])
        self.pick(1); self.build()
        nodes, _h = self.build()
        self.assertEqual(len(self.tile_ids(nodes, "v2")), 3)

    def test_the_row_scroll_does_not_carry_over_between_versions(self):
        self.open(self.item)
        self.b._scroll.on_scroll("detail-scenes", 300, 900)
        self.pick(1); self.build()
        nodes, _h = self.build()
        self.assertIn("detail-scenes-v2", ids(nodes))
        self.assertNotIn("detail-scenes", ids(nodes))
        self.assertIsNone(self.b._scroll.pending("detail-scenes-v2"))

    def test_the_tile_shape_follows_the_selected_versions_streams(self):
        item = movie(self.prim, two_versions(primary_streams=stream(1920, 1080),
                                             alt_streams=stream(1000, 1000)))
        self.open(item)

        def tile_size(row, owner):
            nodes, _h = self.build()
            hit = [n for n in nodes
                   if str(n.get("id", "")).startswith(row + "-%s#ch" % owner)
                   and n["t"] == "rect"]
            self.assertTrue(hit, "no tiles for " + row)
            return hit[0]["w"], hit[0]["h"]
        wide = tile_size("detail-scenes", "m1")
        self.pick(1); self.build()
        square = tile_size("detail-scenes-v2", "v2")
        self.assertNotEqual(wide, square)
        self.assertLess(square[0], wide[0])
        self.pick(0)
        self.assertEqual(tile_size("detail-scenes", "m1"), wide)


class Unknown(Rig):
    def setUp(self):
        super().setUp()
        self.item = movie(chapters("T", [0, 6, 12]), two_versions())

    def _alt_row_ids(self):
        nodes, _h = self.build()
        return [i for i in ids(nodes) if isinstance(i, str)
                and i.startswith("detail-scenes")]

    def test_no_chapters_at_all_hides_the_row_and_asks_once(self):
        self.src.chapters["v2"] = []
        self.open(self.item)
        self.pick(1)
        for _ in range(4):
            self.assertEqual(self._alt_row_ids(), [])
        self.assertEqual(self.src.chapter_calls, ["v2"])

    def test_a_single_chapter_hides_the_row(self):
        self.src.chapters["v2"] = chapters("D", [0])
        self.open(self.item)
        self.pick(1)
        self.assertEqual(self._alt_row_ids(), [])
        self.assertEqual(self._alt_row_ids(), [])

    def test_a_failed_request_hides_the_row_and_is_not_retried(self):
        self.src.chapters["v2"] = OSError("boom")
        self.open(self.item)
        self.pick(1)
        for _ in range(4):
            self.assertEqual(self._alt_row_ids(), [])
        self.assertEqual(self.src.chapter_calls, ["v2"])

    def test_an_offline_source_answering_none_hides_the_row(self):
        self.src.chapters["v2"] = None
        self.open(self.item)
        self.pick(1)
        for _ in range(3):
            self.assertEqual(self._alt_row_ids(), [])
        self.assertEqual(self.src.chapter_calls, ["v2"])

    def test_a_source_that_cannot_list_chapters_at_all_hides_the_row(self):
        self.open(self.item)
        with mock.patch.object(self.src, "get_chapters",
                               side_effect=AttributeError("no such method")):
            self.pick(1)
            for _ in range(3):
                self.assertEqual(self._alt_row_ids(), [])

    def test_an_unexpected_shape_is_no_chapters_not_a_crash(self):
        for bad in ("junk", 7, {"Chapters": []}, [1, "x", None, []]):
            with self.subTest(bad=bad):
                self.setUp()
                self.src.chapters["v2"] = bad
                self.open(self.item)
                self.pick(1)
                self.assertEqual(self._alt_row_ids(), [])
                self.assertEqual(self._alt_row_ids(), [])

    def test_malformed_entries_are_dropped_and_a_bad_start_is_zero(self):
        self.src.chapters["v2"] = [
            {"Name": "A", "StartPositionTicks": "soon", "ImageTag": "a"},
            None, "x",
            {"Name": 5, "StartPositionTicks": -3, "ImageTag": 9},
            {"Name": "C", "StartPositionTicks": 20 * TICKS}]
        self.open(self.item)
        self.pick(1); self.build()
        tiles = self.scene_tiles()["items"]
        self.assertEqual([t["_start_ticks"] for t in tiles], [0, 0, 20 * TICKS])
        self.assertEqual(tiles[1]["Name"], "Chapter 2")
        self.assertIsNone(tiles[1]["_image_url"])

    def test_chapters_without_images_keep_the_placeholder_tiles(self):
        self.src.chapters["v2"] = [
            {"Name": "A", "StartPositionTicks": 0, "ImageTag": "a"},
            {"Name": "B", "StartPositionTicks": 10 * TICKS},
            {"Name": "C", "StartPositionTicks": 20 * TICKS, "ImageTag": None}]
        self.open(self.item)
        self.pick(1); self.build()
        tiles = self.scene_tiles()["items"]
        self.assertEqual([t["Type"] for t in tiles], ["Chapter"] * 3)
        self.assertEqual([t["_image_url"] is None for t in tiles],
                         [False, True, True])
        self.assertEqual([t["_image_spec"] is None for t in tiles],
                         [False, True, True])

    def test_a_primary_with_no_images_is_unchanged_too(self):
        item = movie(chapters("T", [0, 6, 12], tagged=False), two_versions())
        self.open(item)
        tiles = self.scene_tiles()["items"]
        self.assertEqual([t["_image_url"] for t in tiles], [None] * 3)


class Pending(Rig):
    pool = _DeferredPool

    def setUp(self):
        super().setUp()
        self.item = movie(chapters("T", [0, 6, 12, 18, 24]),
                          two_versions() + [{"Id": "v3", "Name": "Extended",
                                             "MediaStreams": stream()}])
        self.src.chapters["v2"] = chapters("D", [0, 14, 20])
        self.src.chapters["v3"] = chapters("E", [0, 3, 9, 27])
        self.open(self.item)

    def test_nothing_is_shown_while_the_request_is_in_flight(self):
        self.pick(1)
        self.build()
        self.assertEqual(len(self.pool_obj.queued), 1)
        nodes, _h = self.build()
        self.assertEqual([i for i in ids(nodes) if isinstance(i, str)
                          and i.startswith("detail-scenes")], [])
        self.assertEqual(len(self.pool_obj.queued), 1,
                         "a repaint must not queue a second request")

    def test_a_late_result_for_a_deselected_version_is_not_displayed(self):
        self.pick(1); self.build()          # v2 queued
        self.pick(2); self.build()          # v3 queued
        self.pool_obj.release(0)            # v2 lands, but v3 is selected
        nodes, _h = self.build()
        self.assertEqual([i for i in ids(nodes) if isinstance(i, str)
                          and i.startswith("detail-scenes")], [])
        self.pool_obj.release(0)            # v3 lands
        nodes, _h = self.build()
        self.assertEqual(self.tile_ids(nodes, "v3"),
                         ["detail-scenes-v3-v3#ch%d" % i for i in range(4)])
        self.assertEqual(self.tile_ids(nodes, "v2"), [])

    def test_a_result_arriving_after_returning_to_the_primary_stays_hidden(self):
        self.pick(1); self.build()
        self.pick(0)
        self.pool_obj.release(0)            # v2's chapters land
        nodes, _h = self.build()
        self.assertEqual(len(self.tile_ids(nodes)), 5)        # the primary's
        self.assertEqual(self.tile_ids(nodes, "v2"), [])
        self.pick(1)                        # now it is there at once
        nodes, _h = self.build()
        self.assertEqual(len(self.tile_ids(nodes, "v2")), 3)
        self.assertEqual(self.src.chapter_calls, ["v2"])

    def test_a_reload_during_a_request_is_not_repopulated_by_it(self):
        self.pick(1); self.build()
        route = self.b.route
        self.page._landed(dict(route["_data"]))     # the page's data reloads
        self.assertEqual(route["_chapters"], {})
        self.pool_obj.release(0)                    # the old request answers
        self.assertEqual(route["_chapters"], {},
                         "a result from before the reload repopulated it")
        self.assertEqual(route["_chapters_pending"], set())
        self.build()                                # and it may ask again
        self.assertEqual(len(self.pool_obj.queued), 1)
        self.pool_obj.release(0)
        nodes, _h = self.build()
        self.assertEqual(self.src.chapter_calls, ["v2", "v2"])
        self.assertEqual(len(self.tile_ids(nodes, "v2")), 3)

    def test_a_failure_from_before_a_reload_does_not_poison_the_new_cache(self):
        self.src.chapters["v2"] = OSError("boom")
        self.pick(1); self.build()
        route = self.b.route
        self.page._landed(dict(route["_data"]))
        self.pool_obj.release(0)
        self.assertEqual(route["_chapters"], {})

    def test_a_reload_clears_the_cache_and_permits_a_retry(self):
        self.src.chapters["v2"] = OSError("boom")
        self.pick(1)
        for _ in range(2):
            self.build()
            self.pool_obj.drain()
        self.assertEqual(self.src.chapter_calls, ["v2"])
        self.assertEqual(self.b.route["_chapters"], {"v2": []})
        self.src.chapters["v2"] = chapters("D", [0, 14, 20])
        self.page._landed(dict(self.b.route["_data"]))
        self.build()
        self.pool_obj.drain()
        nodes, _h = self.build()
        self.assertEqual(self.src.chapter_calls, ["v2", "v2"])
        self.assertEqual(len(self.tile_ids(nodes, "v2")), 3)

    def test_navigating_away_drops_the_answer_and_releases_the_guard(self):
        self.pick(1); self.build()
        route = self.b.route
        self.assertEqual(route["_chapters_pending"], {"v2"})
        self.b.navigate({"kind": "home", "server": "srv1"})
        self.pool_obj.release(0)            # lands after the epoch moved
        self.assertEqual(route["_chapters"], {})
        self.assertEqual(route["_chapters_pending"], set())

class Reload(Rig):
    def test_data_landing_resets_cache_pending_and_bumps_the_generation(self):
        item = movie(chapters("T", [0, 6, 12]), two_versions())
        self.src.chapters["v2"] = chapters("D", [0, 14, 20])
        self.open(item)
        route = self.b.route
        gen = route["_chapters_gen"]
        self.pick(1); self.build(); self.build()
        self.assertIn("v2", route["_chapters"])
        self.page._landed(dict(route["_data"]))
        self.assertEqual(route["_chapters"], {})
        self.assertEqual(route["_chapters_gen"], gen + 1)
        self.assertIn("_data", route)


class Sanitiser(unittest.TestCase):
    def test_only_well_formed_chapters_survive(self):
        out = _clean_chapters([
            {"Name": "A", "StartPositionTicks": 5, "ImageTag": "t"},
            {"Name": None, "StartPositionTicks": 2.9, "ImageTag": ""},
            {"StartPositionTicks": True}, 3, None, "x"])
        self.assertEqual(out, [
            {"Name": "A", "StartPositionTicks": 5, "ImageTag": "t"},
            {"Name": None, "StartPositionTicks": 2, "ImageTag": None},
            {"Name": None, "StartPositionTicks": 0, "ImageTag": None}])

    def test_not_a_list_is_nothing(self):
        for bad in (None, "x", 3, {"Chapters": []}, (1, 2)):
            self.assertEqual(_clean_chapters(bad), [])


class Repository(unittest.TestCase):
    def _live(self, answer):
        from jellyfin_mpv_shim.mpvtk_browser.repository import LibrarySource
        src = LibrarySource.__new__(LibrarySource)
        conn = mock.Mock()
        conn.api.get_item.return_value = answer
        src._conns = {"srv": conn}
        return src, conn

    def test_it_asks_for_that_item_and_only_for_chapters(self):
        src, conn = self._live({"Chapters": [{"Name": "A"}, {"Name": "B"}]})
        self.assertEqual(src.get_chapters("srv", "v2"),
                         [{"Name": "A"}, {"Name": "B"}])
        conn.api.get_item.assert_called_once_with("v2", fields="Chapters")

    def test_missing_null_or_malformed_results_are_no_chapters(self):
        for bad in (None, {}, {"Chapters": None}, {"Chapters": "x"},
                    {"Chapters": {"a": 1}}, "junk", [], 5):
            with self.subTest(bad=bad):
                src, _c = self._live(bad)
                self.assertEqual(src.get_chapters("srv", "v2"), [])

    def test_a_server_error_propagates_to_the_caller(self):
        src, conn = self._live(None)
        conn.api.get_item.side_effect = OSError("down")
        with self.assertRaises(OSError):
            src.get_chapters("srv", "v2")

    def test_offline_does_not_know_a_versions_chapters(self):
        from tests.test_browser_features import _offline_source
        self.assertIsNone(_offline_source([]).get_chapters("offline", "v2"))
        self.assertIsNone(_offline_source([]).chapter_image_url(
            "offline", "v2", 0, {"ImageTag": "t"}))


class Scope(unittest.TestCase):
    def test_the_players_primary_chapters_still_come_from_the_item(self):
        # The player is version-aware for alternates (tests/
        # test_chapter_images.py); the primary and single-version path is
        # still the item's own list, with no request.
        import inspect
        from jellyfin_mpv_shim import media
        self.assertIn('self.item.get("Chapters")',
                      inspect.getsource(media.Video._chapters_of))


if __name__ == "__main__":
    unittest.main()
