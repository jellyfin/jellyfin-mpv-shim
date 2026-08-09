"""Browsing video libraries: tiles, grids, detail pages and their actions.
"""

import re
import unittest
from jellyfin_mpv_shim.mpvtk.layout import layout
from jellyfin_mpv_shim.mpvtk_browser import components
from jellyfin_mpv_shim.mpvtk_browser import theme, tile_renderer
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser

from tests._shell_harness import (
    FakeController,
    FakeSource,
    StubHudApp,
    _DeferredPool,
    _SyncPool,
    build_scene,
    detail_page,
    grid_scroll,
    home_page,
    ids,
    menu_pick,
    music_page,
    series_page,
    types,
)


class TestBannerFetchIsQuantised(unittest.TestCase):
    """Issue #592: resizing the window re-requested the header image.

    The artwork cache keys on exact pixel dimensions, so a banner width that
    followed the window pixel-for-pixel meant a request and a decode per
    pixel of drag, all resident until the LRU pushed them out.

    The fix quantises the FETCH, not the layout. Quantising the layout is
    what the first attempt did, and it overhung the scrollbar when the
    rounded-up width exceeded the space available.
    """

    def _r(self):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        return TileRenderer.__new__(TileRenderer)

    def _layout(self, lo, hi):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r = self._r()
        return [TileRenderer.banner_box(r, w)[0] for w in range(lo, hi)]

    def _fetches(self, lo, hi):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r = self._r()
        return [TileRenderer._banner_fetch_w(r, w) for w in self._layout(lo, hi)]

    def test_the_drawn_banner_never_exceeds_the_space_it_has(self):
        """The regression the first attempt caused: a rounded-up layout
        width paints over the scrollbar."""
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r = self._r()
        for w in (500, 700, 913, 1024, 1280, 1600):
            with self.subTest(w=w):
                got, _h = TileRenderer.banner_box(r, w)
                self.assertLessEqual(got, w - 2 * chrome.CONTENT_PAD)

    def test_and_leaves_no_gap_beside_full_width_content(self):
        """Which is why the layout is not quantised at all: rounding down
        would leave the header short of content that does reach the edge."""
        from jellyfin_mpv_shim.mpvtk_browser.components import chrome
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r = self._r()
        for w in (913, 1001, 1077):
            with self.subTest(w=w):
                got, _h = TileRenderer.banner_box(r, w)
                self.assertEqual(got, w - 2 * chrome.CONTENT_PAD)

    def test_a_long_drag_asks_for_only_a_handful_of_images(self):
        self.assertLessEqual(len(set(self._fetches(400, 1600))), 10,
                             "a resize still asks for an image per pixel")

    def test_neighbouring_widths_share_a_fetch(self):
        """The property that matters: moving the edge by one pixel almost
        never costs a request."""
        f = self._fetches(400, 1600)
        changes = sum(1 for a, b in zip(f, f[1:]) if a != b)
        self.assertLessEqual(changes, 10)

    def test_the_fetch_is_never_smaller_than_the_drawn_banner(self):
        """compose_banner crops the fetched image into the banner, so larger
        costs nothing and smaller would have to be upscaled."""
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r = self._r()
        for drawn in (300, 512, 513, 1000, 1084):
            with self.subTest(drawn=drawn):
                self.assertGreaterEqual(
                    TileRenderer._banner_fetch_w(r, drawn), drawn)

    def test_a_degenerate_width_does_not_go_negative(self):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        self.assertEqual(TileRenderer._banner_fetch_w(self._r(), 0), 0)
        self.assertEqual(TileRenderer._banner_fetch_w(self._r(), -5), 0)

    def _asked_for(self, width=1280):
        """Drive the real backdrop_node and record what it asks the server
        for. `thumbs=None` stops _request_image before any fetch, which is
        all this needs — the URL is built first."""
        from types import SimpleNamespace
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        asked = {}

        class _Source:
            @staticmethod
            def backdrop_spec(_item):
                return ("m1", "Backdrop", "tag9")

            @staticmethod
            def backdrop_url(_server, _item, width=None, height=None,
                             fill=False):
                asked.update(width=width, height=height, fill=fill)
                return "http://srv/bd.jpg"

            @staticmethod
            def image_spec(_item, _type="Primary", _w=280, inherit=True):
                # The header's inset poster (#7). Modelled with a REAL
                # spec, not None: a fake that answers None here leaves the
                # poster path unexercised while every banner test still
                # passes, which is the exact trap this file's siblings
                # document.
                return ("m1", "Primary", "ptag")

            @staticmethod
            def image_url(_server, _id, _type, _tag, w=None, h=None,
                          fill=False, index=None):
                return "http://srv/poster.jpg"

        r = self._r()
        r.art = SimpleNamespace(server="srv1", source=_Source(), thumbs=None)
        r._requested, r._img_retry = set(), {}
        box = TileRenderer.banner_box(r, width)
        TileRenderer.backdrop_node(r, {"Id": "m1"}, box, "detail-bd")
        return asked, box

    def test_the_banner_is_fetched_at_the_banners_aspect(self):
        """`fill=True` is fillWidth+fillHeight: the server CROPS to the shape
        asked for. Asking for a square hands back the centre square of a
        16:9 backdrop, and compose_banner's cover then blows that up to the
        full width — every detail header zoomed ~1.8x, for 2.7x the pixels,
        in the commit whose point was making banners cheaper.
        """
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        asked, _box = self._asked_for()
        self.assertTrue(asked["fill"], "without fill the server squashes")
        self.assertLess(asked["height"], asked["width"], "asked for a square")
        r = self._r()
        self.assertAlmostEqual(asked["height"] / asked["width"],
                               TileRenderer.BANNER_RATIO, delta=0.01)

    def test_the_fetched_height_still_covers_the_drawn_banner(self):
        """The other half: a crop that came back shorter than the box would
        have to be upscaled to cover it."""
        for width in (700, 1024, 1280, 1600):
            with self.subTest(width=width):
                asked, box = self._asked_for(width)
                from jellyfin_mpv_shim.mpvtk import scaling
                self.assertGreaterEqual(asked["height"],
                                        scaling.raster(*box)[1])

    def test_the_aspect_ratio_survives(self):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        r = self._r()
        w, h = TileRenderer.banner_box(r, 1280)
        self.assertEqual(h, int(w * TileRenderer.BANNER_RATIO))


class TestLibraryGridShape(unittest.TestCase):
    """A library grid is shaped by its own artwork, like jellyfin-web's.

    It used to be poster for every collection type, which is why a Home
    Videos library -- 16:9 clips with no poster art to crop -- came out
    portrait. Web asks for CardShape.Auto everywhere and lets the median
    PrimaryImageAspectRatio decide; movies look like posters because movie
    posters are 2:3, not because anything says "movies are posters".
    """

    def _grid(self, ratios, kind="grid"):
        src = FakeSource()
        src.grid_items = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Video",
             **({"PrimaryImageAspectRatio": r} if r else {})}
            for i, r in enumerate(ratios)]
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": kind, "server": "srv1", "parent_id": "lib1",
                    "title": "Lib"})
        return b

    def _tile_size(self, ratios):
        b = self._grid(ratios)
        nodes, _h = build_scene(b)
        hit = [n for n in nodes
               if re.match(r"^grid-\d+-", str(n.get("id", "")))
               and n["t"] == "rect"]
        self.assertTrue(hit, "no grid tiles")
        return hit[0]["w"], hit[0]["h"]

    def test_posters_stay_posters(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        w, _h = self._tile_size([2 / 3] * 6)
        self.assertEqual(w, POSTER_GEOM.tile_w)

    def test_sixteen_by_nine_clips_get_landscape_tiles(self):
        """The Home Videos complaint."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        w, _h = self._tile_size([16 / 9] * 6)
        self.assertEqual(w, LANDSCAPE_GEOM.tile_w)

    def test_four_three_clips_get_landscape_tiles_too(self):
        """4/3 is 1.3333 and the threshold is 1.33 -- a 0.0033 margin, so
        this is arithmetic worth pinning rather than trusting."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        w, _h = self._tile_size([4 / 3] * 6)
        self.assertEqual(w, LANDSCAPE_GEOM.tile_w)

    def test_phone_verticals_get_poster_tiles(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM
        w, _h = self._tile_size([0.5625] * 6)
        self.assertEqual(w, POSTER_GEOM.tile_w)

    def test_square_art_gets_square_tiles(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import SQUARE_GEOM
        w, _h = self._tile_size([1.0] * 6)
        self.assertEqual(w, SQUARE_GEOM.tile_w)

    def test_no_artwork_at_all_falls_back_to_square(self):
        """Web's fallback (cardBuilder.js:102-104). It fires precisely for
        an art-less grid -- the server sets the ratio from the Primary
        image -- and square placeholders tile better than tall ones."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import SQUARE_GEOM
        w, _h = self._tile_size([None] * 6)
        self.assertEqual(w, SQUARE_GEOM.tile_w)

    def test_the_median_ignores_items_with_no_ratio(self):
        """A handful of art-less items in a real library must not drag the
        shape to the fallback."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        w, _h = self._tile_size([16 / 9, None, 16 / 9, None, 16 / 9])
        self.assertEqual(w, LANDSCAPE_GEOM.tile_w)

    def test_a_median_near_four_three_snaps_up_to_landscape(self):
        """jellyfin-web rounds the median onto a canonical ratio before
        bucketing (imageLoader.js:209-233), and the bands overlap the
        thresholds: 1.19 is inside 4:3's +/-0.15 and outside square's, so
        web calls it 4:3 and draws landscape. Without the snap it is 1.19,
        which is > 0.8 and < 1.33 -- square. Real libraries sit near these
        numbers rather than on them, which is why web rounds at all."""
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        w, _h = self._tile_size([1.19] * 6)
        self.assertEqual(w, LANDSCAPE_GEOM.tile_w)

    def test_a_median_below_every_band_is_left_alone(self):
        """The snap must not invent a shape for content that is genuinely
        between the canonical ratios."""
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import _snap_ratio
        self.assertEqual(_snap_ratio(0.82), 0.82)
        self.assertEqual(_snap_ratio(2.4), 2.4)

    def test_the_snap_bands_are_webs(self):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import _snap_ratio
        self.assertAlmostEqual(_snap_ratio(0.5625), 2 / 3)
        self.assertAlmostEqual(_snap_ratio(0.75), 2 / 3)
        self.assertAlmostEqual(_snap_ratio(1.6), 16 / 9)
        self.assertAlmostEqual(_snap_ratio(1.05), 1.0)

    def test_the_shape_is_parked_so_paging_cannot_change_it(self):
        """A median taken per page would change the grid's shape as you
        scroll one library. A route is one folder, so the first page's
        median is the folder's."""
        b = self._grid([16 / 9] * 6)
        build_scene(b)
        first = b.route["_grid_shape"]
        # A later page of nothing but posters must not re-shape the grid.
        b.route["_items"] = b.route["_items"] + [
            {"Id": "p%d" % i, "Name": "P", "Type": "Movie",
             "PrimaryImageAspectRatio": 2 / 3} for i in range(40)]
        build_scene(b)
        self.assertEqual(b.route["_grid_shape"], first)

    def test_a_filter_change_takes_a_fresh_look(self):
        """_reload drops the items, so the parked shape has to go with them
        -- a different set of items deserves a different answer."""
        b = self._grid([16 / 9] * 6)
        build_scene(b)
        self.assertIn("_grid_shape", b.route)
        page = b._page_for(b.route)
        page._set_filter("genre", "Action")
        self.assertNotIn("_grid_shape", b.route)


class TestSearchEpisodeArtwork(unittest.TestCase):
    """A search result row of episodes is about the episodes.

    Inheriting draws one show's thumb over several of them, which is what
    made a season grid useless. jellyfin-web gets to the same place by a
    different route -- its search Episodes row sets no preferThumb at all,
    so it lands on the episode's own Primary -- but asking for a landscape
    image of the episode first fits the tile better.
    """

    def test_episode_results_do_not_inherit_series_artwork(self):
        seen = {}
        src = FakeSource()
        src.search = lambda server, term, limit=60: [
            {"Id": "ep1", "Name": "Ep", "Type": "Episode", "SeriesId": "S1"},
            {"Id": "mv1", "Name": "Film", "Type": "Movie"},
        ]
        real = src.image_spec

        def spy(item, image_type="Primary", width=280, inherit=True):
            seen[str(item.get("Id"))] = (image_type, inherit)
            return real(item, image_type, width, inherit=inherit)

        src.image_spec = spy
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "search", "server": "srv1", "term": "x"})
        build_scene(b)
        self.assertEqual(seen.get("ep1"), ("Thumb", False))
        # Everything else keeps inheriting -- a Movie has no series to
        # borrow from, but the flag must not have been flipped wholesale.
        self.assertEqual(seen.get("mv1"), ("Primary", True))


class TestChapterTileShape(unittest.TestCase):
    """A chapter thumbnail is a frame of the video, so the tile has to be the
    video's shape rather than 16:9 by assumption.

    jellyfin-web reads the first video stream and goes square at <= 1.2
    (chaptercardbuilder.js:30-39). Same rule, same threshold, because the
    alternative is a 4:3 or portrait source letterboxed inside a landscape
    card with black down both sides.
    """

    def _scene_tile(self, width, height):
        src = FakeSource()
        item = dict(src.get_item("srv1", "m1"))
        item["Chapters"] = [
            {"Name": "One", "StartPositionTicks": 0, "ImageTag": "t0"},
            {"Name": "Two", "StartPositionTicks": 10 ** 8, "ImageTag": "t1"},
        ]
        streams = [{"Type": "Audio", "Index": 1}]
        if width:
            streams.insert(0, {"Type": "Video", "Width": width,
                               "Height": height})
        item["MediaSources"] = [{"Id": "src1", "MediaStreams": streams}]
        src.get_item = lambda server, item_id: dict(item)
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1",
                    "title": "Movie"})
        nodes, _h = build_scene(b)
        hit = [n for n in nodes
               if str(n.get("id", "")).startswith("detail-scenes-")
               and n["t"] == "rect"]
        self.assertTrue(hit, "no scene tiles")
        return hit[0]["w"], hit[0]["h"]

    def test_a_widescreen_source_gets_landscape_scenes(self):
        self.assertEqual(self._scene_tile(1920, 1080),
                         self._scene_tile(1280, 720))

    def test_a_four_three_source_gets_square_scenes(self):
        """4:3 is 1.333, comfortably over the threshold, so it stays
        landscape -- the square case is 1.2 and below."""
        self.assertEqual(self._scene_tile(640, 480),
                         self._scene_tile(1920, 1080))

    def test_a_portrait_source_gets_square_scenes(self):
        wide = self._scene_tile(1920, 1080)
        tall = self._scene_tile(1080, 1920)
        self.assertNotEqual(tall, wide,
                            "a portrait video drew landscape scene tiles")

    def test_a_square_source_gets_square_scenes(self):
        self.assertNotEqual(self._scene_tile(1000, 1000),
                            self._scene_tile(1920, 1080))

    def test_no_video_stream_falls_back_to_landscape(self):
        """Audio-only or a DTO without MediaStreams: the old behaviour, not
        a crash and not an accidental square."""
        self.assertEqual(self._scene_tile(None, None),
                         self._scene_tile(1920, 1080))


class TestEpisodeImagesPreference(unittest.TestCase):
    """``useEpisodeImagesInNextUpAndResume`` reaches the tiles.

    The preference is web's and defaults to *off*, meaning series artwork
    wins over the episode still -- the spoiler complaint. It applies to
    Continue Watching / Listening and Next Up, and to nothing else.
    """

    def _rows_asking(self, episode_images, kinds):
        """``{row kind: inherit}`` for a home screen built with the setting
        at ``episode_images`` and one row of each of ``kinds``."""
        src = FakeSource()
        src.get_user_prefs = (
            lambda server, refresh=False: {"episode_images": episode_images})
        src.home_rows = [
            {"title": k, "kind": k, "collection_type": None, "slot": i,
             "items": [{"Id": "%s1" % k, "Name": "X", "Type": "Episode",
                        "SeriesId": "S1"}]}
            for i, k in enumerate(kinds)]

        seen = {}
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        real = b.tiles.image_map

        def spy(items, prefix, geom=None, image_type="Primary", **kw):
            for it in items:
                seen[str(it.get("Id"))] = kw.get("inherit", True)
            return real(items, prefix, geom, image_type, **kw)

        b.tiles.image_map = spy
        b.navigate({"kind": "home", "server": "srv1"})
        build_scene(b)
        self.assertTrue(seen, "no tiles were built at all")
        return seen

    def test_the_default_inherits_series_artwork(self):
        seen = self._rows_asking(False, ["resume", "nextup"])
        self.assertEqual(seen.get("resume1"), True)
        self.assertEqual(seen.get("nextup1"), True)

    def test_turning_it_on_stops_inheriting(self):
        seen = self._rows_asking(True, ["resume", "nextup", "resumeaudio"])
        self.assertEqual(seen.get("resume1"), False)
        self.assertEqual(seen.get("nextup1"), False)
        self.assertEqual(seen.get("resumeaudio1"), False)

    def test_latest_rows_never_follow_the_setting(self):
        """The trap. A Latest-Episodes row sets preferThumb with no
        inheritThumb at all in web (utils/sections.ts:135-144), so it always
        prefers series artwork -- and ours goes further, drawing those rows
        as the *show* (ParentPrimary, captioned with the series name). If
        LATEST followed the setting, turning it on would scatter episode
        stills through a row that is a list of shows.
        """
        seen = self._rows_asking(True, ["latestmedia"])
        self.assertEqual(seen.get("latestmedia1"), True,
                         "a Latest row followed the episode-images setting")

    def test_an_unknown_row_kind_inherits(self):
        seen = self._rows_asking(True, ["activerecordings"])
        self.assertEqual(seen.get("activerecordings1"), True)

    def test_a_source_without_the_method_still_loads(self):
        """The offline source has no display preferences. It must fall back
        to the default rather than raise inside the home fan-out -- that
        exception re-triggers the fallback that got us there."""
        src = FakeSource()
        self.assertFalse(hasattr(src, "get_user_prefs"))
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "home", "server": "srv1"})
        nodes, _h = build_scene(b)
        self.assertTrue(nodes, "the home screen did not render at all")


class TestTileShapes(unittest.TestCase):
    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()

    def test_row_shape_classification(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import (
            POSTER_GEOM, LANDSCAPE_GEOM, SQUARE_GEOM)
        g, _it = home_page(self.b)._row_shape({"collection_type": "movies", "items": []})
        self.assertIs(g, POSTER_GEOM)
        g, _it = home_page(self.b)._row_shape({"collection_type": "music", "items": []})
        self.assertIs(g, SQUARE_GEOM)
        g, it = home_page(self.b)._row_shape(
            {"collection_type": None, "items": [{"Type": "Episode"}]})
        self.assertIs(g, LANDSCAPE_GEOM)
        self.assertEqual(it, "Thumb")
        # collection-type wins over a stray episode in the row
        g, _it = home_page(self.b)._row_shape(
            {"collection_type": "tvshows", "items": [{"Type": "Episode"}]})
        self.assertIs(g, POSTER_GEOM)

    def test_latest_tv_is_shaped_like_any_other_tv_row(self):
        """Drawing its Episodes as their series is a per-item image choice
        (see _tile); it must not reshape the row."""
        from jellyfin_mpv_shim.mpvtk_browser import home_sections
        from jellyfin_mpv_shim.mpvtk_browser.strips import POSTER_GEOM

        g, it = home_page(self.b)._row_shape(
            {"collection_type": "tvshows", "kind": home_sections.LATEST,
             "items": [{"Type": "Episode"}]})
        self.assertIs(g, POSTER_GEOM)
        self.assertEqual(it, "Primary")

    def test_only_episodes_swap_to_the_series_poster(self):
        """The Series entries the same row carries keep their own poster."""
        seen = []
        self.b.source.image_spec = (
            lambda i, t="Primary", w=280, inherit=True:
            seen.append((i.get("Id"), t, inherit)))
        self.b.tiles._tile({"Id": "e1", "Type": "Episode"}, self.b.geom,
                           "Primary", True)
        self.b.tiles._tile({"Id": "s1", "Type": "Series"}, self.b.geom,
                           "Primary", True)
        # ...and elsewhere an episode still shows its own still.
        self.b.tiles._tile({"Id": "e2", "Type": "Episode"}, self.b.geom_wide,
                           "Thumb")
        # inherit rides along unchanged: parent_item is a different mechanism
        # (it overrides the requested *type*), and a row that has not opted
        # out still inherits.
        self.assertEqual(seen, [("e1", "ParentPrimary", True),
                                ("s1", "Primary", True),
                                ("e2", "Thumb", True)])

    def test_live_tv_falls_back_to_a_landscape_row(self):
        """Live TV rows are shaped by their artwork (see
        TileRenderer.auto_geom, which is jellyfin-web's rule); this is the
        fallback, for a row where nothing carries an aspect ratio.

        Landscape rather than the poster row it used to be, because guide
        entries rarely carry art of their own and most tiles end up on the
        channel logo (see repository.image_spec). Cropped to fill a 2:3
        poster a logo loses most of itself; a 16:9 frame keeps it readable.
        """
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM

        g, it = home_page(self.b)._row_shape(
            {"collection_type": "livetv", "items": [{"Type": "Program"}]})
        self.assertIs(g, LANDSCAPE_GEOM)
        self.assertEqual(it, "Thumb")

    def _long_row(self, n=30):
        """A libraries row with more tiles than fit, so it gets page buttons."""
        many = [dict(self.b.source.libraries[0], Id="lib%d" % i,
                     Name="Library %d" % i) for i in range(n)]
        self.b.route["_data"] = {"libraries": many, "rows": []}
        nodes, _h = build_scene(self.b)
        return {n["id"]: n for n in nodes}, nodes

    def test_scroll_arrows_appear_only_when_the_row_overflows(self):
        # One library fits, so no page buttons at all.
        self.b.route["_data"] = {"libraries": self.b.source.libraries,
                                 "rows": []}
        nodes, _h = build_scene(self.b)
        self.assertNotIn("row-libs-pl", ids(nodes))

        by_id, _nodes = self._long_row()
        self.assertIn("row-libs-pl", by_id)
        self.assertIn("row-libs-pr", by_id)

    def test_default_page_buttons_ride_the_heading_clear_of_the_artwork(self):
        """jellyfin-web's design, and the reason the default needs no
        compositing trick at all: the pair sits in the section heading, above
        the strip, so nothing is drawn over a poster."""
        by_id, _nodes = self._long_row()
        strip = by_id["row-libs"]
        left, right = by_id["row-libs-pl"], by_id["row-libs-pr"]
        for b in (left, right):
            self.assertLessEqual(b["y"] + b["h"], strip["y"] + 1)
        # ...and right-aligned, prev before next.
        self.assertLess(left["x"], right["x"])
        self.assertGreater(left["x"], strip["x"] + strip["w"] / 2)

    def test_a_page_button_with_nowhere_to_go_is_dimmed_not_hot(self):
        """An unscrolled row can only page forward. The back button stays put
        and loses its hover wash rather than disappearing, so the pair does not
        shuffle around as the row reaches its ends."""
        by_id, _nodes = self._long_row()
        left, right = by_id["row-libs-pl"], by_id["row-libs-pr"]
        self.assertNotIn("hover", left)
        self.assertIn("hover", right)
        # Only the live one hold-repeats.
        self.assertNotIn("rpt", left)
        self.assertTrue(right.get("rpt"))

    def test_the_disabled_state_follows_the_row_as_it_is_paged(self):
        """The row's scroll lives entirely in the renderer, which only reports
        back when the container asks to be watched. Without that watch the
        buttons were built once at offset 0 and never restyled: back
        permanently dim, forward permanently lit, however far the row had been
        paged."""
        by_id, _nodes = self._long_row()
        self.assertNotIn("hover", by_id["row-libs-pl"])   # at the start

        # Somewhere in the middle: both directions are live.
        self.b._scroll.on_scroll("row-libs", 900, 4000, edges_only=True)
        by_id, _nodes = self._long_row()
        self.assertIn("hover", by_id["row-libs-pl"])
        self.assertIn("hover", by_id["row-libs-pr"])

        # ...and against the far end, forward goes dim instead.
        strip = by_id["row-libs"]
        max_offset = strip["cw"] - strip["w"]
        self.b._scroll.on_scroll("row-libs", max_offset, max_offset,
                                edges_only=True)
        by_id, _nodes = self._long_row()
        self.assertIn("hover", by_id["row-libs-pl"])
        self.assertNotIn("hover", by_id["row-libs-pr"])

    def test_a_watch_is_only_asked_for_when_the_row_can_page(self):
        """Watching costs a renderer->Python event per scroll, so a row that
        fits does not ask for one."""
        self.b.route["_data"] = {"libraries": self.b.source.libraries,
                                 "rows": []}
        nodes, _h = build_scene(self.b)
        by_id = {n["id"]: n for n in nodes}
        self.assertNotIn("watch", by_id["row-libs"])

        by_id, _nodes = self._long_row()
        self.assertTrue(by_id["row-libs"].get("watch"))

    def test_overlay_mode_composites_over_the_strip_instead_of_punching_it(self):
        """Nebula keeps the arrows ON the artwork. They are bitmaps, which mpv
        composites ABOVE the strip and alpha-blends with it. The ASS-button
        version could not do either, so it had to punch an occluder rect out of
        the strip below — a hard-edged notch in the artwork."""
        theme.apply("nebula")
        try:
            by_id, nodes = self._long_row()
            self.assertEqual([n for n in nodes if n["t"] == "occ"], [])
            strip = by_id["row-libs"]
            left, right = by_id["row-libs-pl"], by_id["row-libs-pr"]
            for b in (left, right):
                self.assertEqual(b["t"], "img")
            pad = tile_renderer.ARROW_INSET
            self.assertAlmostEqual(left["x"], strip["x"] + pad, places=1)
            self.assertAlmostEqual(right["x"] + right["w"],
                                   strip["x"] + strip["w"] - pad, places=1)
            # Circular, and small enough to cover little artwork.
            self.assertEqual(left["w"], left["h"])
            self.assertLess(left["h"], strip["h"] / 2)
            # Same end-stop rule as the header pair, baked into the bitmap
            # rather than restyled (the renderer cannot restyle an image).
            self.assertNotIn("rpt", left)
            self.assertTrue(right.get("rpt"))
            self.assertNotEqual(left["src"], right["src"])
        finally:
            theme.apply("default")

    def test_arrows_hold_repeat(self):
        """Survives the move to bitmaps: Image carries ``repeat`` too, and the
        renderer keys hold-repeat off click/rpt regardless of node type."""
        theme.apply("nebula")
        try:
            by_id, _nodes = self._long_row()
            self.assertTrue(by_id["row-libs-pr"].get("rpt"))
        finally:
            theme.apply("default")

    def test_downloaded_and_glyph(self):
        self.b.tiles._downloaded = {"m1"}
        t = self.b.tiles._tile({"Id": "m1", "Name": "Alpha", "Type": "Movie"},
                         self.b.geom)
        self.assertTrue(t.downloaded)
        # Material icon names now, not characters -- jellyfin-web's map.
        # See test_photos.PhotoGlyphTest for why the rule changed.
        self.assertEqual(t.glyph, "movie")
        t2 = self.b.tiles._tile({"Id": "a1", "Name": "Song", "Type": "Audio"},
                          self.b.geom)
        self.assertEqual(t2.glyph, "audiotrack")

    def test_watched_series_fallback(self):
        t = self.b.tiles._tile({"Id": "s1", "Type": "Series",
                          "UserData": {"UnplayedItemCount": 0}}, self.b.geom)
        self.assertTrue(t.watched)
        t2 = self.b.tiles._tile({"Id": "s2", "Type": "Series",
                           "UserData": {"UnplayedItemCount": 3}}, self.b.geom)
        self.assertFalse(t2.watched)

    def test_season_episodes_are_landscape(self):
        from jellyfin_mpv_shim.mpvtk_browser.strips import LANDSCAPE_GEOM
        self.b.navigate({"kind": "season", "server": "srv1", "item_id": "se1",
                         "series_id": "sh1", "title": "Season 1"})
        nodes, _h = build_scene(self.b)
        imgs = [n for n in nodes if n["t"] == "img"]
        self.assertTrue(imgs)
        self.assertEqual(imgs[0]["ih"], LANDSCAPE_GEOM.strip_h)

class TestDetailActions(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _detail(self):
        self.b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1",
                         "title": "Movie"})
        return build_scene(self.b)

    def test_action_row_and_pickers_render(self):
        nodes, _h = self._detail()
        for nid in ("act-watched", "act-fav", "act-download",
                    "dt-audio", "dt-sub"):
            self.assertIn(nid, ids(nodes))
        # cast row present, single source -> no version picker
        self.assertNotIn("dt-version", ids(nodes))
        self.assertTrue(any(k.startswith("detail-people-") for k in _h))

    def test_track_selection_passed_to_play(self):
        """**No rebuild between the pick and the click.** There is none in
        the app either: a track picker writes to the route and forces no
        repaint, because nothing drawn depends on the choice and the dropdown
        shows its own selection.

        This used to rebuild the scene here, which quietly refreshed the Play
        button's closure and made the whole thing pass while the app was
        playing the *previous* selection. It came back as an intermittent bug
        precisely because a repaint from somewhere else -- a thumbnail
        landing, a websocket item update -- fixed it by accident, so it
        depended on how long you sat on the page.
        """
        _n, h = self._detail()
        h["dt-audio"]["select"](0, "English 5.1")     # aid=1
        h["dt-sub"]["select"](1, "English")           # sid=2 (index 0 = None)
        h["btn-play"]["click"]()
        self.assertEqual(self.ctl.tracks[-1],
                         {"srcid": "src1", "aid": 1, "sid": 2})

    def test_a_repaint_does_not_change_what_play_sends(self):
        """The same, with the repaint that used to be doing the work. Both
        orders have to give the same answer or one of them is luck."""
        _n, h = self._detail()
        h["dt-audio"]["select"](0, "English 5.1")
        h["dt-sub"]["select"](1, "English")
        _n, h = build_scene(self.b)
        h["btn-play"]["click"]()
        self.assertEqual(self.ctl.tracks[-1],
                         {"srcid": "src1", "aid": 1, "sid": 2})

    def test_resume_carries_the_selection_too(self):
        """Resume is a second closure over the same pair, and the button a
        part-watched item lands focused on."""
        self.b.navigate({"kind": "detail", "server": "srv1", "item_id": "m1",
                         "title": "Movie"})
        route = self.b.route
        route["_data"]["item"]["UserData"] = {
            "PlaybackPositionTicks": 6000000000}
        _n, h = build_scene(self.b)
        h["dt-audio"]["select"](0, "English 5.1")
        h["btn-resume"]["click"]()
        self.assertEqual(self.ctl.tracks[-1]["aid"], 1)
        self.assertEqual(self.ctl.played[-1][2], 6000000000)  # offset_ticks

    def test_mark_watched_from_detail(self):
        _n, h = self._detail()
        h["act-watched"]["click"]()
        self.assertIn("set_watched",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_cast_click_opens_person_route(self):
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        self.assertEqual(self.b.route["kind"], "person")
        nodes, _h = build_scene(self.b)
        self.assertIn("img", types(nodes))   # person filmography grid

    def test_cast_tiles_are_portrait_like_every_other_poster(self):
        """Jellyfin serves person Primary images at 2:3. A square tile
        letterboxed or cropped every face; geom_square is for album art.
        Asserted on the laid-out tile, not the geom constant, because the
        shape on screen is the thing that was wrong."""
        nodes, _h = self._detail()

        def tile(prefix):
            hit = [n for n in nodes
                   if str(n.get("id", "")).startswith(prefix + "-")
                   and n["t"] == "rect"]
            self.assertTrue(hit, "no tiles under %s" % prefix)
            return hit[0]["w"], hit[0]["h"]

        # Against the poster row in the same scene, not against a geom
        # constant: every geom produces a taller-than-wide tile once the
        # caption is added, so "is it portrait" cannot tell them apart.
        self.assertEqual(tile("detail-people"), tile("detail-similar"),
                         "cast tiles are not the same shape as posters")

    def test_a_filmography_can_be_sorted(self):
        """The filter bar is gated on kind == "grid" and person routes are
        "person", so a filmography had no ordering control at all — always
        by name, however long the credit list."""
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        _nodes, h = build_scene(self.b)
        self.assertIn("person-sort", h, "no sort control on a filmography")

        from jellyfin_mpv_shim.mpvtk_browser.views import SORTS
        want = next(i for i, s in enumerate(SORTS) if s[1] == "PremiereDate")
        h["person-sort"]["select"](want, SORTS[want][0])
        self.assertEqual(self.b.source.person_sorts[-1],
                         ("PremiereDate", "Descending"))

    def test_a_filmography_defaults_to_name(self):
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        self.assertEqual(self.b.source.person_sorts[-1],
                         ("SortName", "Ascending"))

    def test_the_filmography_sort_survives_paging(self):
        """The sort was read in _on_grid_scroll and then not passed to
        get_person_items, so page 1 honoured the dropdown and every page
        after it reverted to SortName — the two orderings interleave into
        duplicates and skips."""
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        _nodes, h = build_scene(self.b)
        from jellyfin_mpv_shim.mpvtk_browser.views import SORTS
        want = next(i for i, s in enumerate(SORTS) if s[1] == "PremiereDate")
        h["person-sort"]["select"](want, SORTS[want][0])

        self.b.route["_total"] = 500          # more to page in
        grid_scroll(self.b, self.b.route, 100000, 100001)
        self.assertEqual(self.b.source.person_sorts[-1],
                         ("PremiereDate", "Descending"),
                         "page 2 reverted to the default sort")

    def test_a_random_filmography_is_capped_to_one_page(self):
        """Random reshuffles per request, so paging it yields duplicates and
        skips. The grid caps for exactly this reason; the person route
        assigned the raw total and had the corruption the cap prevents."""
        # The server reports far more than it returned in this page — that
        # gap is what the pager uses to decide there is more to fetch, and
        # what the cap has to close.
        self.b.source.get_person_items = (
            lambda srv, pid, start_index=0, **kw:
            ([{"Id": "pf%d" % i, "Name": "F%d" % i, "Type": "Movie"}
              for i in range(20)], 500))

        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        _nodes, h = build_scene(self.b)
        from jellyfin_mpv_shim.mpvtk_browser.views import SORTS
        rnd = next(i for i, s in enumerate(SORTS) if s[1] == "Random")
        h["person-sort"]["select"](rnd, SORTS[rnd][0])
        route = self.b.route
        self.assertEqual(route["_total"], len(route["_items"]),
                         "a Random filmography still advertises more pages")

    def test_a_non_random_filmography_still_pages(self):
        """The cap must be Random-only — capping everything would silently
        truncate every long credit list at one page."""
        self.b.source.get_person_items = (
            lambda srv, pid, start_index=0, **kw:
            ([{"Id": "pf%d" % i, "Name": "F%d" % i, "Type": "Movie"}
              for i in range(20)], 500))
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        self.assertEqual(self.b.route["_total"], 500)

    def test_a_filmography_has_no_genre_or_year_filters(self):
        """They would be filtering one person's credits by genre, and the
        A-Z strip is meaningless over four films."""
        self.b._open_item({"Id": "pp1", "Name": "Actor", "Type": "Actor"})
        nodes, _h = build_scene(self.b)
        for nid in ("grid-genre", "grid-year", "grid-l-A"):
            self.assertNotIn(nid, ids(nodes))

    def test_episode_play_queues_season(self):
        ep = {"Id": "e1", "Type": "Episode", "SeriesId": "sh1"}
        self.b._play(ep, "srv1")
        ids_, srv, start = self.ctl.played[-1]
        self.assertEqual(len(ids_), 3)        # whole-season queue
        self.assertEqual(start, 0)

    def test_series_actions_next_up(self):
        self.b.navigate({"kind": "series", "server": "srv1", "item_id": "sh1",
                         "title": "Show"})
        _n, h = build_scene(self.b)
        self.assertIn("sa-nextup", ids(_n))
        h["sa-nextup"]["click"]()
        self.assertTrue(self.ctl.played)      # next-up episode played

class TestGridFilters(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def _grid(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        return build_scene(self.b)

    def _panel(self):
        """The grid with its filter panel open.

        The three drop-downs and two checkboxes that used to sit on the
        bar are behind one Filter button now -- the bar had 277px spare
        at 1280 and web offers eight categories, so they could not have
        stayed. Everything these tests assert is unchanged; only the door
        to it moved.
        """
        _n, h = self._grid()
        h["grid-filter"]["click"]()
        return build_scene(self.b)

    def test_filter_bar_present(self):
        nodes, _h = self._grid()
        for nid in ("grid-sort", "grid-filter", "grid-shuffle",
                    "grid-l-A", "grid-l-#"):
            self.assertIn(nid, ids(nodes))

    def test_the_filters_are_in_the_panel(self):
        nodes, _h = self._panel()
        for nid in ("flt-genre", "flt-unplayed", "flt-favorite", "flt-year"):
            self.assertIn(nid, ids(nodes))

    def test_sort_change_sets_and_reloads(self):
        _n, h = self._grid()
        h["grid-sort"]["select"](3, "Community Rating")
        self.assertEqual(self.b.route["_sort"], 3)

    def test_genre_filter(self):
        _n, h = self._panel()
        h["flt-genre"]["select"](1, "Action")   # index 0 = All Genres
        self.assertEqual(self.b.route["_filters"]["genre"], "Action")

    def test_unplayed_toggle(self):
        _n, h = self._panel()
        h["flt-unplayed"]["click"]()
        self.assertTrue(self.b.route["_filters"]["unplayed"])

    def test_letter_jump(self):
        _n, h = self._grid()
        h["grid-l-M"]["click"]()
        self.assertEqual(self.b.route["_filters"]["letter"], "M")

    def test_shuffle_plays(self):
        _n, h = self._grid()
        h["grid-shuffle"]["click"]()
        self.assertTrue(self.ctl.played)
        ids_, _srv, _s = self.ctl.played[-1]
        self.assertEqual(ids_, ["g0", "g5", "g9"])

    # The tests above assert the browser recorded the choice. These assert
    # the choice reaches the SOURCE — without them the view could stop
    # passing filters= entirely and every one of them stays green while no
    # filter in the app does anything.

    def _last_query(self):
        self.assertTrue(self.b.source.queries, "the source was never queried")
        return self.b.source.queries[-1]

    def test_the_sort_reaches_the_source(self):
        from jellyfin_mpv_shim.mpvtk_browser.views import SORTS
        _n, h = self._grid()
        want = next(i for i, s in enumerate(SORTS) if s[1] == "CommunityRating")
        h["grid-sort"]["select"](want, SORTS[want][0])
        q = self._last_query()
        self.assertEqual((q["sort_by"], q["sort_order"]),
                         ("CommunityRating", "Descending"))

    def test_the_genre_filter_reaches_the_source(self):
        _n, h = self._panel()
        h["flt-genre"]["select"](1, "Action")
        self.assertEqual(self._last_query()["filters"].get("genre"), "Action")

    def test_the_unplayed_toggle_reaches_the_source(self):
        _n, h = self._panel()
        h["flt-unplayed"]["click"]()
        self.assertTrue(self._last_query()["filters"].get("unplayed"))

    def test_the_letter_jump_reaches_the_source(self):
        _n, h = self._grid()
        h["grid-l-M"]["click"]()
        self.assertEqual(self._last_query()["filters"].get("letter"), "M")

    def test_the_year_filter_reaches_the_source(self):
        _n, h = self._panel()
        h["flt-year"]["select"](1, "2020")
        self.assertEqual(self._last_query()["filters"].get("year"), 2020)

    def test_filters_accumulate_rather_than_replace(self):
        """Picking a genre then a year must send both — dropping the first
        would silently widen the result set."""
        _n, h = self._panel()
        h["flt-genre"]["select"](1, "Action")
        _n, h = build_scene(self.b)
        h["flt-unplayed"]["click"]()
        f = self._last_query()["filters"]
        self.assertEqual(f.get("genre"), "Action")
        self.assertTrue(f.get("unplayed"))

    def test_windowing_carries_the_filters(self):
        """A window fetched later losing them is how the person route
        shipped: the two result sets interleave into duplicates and skips."""
        _n, h = self._panel()
        h["flt-genre"]["select"](1, "Action")
        before = len(self.b.source.queries)
        grid_scroll(self.b, self.b.route, 100000, 200000)
        self.assertGreater(len(self.b.source.queries), before,
                           "no window was fetched for where we scrolled to")
        self.assertEqual(self._last_query()["filters"].get("genre"), "Action")

class TestTileContextMenu(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_context_opens_menu(self):
        self.b._open_tile_menu({"Id": "m1", "Name": "A", "Type": "Movie"},
                               100, 200)
        nodes, _h = build_scene(self.b)
        self.assertTrue(any(n["t"] == "menu" for n in nodes))

    def test_mark_watched_calls_client_and_updates_item(self):
        item = {"Id": "m1", "Name": "A", "Type": "Movie",
                "UserData": {"Played": False}}
        self.b._open_tile_menu(item, 10, 10)
        menu_pick(self.b, "watched")
        self.assertTrue(item["UserData"]["Played"])
        self.assertIsNone(self.b._menu)       # menu closed
        calls = getattr(self.ctl, "transport", [])
        self.assertIn("set_watched", [c[0] for c in calls])

    def test_toggle_favorite_calls_client(self):
        item = {"Id": "m1", "Type": "Movie", "UserData": {"IsFavorite": False}}
        self.b._open_tile_menu(item, 10, 10)
        menu_pick(self.b, "favorite")
        self.assertTrue(item["UserData"]["IsFavorite"])
        self.assertIn("set_favorite",
                      [c[0] for c in getattr(self.ctl, "transport", [])])

    def test_menu_play_audio_plays(self):
        item = {"Id": "s1", "Type": "Audio"}
        self.b._open_tile_menu(item, 10, 10)
        menu_pick(self.b, "play")
        self.assertTrue(self.ctl.played)

    # -- resume ---------------------------------------------------------
    #
    # The tile's play gesture is the one on the home screen's Continue
    # Watching row, where every item has a position by definition. It used to
    # start at zero, throwing that position away with nothing on screen to
    # say so.

    def _pos_item(self, ticks=600000000):
        return {"Id": "m1", "Name": "A", "Type": "Movie",
                "UserData": {"PlaybackPositionTicks": ticks}}

    def test_the_play_chip_resumes(self):
        self.b._play_tile(self._pos_item())
        self.assertEqual([p[2] for p in self.ctl.played], [600000000])

    def test_the_play_chip_starts_a_fresh_item_at_zero(self):
        self.b._play_tile({"Id": "m2", "Type": "Movie", "UserData": {}})
        self.assertEqual([p[2] for p in self.ctl.played], [None])

    def test_a_resumable_item_offers_both_readings(self):
        acts = [e[2] for e in self.b._tile_menu_entries(self._pos_item())]
        self.assertIn("play", acts)
        self.assertIn("restart", acts)

    def test_an_unstarted_item_offers_only_play(self):
        acts = [e[2] for e in self.b._tile_menu_entries(
            {"Id": "m2", "Type": "Movie", "UserData": {}})]
        self.assertNotIn("restart", acts)

    def test_a_container_offers_no_restart(self):
        """Its Play resolves to a queue; there is no one position to skip."""
        acts = [e[2] for e in self.b._tile_menu_entries(
            {"Id": "a1", "Type": "MusicAlbum",
             "UserData": {"PlaybackPositionTicks": 5}})]
        self.assertNotIn("restart", acts)

    def test_menu_play_resumes(self):
        item = self._pos_item()
        self.b._open_tile_menu(item, 10, 10)
        menu_pick(self.b, "play")
        self.assertEqual([p[2] for p in self.ctl.played], [600000000])

    def test_menu_play_from_beginning_does_not_resume(self):
        item = self._pos_item()
        self.b._open_tile_menu(item, 10, 10)
        menu_pick(self.b, "restart")
        self.assertEqual([p[2] for p in self.ctl.played], [None])

    def test_dismiss_closes_menu(self):
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        self.b._close_menu()
        self.assertIsNone(self.b._menu)

class TestVirtualizedGrid(unittest.TestCase):
    """Long grids must only composite the rows near the viewport: rendering
    all of them blew past the strip cache and mpv's 63-overlay budget, which
    showed as tiles that came back blank after scrolling away and back."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        # A library far taller than one screen.
        self.b.route["_items"] = [
            {"Id": "g%d" % i, "Name": "Item %d" % i, "Type": "Movie"}
            for i in range(600)]
        self.b.route["_total"] = 600

    def _strip_count(self, nodes):
        return len([n for n in nodes if n["t"] == "img"])

    def test_only_a_window_of_rows_is_composited(self):
        nodes, _h = build_scene(self.b)
        n = self._strip_count(nodes)
        self.assertGreater(n, 0)
        self.assertLess(n, 40, "should not materialize every row")

    def test_scrolling_moves_the_window(self):
        build_scene(self.b)
        top = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.b._on_scroll("grid", 6000, 20000)
        bottom = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.assertTrue(top and bottom)
        self.assertNotEqual(top, bottom)

    def test_scrolling_back_re_materializes_the_original_rows(self):
        first = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.b._on_scroll("grid", 6000, 20000)
        build_scene(self.b)
        self.b._on_scroll("grid", 0, 20000)
        again = {r["id"] for r in build_scene(self.b)[0] if r["t"] == "img"}
        self.assertEqual(first, again)

class TestRemoteDisplayContent(unittest.TestCase):
    """Jellyfin's DisplayContent ("show me this" from a phone) opens the
    item's page in the browser, which the remote's arrows can then drive.
    In headless mode it paints the item on the cast screen instead — see
    tests/test_mpvtk_headless.py."""

    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_opens_the_item_page(self):
        self.b.display_item("srv1", "m1")
        self.assertEqual(self.b.route["kind"], "detail")
        self.assertEqual(self.b.route["item_id"], "m1")

    def test_routes_by_item_type(self):
        """A series lands on the series page, not a detail page — the same
        dispatch a click uses."""
        src = self.b.source
        src.get_item = lambda s, i: {"Id": i, "Name": "Show", "Type": "Series"}
        self.b.display_item("srv1", "sh1")
        self.assertEqual(self.b.route["kind"], "series")

    def test_wakes_a_minimized_client_only_when_asked_to(self):
        """This used to wake unconditionally. It no longer does by default:
        the browser being closed to the tray is a deliberate state, and
        someone idly scrolling a phone should not take over the TV. The
        route is still set, so the page is waiting when it is opened.
        See TestCastingDoesNotSummonTheBrowser for the default."""
        from jellyfin_mpv_shim.conf import settings
        saved = settings.display_mirror_summon
        self.addCleanup(
            lambda: setattr(settings, "display_mirror_summon", saved))
        settings.display_mirror_summon = True
        self.b.minimize()
        self.b.display_item("srv1", "m1")
        self.assertFalse(self.b.minimized)
        self.assertTrue(self.b._browsing)
        self.assertEqual(self.b.route["kind"], "detail")

    def test_never_interrupts_playback(self):
        """jellyfin-web emits DisplayContent as you browse on the phone, so
        casting a page while something plays here must not stop it."""
        self.b._browsing = False        # video playing
        self.b.display_item("srv1", "m1")
        self.assertFalse(self.b._browsing, "took the window from playback")
        self.assertEqual(self.ctl.entered, 0)
        # The page is waiting when playback ends.
        self.assertEqual(self.b.route["kind"], "detail")

    def test_a_cast_track_opens_its_album_rather_than_playing(self):
        """Same reason: DisplayContent is a browse gesture, not a play one."""
        self.b.source.get_item = lambda s, i: {
            "Id": i, "Name": "Song", "Type": "Audio", "AlbumId": "al9",
            "Album": "The Album"}
        self.b.display_item("srv1", "so1")
        self.assertEqual(self.b.route["kind"], "album")
        self.assertEqual(self.b.route["item_id"], "al9")
        self.assertEqual(self.ctl.played, [], "must not start playback")

    def test_a_cast_track_with_no_album_falls_back(self):
        self.b.source.get_item = lambda s, i: {
            "Id": i, "Name": "Song", "Type": "Audio"}
        self.b.display_item("srv1", "so1")
        self.assertEqual(self.ctl.played, [], "must not start playback")

    def test_switches_server_when_the_cast_comes_from_another(self):
        self.b.display_item("srv2", "m1")
        self.assertEqual(self.b.server, "srv2")

    def test_go_to_settings_opens_the_settings_page(self):
        """GoToSettings used to alias to GoHome, which predates the browser
        having a settings page."""
        self.assertTrue(self.b.on_nav_command("settings"))
        self.assertEqual(self.b.route["kind"], "settings")

    def test_go_home_resets_to_the_library(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies"})
        self.assertTrue(self.b.on_nav_command("home"))
        self.assertEqual(self.b.route["kind"], "home")
        self.assertEqual(len(self.b.nav_stack), 1, "should reset the stack")

    def test_unknown_command_is_declined(self):
        self.assertFalse(self.b.on_nav_command("nope"))

    def _play(self, audio):
        self.b.on_playstate({"stopped": False, "is_audio": audio,
                             "id": "v1", "title": "Something",
                             "position": 1, "duration": 10})

    def test_go_home_over_a_video_stops_it(self):
        """"Go home" cannot mean "go home behind this film". The navigate
        happens first so the home screen is loaded by the time stopping
        hands it the window."""
        self._play(audio=False)
        self.assertTrue(self.b.on_nav_command("home"))
        self.assertEqual(self.b.route["kind"], "home")
        self.assertIn("stop", [c[0] for c in self.ctl.transport])

    def test_go_home_over_music_stops_it_too(self):
        """Video and audio playstates are held in different attributes, so
        one check covers one of them and quietly misses the other."""
        self._play(audio=True)
        self.assertTrue(self.b.on_nav_command("home"))
        self.assertIn("stop", [c[0] for c in self.ctl.transport])

    def test_go_home_with_nothing_playing_stops_nothing(self):
        self.assertTrue(self.b.on_nav_command("home"))
        self.assertNotIn("stop", [c[0] for c in self.ctl.transport])

    def test_go_to_search_puts_the_cursor_in_the_search_box(self):
        """jellyfin-web's search button opens a search page; the box lives
        in our top bar on every screen, so this is the same gesture with
        one less screen. It is a renderer operation — only the renderer
        knows what has focus — so the assertion is on the request."""
        self.b.app = StubHudApp()
        self.assertTrue(self.b.on_nav_command("search"))
        self.assertEqual(_focus_calls(self.b.app), ["nav-search"])

    def test_search_is_declined_while_the_library_is_not_up(self):
        """Mid-playback the browser is not on screen; there is nothing to
        put a cursor into, and claiming the command would swallow it."""
        self.b.app = StubHudApp()
        self.b._browsing = False
        self.assertFalse(self.b.on_nav_command("search"))
        self.assertEqual(_focus_calls(self.b.app), [])


def _focus_calls(app):
    return [node for name, node in app.calls if name == "focus"]


class TestRemoteArrivalFocusesThePage(unittest.TestCase):
    """A page opened by remote or arrow key lands on its own default
    action, so the first press of anything is not a hunt for the Play
    button from wherever focus happened to be.

    Split across two sides on purpose. The browser only ever *asks* — it
    cannot know what has focus, or whether the pointer took over since —
    and the renderer decides (tests/lua/test_renderer.lua). What is
    checked here is that the ask happens and that a page nominates a node
    for it to land on."""

    def setUp(self):
        self.app = StubHudApp()
        self.b = MpvtkBrowser(app=self.app, source=FakeSource(),
                              controller=FakeController())
        self.b._pool = _SyncPool()

    def test_navigating_asks_for_the_page_default(self):
        self.app.calls.clear()
        self.b.navigate({"kind": "detail", "server": "srv1",
                         "item_id": "m1", "title": "A Movie"})
        self.assertIn(None, _focus_calls(self.app),
                      "no autofocus request went out on a navigation")

    def test_a_movie_page_nominates_its_play_button(self):
        self.b.navigate({"kind": "detail", "server": "srv1",
                         "item_id": "m1", "title": "A Movie"})
        nodes, _h = build_scene(self.b)
        af = [n["id"] for n in nodes if n.get("af")]
        self.assertEqual(len(af), 1, "a page needs exactly one default")
        self.assertIn(af[0], ("btn-play", "btn-resume"))

    def test_it_is_resume_when_there_is_something_to_resume(self):
        """Watching is why the page was opened; where from is the item's
        business, not the button's."""
        item = dict(self.b.source.get_item("srv1", "m1") or {})
        item["UserData"] = {"PlaybackPositionTicks": 600000000}
        self.b.source.get_item = lambda s, i, _it=item: _it
        self.b.navigate({"kind": "detail", "server": "srv1",
                         "item_id": "m1", "title": "A Movie"})
        nodes, _h = build_scene(self.b)
        af = [n["id"] for n in nodes if n.get("af")]
        self.assertEqual(af, ["btn-resume"])

    def test_a_missing_item_is_a_no_op(self):
        self.b.source.get_item = lambda s, i: None
        before = self.b.route["kind"]
        self.b.display_item("srv1", "nope")
        self.assertEqual(self.b.route["kind"], before)

class TestWatchedState(unittest.TestCase):
    """`(count or 0) == 0` reads a MISSING unplayed count as "nothing
    unplayed", i.e. fully watched — so a Series without UserData showed a
    watched tick and the toggle then marked an unwatched show unwatched."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_a_series_without_userdata_is_not_watched(self):
        self.assertFalse(components.is_watched({"Id": "s1", "Type": "Series"}))

    def test_a_series_with_no_unplayed_count_is_not_watched(self):
        self.assertFalse(components.is_watched(
            {"Id": "s1", "Type": "Series", "UserData": {}}))

    def test_zero_unplayed_is_watched(self):
        self.assertTrue(components.is_watched(
            {"Id": "s1", "Type": "Series",
             "UserData": {"UnplayedItemCount": 0}}))

    def test_remaining_episodes_are_not_watched(self):
        self.assertFalse(components.is_watched(
            {"Id": "s1", "Type": "Series",
             "UserData": {"UnplayedItemCount": 3}}))

    def test_played_flag_still_wins_for_movies(self):
        self.assertTrue(components.is_watched(
            {"Id": "m1", "Type": "Movie", "UserData": {"Played": True}}))

    def test_toggling_an_untouched_series_marks_it_watched(self):
        """The consequence of the bug: the first click was a no-op."""
        calls = []
        ctl = FakeController()
        ctl.set_watched = lambda srv, iid, w: calls.append(w) or True
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        b._act_watched({"Id": "s1", "Type": "Series"}, "srv1")
        self.assertEqual(calls, [True], "first click must mark it WATCHED")

    def test_a_failed_write_rolls_the_optimistic_flip_back(self):
        ctl = FakeController()
        ctl.set_watched = lambda srv, iid, w: False
        b = MpvtkBrowser(app=None, source=FakeSource(), controller=ctl)
        b._pool = _SyncPool()
        item = {"Id": "m1", "Type": "Movie", "UserData": {"Played": False}}
        b._act_watched(item, "srv1")
        self.assertFalse(item["UserData"]["Played"],
                         "UI kept a tick for a change that never happened")

class TestLiveTvActivation(unittest.TestCase):
    """Clicking a live tile.

    The two live types are activated differently, deliberately. A **channel**
    goes straight to playback: there is nothing to read about it and nothing
    to resume. A **program** opens its own page, because that is the only
    place Record and Record Series live — and it is one click from there to
    Watch. (It used to play the channel outright, which made recording
    unreachable from every screen that lists programs.)

    A program is still never played by its own id: what you watch is the
    channel carrying it, which is what ``channel_id`` on the route carries.
    """

    def setUp(self):
        self.ctl = FakeController()
        self.plays = []
        self.ctl.play_list = lambda ids, srv, i, **kw: self.plays.append(
            list(ids))
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_program_opens_its_page(self):
        self.b._open_item({"Id": "p1", "Name": "The News", "Type": "Program",
                           "ChannelId": "c1"})
        self.assertEqual(self.b.route.get("kind"), "program")
        self.assertEqual(self.b.route.get("item_id"), "p1")
        self.assertEqual(self.plays, [], "a program tile started playback")

    def test_the_program_route_carries_its_channel(self):
        # Watch has to tune the channel, not the programme.
        self.b._open_item({"Id": "p1", "Name": "The News", "Type": "Program",
                           "ChannelId": "c1"})
        self.assertEqual(self.b.route.get("channel_id"), "c1")

    def test_channel_opens_its_page(self):
        """It used to tune in on the click, which left no way to see what was
        on the channel later. jellyfin-web's channel card is a link too."""
        self.b._open_item({"Id": "c1", "Name": "BBC One", "Type": "TvChannel"})
        self.assertEqual(self.b.route.get("kind"), "channel")
        self.assertEqual(self.b.route.get("item_id"), "c1")
        self.assertEqual(self.plays, [], "a channel tile started playback")

    def test_a_live_tv_library_opens_the_live_tv_screen(self):
        # Not a grid of its children: browsing channels as a grid loses the
        # guide, the recordings and the schedule.
        self.b._open_item({"Id": "lt", "Name": "Live TV", "Type": "UserView",
                           "CollectionType": "livetv"})
        self.assertEqual(self.b.route.get("kind"), "livetv")

    def test_live_tiles_do_not_open_a_detail_page(self):
        self.b._open_item({"Id": "p1", "Name": "The News", "Type": "Program",
                           "ChannelId": "c1"})
        self.assertNotEqual(self.b.route.get("kind"), "detail")

class TestTileMenuGating(unittest.TestCase):
    """Every menu entry used to be offered for every item, so right-clicking
    a cast member offered to play, download and mark a Person watched."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def _actions(self, item):
        return [e[2] for e in self.b._tile_menu_entries(item)]

    def test_a_movie_gets_the_full_menu(self):
        acts = self._actions({"Id": "m1", "Type": "Movie"})
        for expected in ("play", "queue", "watched", "favorite", "addto",
                         "download"):
            self.assertIn(expected, acts)

    def test_a_person_gets_nothing_playable(self):
        acts = self._actions({"Id": "p1", "Type": "Person"})
        self.assertEqual(acts, [], "offered actions on a Person")

    def test_a_music_genre_is_not_downloadable_or_watchable(self):
        acts = self._actions({"Id": "g1", "Type": "MusicGenre"})
        self.assertNotIn("watched", acts)
        self.assertNotIn("download", acts)

    def test_a_music_genre_cannot_be_favorited(self):
        """A genre is not a library item — favoriting one posts a
        non-favoritable id that the server rejects. Tk excluded it."""
        self.assertNotIn("favorite",
                         self._actions({"Id": "g1", "Type": "MusicGenre"}))
        # ...but the types either side of it in MENU_PLAYABLE still can be,
        # so the exclusion is targeted rather than a blanket loss.
        for t in ("MusicAlbum", "MusicArtist"):
            self.assertIn("favorite", self._actions({"Id": "x", "Type": t}), t)

    def test_an_album_can_be_played_and_queued(self):
        acts = self._actions({"Id": "a1", "Type": "MusicAlbum"})
        self.assertIn("play", acts)
        self.assertIn("queue", acts)

    def test_editing_actions_hide_offline(self):
        self.b._offline = True
        acts = self._actions({"Id": "m1", "Type": "Movie"})
        self.assertNotIn("addto", acts)
        self.assertNotIn("download", acts)

    def test_an_empty_menu_renders_nothing(self):
        self.b._menu = {"item": {"Id": "p1", "Type": "Person"},
                        "server": "srv1", "x": 10, "y": 10}
        self.assertIsNone(self.b._tile_menu_node())

class TestMenuQueueAndPlay(unittest.TestCase):
    """Play/Add to Queue on a container must resolve it to its items — the
    container's own id isn't playable, which is why Play on an album tile
    used to just navigate."""

    def setUp(self):
        self.ctl = FakeController()
        self.queued = []
        self.ctl.queue_items = lambda srv, ids: self.queued.append(list(ids))
        self.src = FakeSource()
        self.src.get_album_tracks = lambda srv, aid: [
            {"Id": "t1"}, {"Id": "t2"}]
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()

    def test_queueing_an_album_queues_its_tracks(self):
        self.b._menu_queue({"Id": "a1", "Type": "MusicAlbum"}, "srv1")
        self.assertEqual(self.queued, [["t1", "t2"]])

    def test_queueing_a_movie_queues_the_movie(self):
        self.b._menu_queue({"Id": "m1", "Type": "Movie"}, "srv1")
        self.assertEqual(self.queued, [["m1"]])

    def test_playing_an_album_plays_its_tracks(self):
        played = []
        self.ctl.play_list = lambda ids, srv, i, **kw: played.append(list(ids))
        self.b._menu_play({"Id": "a1", "Type": "MusicAlbum"}, "srv1")
        self.assertEqual(played, [["t1", "t2"]], "navigated instead of playing")

    def test_an_unresolvable_container_falls_back_to_opening_it(self):
        self.src.get_album_tracks = lambda srv, aid: []
        self.b._menu_play({"Id": "a1", "Type": "MusicAlbum"}, "srv1")
        self.assertEqual(self.b.route["kind"], "album")

class TestYearFilter(unittest.TestCase):
    """The repository supported year filtering all along (online and
    offline); only the picker was missing."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b.server = "srv1"

    def _route(self, **kw):
        r = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
             "_items": [], "_total": 0,
             "_filtervals": {"genres": ["Drama"], "years": [2021, 1999]}}
        r.update(kw)
        self.b.nav_stack = [r]
        return r

    def _panel(self):
        """The year picker lives in the filter panel now."""
        _n, h = build_scene(self.b)
        h["grid-filter"]["click"]()
        return build_scene(self.b)

    def test_the_year_picker_lists_the_available_years(self):
        self._route()
        nodes, _h = self._panel()
        dd = next(n for n in nodes if n.get("id") == "flt-year")
        self.assertEqual(dd["items"][1:], ["2021", "1999"])

    def test_choosing_a_year_stores_it_as_an_int(self):
        route = self._route()
        _n, handlers = self._panel()
        handlers["flt-year"]["select"](1, "2021")
        self.assertEqual(route["_filters"]["year"], 2021)

    def test_all_years_clears_the_filter(self):
        route = self._route(_filters={"year": 2021})
        _n, handlers = self._panel()
        handlers["flt-year"]["select"](0, "All Years")
        self.assertIsNone(route["_filters"].get("year"))

    def test_the_current_year_is_preselected(self):
        self._route(_filters={"year": 1999})
        nodes, _h = self._panel()
        dd = next(n for n in nodes if n.get("id") == "flt-year")
        self.assertEqual(dd["sel"], 2)

class TestCollections(unittest.TestCase):
    """Collections were missing from mpvtk entirely: no Movies-library
    toggle, no add-to-collection, no remove-from-collection."""

    def setUp(self):
        self.src = FakeSource()
        self.calls = []
        self.src.get_movie_collections = lambda srv, **kw: (
            self.calls.append(("collections", kw))
            or ([{"Id": "c1", "Name": "Trilogy", "Type": "BoxSet"}], 1))
        self.src.get_library_items = lambda srv, parent, **kw: (
            self.calls.append(("library", kw))
            or ([{"Id": "m1", "Name": "A Movie", "Type": "Movie"}], 1))
        self.src.get_collections = lambda srv: [
            {"Id": "c1", "Name": "Trilogy"}]
        self.ctl = FakeController()
        self.added = []
        self.ctl.collection_add = lambda srv, cid, ids: self.added.append(
            (cid, list(ids)))
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def _movies_grid(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies",
                         "collection_type": "movies"})
        return self.b.route

    def test_a_movies_library_offers_the_toggle(self):
        self._movies_grid()
        nodes, _h = build_scene(self.b)
        self.assertIn("grid-collections", ids(nodes))

    def test_a_music_library_does_not(self):
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib2", "title": "Music",
                         "collection_type": "music"})
        nodes, _h = build_scene(self.b)
        self.assertNotIn("grid-collections", ids(nodes))

    def test_toggling_queries_collections_instead(self):
        route = self._movies_grid()
        self.calls.clear()
        self.b._toggle_collections(route)
        kinds = [c[0] for c in self.calls]
        self.assertIn("collections", kinds)
        self.assertNotIn("library", kinds, "still queried the library")
        self.assertEqual([i["Id"] for i in route["_items"]], ["c1"])

    def test_toggling_back_returns_to_the_library(self):
        route = self._movies_grid()
        self.b._toggle_collections(route)
        self.calls.clear()
        self.b._toggle_collections(route)
        self.assertIn("library", [c[0] for c in self.calls])

    def test_collections_are_a_separate_window(self):
        """Two long lists stacked in one dialog was the crowding."""
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        nodes, h = build_scene(self.b)
        self.assertNotIn("add-col-0", ids(nodes), "still stacked inline")
        self.assertIn("add-collections", h, "no way through to collections")
        h["add-collections"]["click"]()
        nodes, _h = build_scene(self.b)
        self.assertIn("add-col-0", ids(nodes))
        self.assertIn("Trilogy",
                      [n.get("text") for n in nodes if n.get("text")])

    def test_adding_to_a_collection_calls_the_api(self):
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        _n, h = build_scene(self.b)
        h["add-collections"]["click"]()
        _n, h = build_scene(self.b)
        h["add-col-0"]["click"]()
        self.assertEqual(self.added, [("c1", ["m1"])])

    def test_back_returns_to_the_playlist_dialog(self):
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        _n, h = build_scene(self.b)
        h["add-collections"]["click"]()
        _n, h = build_scene(self.b)
        h["addcol-back"]["click"]()
        nodes, _h = build_scene(self.b)
        self.assertIn("add-newname", ids(nodes))

    def test_a_source_without_collections_offers_no_way_in(self):
        """The offline catalog has no collections; the dialog must not
        break, it just doesn't offer that button."""
        del self.src.get_collections
        self.b._open_add_to({"Id": "m1", "Type": "Movie"})
        nodes, h = build_scene(self.b)
        self.assertNotIn("add-collections", h)
        self.assertIn("add-newname", ids(nodes))

class TestRightClickCrash(unittest.TestCase):
    """Right-clicking an item with no applicable menu entries (a cast
    member) built a None menu node, appended it to the scene tree, and
    took down the whole browser render loop."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())
        self.b._pool = _SyncPool()

    def test_right_clicking_a_person_does_not_crash(self):
        self.b._open_tile_menu({"Id": "p1", "Type": "Person"}, 10, 10)
        build_scene(self.b)          # would raise before the fix

    def test_no_menu_opens_for_a_person(self):
        self.b._open_tile_menu({"Id": "p1", "Type": "Person"}, 10, 10)
        self.assertIsNone(self.b._menu, "opened an empty menu")

    def test_a_movie_still_opens_its_menu(self):
        self.b._open_tile_menu({"Id": "m1", "Type": "Movie"}, 10, 10)
        self.assertIsNotNone(self.b._menu)
        nodes, _h = build_scene(self.b)
        self.assertIn("tilemenu", ids(nodes))

    def test_a_stale_empty_menu_still_renders(self):
        """Belt and braces: even if _menu is set to something with no
        entries by another path, the build must survive."""
        self.b._menu = {"item": {"Id": "p1", "Type": "Person"},
                        "server": "srv1", "x": 5, "y": 5}
        build_scene(self.b)

class TestMediaInfoLine(unittest.TestCase):
    """The detail page showed only video title/range/container — size,
    bitrate, audio codec and "Ends at" were all dropped."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def _item(self, **src):
        base = {"Id": "ms", "Container": "mkv", "Size": 8 * 1024 ** 3,
                "Bitrate": 12000000, "MediaStreams": [
                    {"Type": "Video", "DisplayTitle": "1080p HEVC",
                     "VideoRange": "HDR", "VideoRangeType": "HDR10"},
                    {"Type": "Audio", "Codec": "eac3",
                     "ChannelLayout": "5.1"}]}
        base.update(src)
        return {"Id": "m1", "Type": "Movie", "MediaSources": [base],
                "RunTimeTicks": 72000000000}

    def test_it_includes_audio_size_and_bitrate(self):
        line = detail_page(self.b, {})._media_info_line(self._item())
        self.assertIn("EAC3 5.1", line)
        self.assertIn("GB", line)
        self.assertIn("Mbps", line)

    def test_video_range_type_wins_over_range(self):
        """VideoRange only says "HDR"; VideoRangeType says which."""
        self.assertIn("HDR10", detail_page(self.b, {})._media_info_line(self._item()))

    def test_it_shows_when_playback_would_end(self):
        self.assertIn("Ends at", detail_page(self.b, {})._media_info_line(self._item()))

    def test_sdr_is_not_called_out(self):
        item = self._item()
        item["MediaSources"][0]["MediaStreams"][0] = {
            "Type": "Video", "DisplayTitle": "1080p", "VideoRange": "SDR",
            "VideoRangeType": "SDR"}
        self.assertNotIn("SDR", detail_page(self.b, {})._media_info_line(item))

    def test_a_sourceless_item_is_empty_not_broken(self):
        self.assertEqual(
            detail_page(self.b, {})._media_info_line({"Id": "m1", "Type": "Movie"}), "")

class TestScenesRow(unittest.TestCase):
    """Chapter navigation. The row builder existed but nothing called it,
    and the whole suite stayed green — asserting the builder in isolation
    proves nothing; the assertion has to be that it REACHES the page."""

    def setUp(self):
        self.ctl = FakeController()
        self.played = []
        self.ctl.play = lambda item, srv, **kw: self.played.append(kw)
        self.src = FakeSource()
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"
        self.item = {
            "Id": "m1", "Name": "Movie", "Type": "Movie",
            "MediaSources": [{"Id": "src1", "MediaStreams": []}],
            "Chapters": [
                {"Name": "Opening", "StartPositionTicks": 0},
                {"Name": "The Middle", "StartPositionTicks": 6000000000},
                {"Name": "The End", "StartPositionTicks": 12000000000},
            ]}
        self.b.nav_stack = [{"kind": "detail", "server": "srv1",
                             "item_id": "m1", "title": "Movie",
                             "_data": {"item": self.item, "similar": []}}]

    def test_the_scenes_row_is_on_the_detail_page(self):
        nodes, _h = build_scene(self.b)
        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("Scenes", texts, "chapter row never reached the page")

    def test_a_chapter_click_seeks_to_its_start(self):
        row = detail_page(self.b, self.b.route)._scenes_row(self.item, "srv1")
        self.assertIsNotNone(row)
        nodes, handlers = layout(row, 1280, 720)
        rects = [n for n in nodes
                 if n["t"] == "rect" and "detail-scenes" in str(n.get("id"))]
        self.assertTrue(rects, "no clickable chapter regions")
        handlers[rects[1]["id"]]["click"]()
        self.assertEqual(self.played[0].get("offset_ticks"), 6000000000)

    def test_a_chapter_carries_the_selected_tracks(self):
        """Starting at a chapter must use the same version/tracks the Play
        button would, not the server's defaults."""
        self.b.route["_srcid"] = "src1"
        self.b.route["_aid"] = 3
        self.b.route["_sid"] = 4
        row = detail_page(self.b, self.b.route)._scenes_row(self.item, "srv1")
        nodes, handlers = layout(row, 1280, 720)
        rects = [n for n in nodes
                 if n["t"] == "rect" and "detail-scenes" in str(n.get("id"))]
        handlers[rects[0]["id"]]["click"]()
        self.assertEqual(
            (self.played[0].get("srcid"), self.played[0].get("aid"),
             self.played[0].get("sid")), ("src1", 3, 4))

    def test_a_single_chapter_is_not_a_row(self):
        self.item["Chapters"] = [{"Name": "All", "StartPositionTicks": 0}]
        self.assertIsNone(detail_page(self.b, self.b.route)._scenes_row(self.item, "srv1"))
        nodes, _h = build_scene(self.b)
        self.assertNotIn("Scenes",
                         [n.get("text") for n in nodes if n.get("text")])

    def test_no_chapters_is_not_a_row(self):
        self.item.pop("Chapters")
        self.assertIsNone(detail_page(self.b, self.b.route)._scenes_row(self.item, "srv1"))

class TestNextUp(unittest.TestCase):
    """Next Up was a dead button on a series nobody had started, and
    restarted a part-watched episode from zero."""

    def setUp(self):
        self.ctl = FakeController()
        self.played = []
        self.ctl.play_list = lambda ids, srv, i, **kw: self.played.append(
            (list(ids), kw.get("offset_ticks")))
        self.ctl.play = lambda item, srv, **kw: self.played.append(
            ([item.get("Id")], kw.get("offset_ticks")))
        self.src = FakeSource()
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def _ep(self, **kw):
        base = {"Id": "e1", "Type": "Episode", "Name": "Pilot"}
        base.update(kw)
        return base

    def test_it_resumes_a_part_watched_episode(self):
        self.src.get_next_up = lambda srv, sid: self._ep(
            UserData={"PlaybackPositionTicks": 55000000})
        self.b._play_next_up("sh1", "srv1")
        self.assertEqual(self.played[0][1], 55000000, "restarted from zero")

    def test_an_unwatched_episode_starts_at_the_beginning(self):
        self.src.get_next_up = lambda srv, sid: self._ep()
        self.b._play_next_up("sh1", "srv1")
        self.assertIsNone(self.played[0][1])

    def test_a_series_with_no_next_up_starts_at_episode_one(self):
        """An unstarted series returns no NextUp; the button did nothing."""
        self.src.get_next_up = lambda srv, sid: None
        self.src.get_series_queue = lambda srv, sid, **kw: [self._ep(Id="s1e1")]
        self.b._play_next_up("sh1", "srv1")
        self.assertTrue(self.played, "Next Up did nothing")
        self.assertIn("s1e1", self.played[0][0])

    def test_a_genuinely_empty_series_is_a_no_op(self):
        self.src.get_next_up = lambda srv, sid: None
        self.src.get_series_queue = lambda srv, sid, **kw: []
        self.b._play_next_up("sh1", "srv1")
        self.assertEqual(self.played, [])

class TestSeasonTitles(unittest.TestCase):
    def _title(self, **row):
        from jellyfin_mpv_shim.mpvtk_browser.downloads import (
            season_title)

        return season_title(row)

    def test_season_zero_is_specials(self):
        self.assertEqual(self._title(parent_index=0), "Specials")

    def test_a_normal_season(self):
        self.assertEqual(self._title(parent_index=2), "Season 2")

    def test_the_stored_name_wins(self):
        self.assertEqual(
            self._title(parent_index=1,
                        item_json='{"SeasonName": "Book One"}'),
            "Book One")

    def test_no_index_is_episodes(self):
        self.assertEqual(self._title(), "Episodes")

    def test_bad_json_falls_back(self):
        self.assertEqual(self._title(parent_index=3, item_json="{bad"),
                         "Season 3")

class TestSeriesExtras(unittest.TestCase):
    def setUp(self):
        self.ctl = FakeController()
        self.played = []
        self.ctl.play_list = lambda ids, s, i, **kw: self.played.append(
            sorted(ids))
        self.src = FakeSource()
        self.src.get_similar = lambda srv, iid, **kw: [
            {"Id": "s2", "Name": "Other", "Type": "Series"}]
        self.src.get_series_queue = lambda srv, sid, **kw: [
            {"Id": "e1"}, {"Id": "e2"}, {"Id": "e3"}]
        self.b = MpvtkBrowser(app=None, source=self.src, controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"

    def test_shuffle_plays_the_whole_show(self):
        self.b._shuffle_series("sh1", "srv1")
        self.assertEqual(self.played, [["e1", "e2", "e3"]])

    def test_more_like_this_reaches_the_series_page(self):
        self.b.nav_stack = [{"kind": "series", "server": "srv1",
                             "item_id": "sh1", "title": "Show"}]
        self.b._load_route(self.b.route)
        nodes, _h = build_scene(self.b)
        self.assertIn("More Like This",
                      [n.get("text") for n in nodes if n.get("text")])

    def test_the_shuffle_button_is_on_the_page(self):
        row = series_page(self.b)._series_actions({"Id": "sh1", "Type": "Series"},
                                     "srv1", "sh1")
        nodes, _h = layout(row, 1280, 720)
        self.assertIn("sa-shuffle", ids(nodes))

class TestVersionPickerDedups(unittest.TestCase):
    """Two sources with the same Name gave two indistinguishable dropdown
    rows — you could not tell which one you were picking."""

    def _names(self, source_names):
        from jellyfin_mpv_shim.mpvtk.widgets import Column
        b = MpvtkBrowser(app=None, source=FakeSource())
        item = {"Id": "m1", "MediaSources": [
            {"Id": "s%d" % i, "Name": n, "MediaStreams": []}
            for i, n in enumerate(source_names)]}
        controls = detail_page(b, {"kind": "detail"})._track_pickers(item)
        for n in layout(Column(list(controls)), 1280, 720)[0]:
            if n.get("id") == "dt-version":
                return n.get("items")
        return None

    def test_duplicate_names_are_distinguished(self):
        self.assertEqual(self._names(["Bluray", "Bluray"]),
                         ["Bluray", "Bluray (2)"])

    def test_distinct_names_are_untouched(self):
        self.assertEqual(self._names(["Bluray", "Web"]), ["Bluray", "Web"])

    def test_an_unnamed_source_still_gets_a_number(self):
        self.assertEqual(self._names([None, None]),
                         ["Version 1", "Version 2"])

class TestMediaInfoKeepsTheCodec(unittest.TestCase):
    """Without a DisplayTitle the line collapsed to "1080p", dropping the
    one thing that decides whether it will direct-play."""

    def _line(self, video):
        b = MpvtkBrowser(app=None, source=FakeSource())
        item = {"MediaSources": [{"Id": "s1", "MediaStreams": [
            dict(video, Type="Video")]}]}
        return detail_page(b, {"kind": "detail"})._media_info_line(item)

    def test_codec_and_resolution_when_the_server_gives_no_title(self):
        line = self._line({"Codec": "hevc", "Width": 1920, "Height": 1080})
        self.assertIn("HEVC", line)
        self.assertIn("1920x1080", line)

    def test_height_alone_still_works(self):
        self.assertIn("1080p", self._line({"Codec": "h264", "Height": 1080}))

    def test_a_display_title_still_wins(self):
        line = self._line({"DisplayTitle": "4K HEVC", "Height": 2160})
        self.assertIn("4K HEVC", line)
        self.assertNotIn("2160p", line)

class TestNoDeadButtons(unittest.TestCase):
    """Controls that rendered regardless of whether they could do anything."""

    def _browser(self, src=None):
        b = MpvtkBrowser(app=None, source=src or FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        return b

    def test_an_empty_playlist_offers_no_play_all(self):
        src = FakeSource()
        src.get_playlist_items = lambda srv, pid: []
        b = self._browser(src)
        b.navigate({"kind": "playlist", "server": "srv1", "item_id": "P",
                    "title": "Mix"})
        nodes, _h = build_scene(b)
        present = ids(nodes)
        self.assertNotIn("pl-play", present, "Play All on an empty playlist")
        self.assertNotIn("pl-shuffle", present)

    def test_a_playlist_with_tracks_still_offers_them(self):
        b = self._browser()
        b.navigate({"kind": "playlist", "server": "srv1", "item_id": "P",
                    "title": "Mix"})
        present = ids(build_scene(b)[0])
        self.assertIn("pl-play", present)
        self.assertIn("pl-shuffle", present)

    def _playlist_tiles(self, items):
        """The tile geometry a playlist of ``items`` is drawn at."""
        src = FakeSource()
        src.get_playlist_items = lambda srv, pid: list(items)
        b = self._browser(src)
        b.navigate({"kind": "playlist", "server": "srv1", "item_id": "P",
                    "title": "Mix"})
        nodes, _h = build_scene(b)
        return b, [n for n in nodes
                   if str(n.get("id") or "").startswith("pl-")
                   and n.get("t") == "rect" and n.get("ring")]

    @staticmethod
    def _video(i, ratio):
        return {"Id": "v%d" % i, "Name": "Item %d" % i, "Type": "Episode",
                "PrimaryImageAspectRatio": ratio,
                "ImageTags": {"Primary": "t%d" % i}}

    def test_a_playlist_of_stills_is_drawn_as_stills(self):
        """A playlist can hold anything, and this was the one grid in the app
        whose shape did not follow its artwork: 16:9 episode stills were
        drawn in 2:3 poster tiles, so the fill crop threw most of each
        picture away. jellyfin-web's cardBuilder shapes a row from the
        median aspect ratio; so does every other grid here."""
        b, tiles = self._playlist_tiles([self._video(i, 16 / 9)
                                         for i in range(8)])
        self.assertTrue(tiles, "no playlist tiles were drawn")
        wide = b.tiles.art.geom_wide
        # strip_h, not tile_h: a hit region spans the caption under the tile.
        self.assertAlmostEqual(tiles[0]["w"], wide.tile_w, delta=1)
        self.assertAlmostEqual(tiles[0]["h"], wide.strip_h, delta=1)

    def test_a_playlist_of_posters_still_gets_posters(self):
        """The same rule reaching the other answer — this must not become
        "landscape always"."""
        b, tiles = self._playlist_tiles([self._video(i, 2 / 3)
                                         for i in range(8)])
        self.assertTrue(tiles)
        poster = b.tiles.art.geom
        self.assertAlmostEqual(tiles[0]["w"], poster.tile_w, delta=1)
        self.assertAlmostEqual(tiles[0]["h"], poster.strip_h, delta=1)

    def test_a_playlist_entry_shows_its_own_art_not_the_series(self):
        """Same exception the season listing takes. Once the row is shaped
        for stills, the Thumb chain will borrow the SERIES' thumb or backdrop
        for an episode with no still of its own — so a playlist built out of
        one show drew the same series artwork in every cell and stopped
        distinguishing anything. Borrowing is right for a Continue Watching
        card, which is a pointer back to the show; a playlist entry is the
        thing you are about to play."""
        asked = []
        src = FakeSource()
        real = src.image_spec

        def image_spec(item, image_type="Primary", width=280, inherit=True):
            asked.append(inherit)
            return real(item, image_type, width, inherit=inherit)

        src.image_spec = image_spec
        src.get_playlist_items = lambda srv, pid: [
            self._video(i, 16 / 9) for i in range(6)]
        b = self._browser(src)
        b.navigate({"kind": "playlist", "server": "srv1", "item_id": "P",
                    "title": "Mix"})
        build_scene(b)
        self.assertTrue(asked, "no artwork was resolved for the tiles")
        self.assertNotIn(True, asked,
                         "a playlist tile inherited its parent's artwork")

    def test_artwork_with_no_ratio_keeps_the_poster_default(self):
        """Nothing to measure: the shape it has always had, not a guess."""
        b, tiles = self._playlist_tiles(
            [{"Id": "v%d" % i, "Name": "N", "Type": "Video"}
             for i in range(4)])
        self.assertTrue(tiles)
        self.assertAlmostEqual(tiles[0]["w"], b.tiles.art.geom.tile_w,
                               delta=1)

    def test_the_artist_bar_drops_play_when_the_songs_failed_to_load(self):
        b = self._browser()
        bar = music_page(b, {"kind": "artist", "item_id": "art1"}) \
            .action_bar("srv1", [], "art1", "art")
        present = ids(layout(bar, 1280, 720)[0])
        for dead in ("art-play", "art-shuffle", "art-queue"):
            self.assertNotIn(dead, present, "%s is a dead click" % dead)
        self.assertIn("art-mix", present,
                      "Instant Mix seeds from the container, not the tracks")

    def test_play_all_on_an_album_carries_the_dtos_so_it_can_resume(self):
        """_play_list needs the DTOs for the resume offset; without them a
        half-played track restarts from zero."""
        b = self._browser()
        got = {}
        # ItemActions.play_list: the page calls its own service.
        b._actions.play_list = lambda ids_, srv, i, **kw: got.update(kw)
        b.navigate({"kind": "album", "server": "srv1", "item_id": "al1",
                    "title": "Album"})
        _n, handlers = build_scene(b)
        handlers["album-play"]["click"]()
        self.assertIsNotNone(got.get("items"), "no DTOs passed: %r" % got)

    def test_a_music_genre_is_not_offered_as_a_favorite(self):
        """A genre is not a library item — favoriting one posts an id the
        server rejects."""
        b = self._browser()
        labels = [e[0] for e in b._tile_menu_entries(
            {"Id": "g1", "Type": "MusicGenre", "Name": "Rock"})]
        self.assertNotIn("Add to Favorites", labels)

    def test_an_album_is_still_favoritable(self):
        b = self._browser()
        labels = [e[0] for e in b._tile_menu_entries(
            {"Id": "al1", "Type": "MusicAlbum", "Name": "Album"})]
        self.assertIn("Add to Favorites", labels)

class TestSeasonPageNextUp(unittest.TestCase):
    """Tk had Play Next Up on the season page. Landing on a season and being
    able to carry on is the point of the screen; without it you had to go up
    to the series page to resume."""

    def _season(self, series_id="sh1"):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "season", "server": "srv1", "item_id": "sea1",
                 "title": "Season 1"}
        if series_id:
            route["series_id"] = series_id
        b.nav_stack = [route]
        b._load_route(route)
        return b, route

    def test_the_button_is_on_the_season_page(self):
        b, _r = self._season()
        nodes, handlers = build_scene(b)
        self.assertIn("se-nextup", ids(nodes), "no Next Up on the season page")
        self.assertIn("se-nextup", handlers)

    def test_it_plays_the_next_episode_of_the_series(self):
        b, _r = self._season()
        played = []
        # ItemActions.play, not the shell forwarder: Next Up is an action and
        # now calls its own service rather than bouncing off the shell.
        b._actions.play = lambda item, server, **kw: played.append(
            item.get("Id"))
        _n, handlers = build_scene(b)
        handlers["se-nextup"]["click"]()
        self.assertTrue(played, "Next Up played nothing")

    def test_a_season_with_no_series_id_does_not_offer_it(self):
        """Nothing to resume against."""
        b, _r = self._season(series_id=None)
        nodes, _h = build_scene(b)
        self.assertNotIn("se-nextup", ids(nodes))

class TestTileAndMetaParity(unittest.TestCase):
    """Small captions that carry most of the information on a tile."""

    def setUp(self):
        self.b = MpvtkBrowser(app=None, source=FakeSource())

    def test_an_episode_tile_names_its_show(self):
        """A bare "S1E1" on a Continue Watching tile does not say which show
        it belongs to, which is the one thing you need there."""
        self.assertEqual(
            components.episode_subtitle({"Type": "Episode", "SeriesName": "The Show",
                              "ParentIndexNumber": 1, "IndexNumber": 2}),
            "The Show · S1E2")

    def test_an_episode_with_no_numbering_still_names_the_show(self):
        self.assertEqual(
            components.episode_subtitle({"Type": "Episode", "SeriesName": "The Show"}),
            "The Show")

    def test_an_episode_with_no_series_name_still_shows_the_number(self):
        self.assertEqual(
            components.episode_subtitle({"Type": "Episode", "ParentIndexNumber": 1,
                              "IndexNumber": 2}), "S1E2")

    def test_a_latest_tv_episode_leads_with_its_series(self):
        """Recently Added for a TV library is read as a list of shows, so the
        episode name belongs on the second line."""
        self.assertEqual(
            components.tile_lines({"Type": "Episode", "Name": "Pilot",
                                   "SeriesName": "The Show",
                                   "ParentIndexNumber": 1, "IndexNumber": 1},
                                  parent_item=True),
            ("The Show", "Pilot"))

    def test_a_series_in_that_row_is_untouched(self):
        self.assertEqual(
            components.tile_lines({"Type": "Series", "Name": "The Show",
                                   "ProductionYear": 2001},
                                  parent_item=True),
            ("The Show", "2001"))

    def test_an_episode_with_no_series_name_keeps_its_own_title(self):
        # Falling through to the series name would have blanked the tile.
        self.assertEqual(
            components.tile_lines({"Type": "Episode", "Name": "Pilot",
                                   "ParentIndexNumber": 1, "IndexNumber": 1},
                                  parent_item=True),
            ("Pilot", "S1E1"))

    def test_elsewhere_an_episode_tile_is_unchanged(self):
        self.assertEqual(
            components.tile_lines({"Type": "Episode", "Name": "Pilot",
                                   "SeriesName": "The Show",
                                   "ParentIndexNumber": 1, "IndexNumber": 1}),
            ("Pilot", "The Show · S1E1"))

    def test_a_movie_tile_is_unchanged(self):
        self.assertEqual(
            components.episode_subtitle({"Type": "Movie", "ProductionYear": 2001}),
            "2001")

    def test_a_crew_member_is_captioned_with_their_job(self):
        """Crew have no Role — their job IS the Type — so `Role or ""`
        captioned every Director and Writer blank."""
        # Tile captions are baked into the strip bitmap, so catch them at
        # the boundary where _people_row hands its tiles over.
        seen = []
        self.b.tiles.tile_row = lambda title, items, rid, **kw: seen.extend(items)
        self.b._people_row(
            [{"Id": "p1", "Name": "A Director", "Type": "Director"},
             {"Id": "p2", "Name": "An Actor", "Type": "Actor",
              "Role": "Some Character"}], "srv1")
        self.assertEqual([p["_subtitle"] for p in seen],
                         ["Director", "Some Character"])

    def test_the_people_row_does_not_mutate_the_source_dtos(self):
        """These DTOs are shared with whatever else holds the item."""
        people = [{"Id": "p1", "Name": "A Director", "Type": "Director"}]
        self.b._tile_row = lambda *a, **kw: None
        self.b._people_row(people, "srv1")
        self.assertEqual(people[0]["Type"], "Director")

    def test_the_metadata_line_lists_genres(self):
        line = self.b._meta_line({"ProductionYear": 2001,
                                  "Genres": ["Drama", "Comedy"]})
        self.assertIn("Drama, Comedy", line)

    def test_no_genres_leaves_no_empty_separator(self):
        self.assertEqual(self.b._meta_line({"ProductionYear": 2001}), "2001")

class TestSeriesAddTo(unittest.TestCase):
    def test_a_series_can_be_added(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        acts = [e[2] for e in b._tile_menu_entries(
            {"Id": "sh1", "Type": "Series"})]
        self.assertIn("addto", acts)

    def test_a_season_can_be_added(self):
        b = MpvtkBrowser(app=None, source=FakeSource())
        acts = [e[2] for e in b._tile_menu_entries(
            {"Id": "s1", "Type": "Season"})]
        self.assertIn("addto", acts)

class TestShippedThemesRender(unittest.TestCase):
    """Every shipped theme, checked at the layer that decides what you see.

    test_themes.py checks that each palette's colours work against each other.
    That is necessary and not sufficient: mpvtk's widgets carry a hardcoded
    dark palette as their DEFAULTS (``Text(color="eeeeee")``,
    ``Button(bg="333333")``, and so on), and 138 of the browser's 242 widget
    constructions take them rather than passing a theme colour. So a palette
    can be perfectly balanced on paper and still render near-white text,
    because most of the tree never asked the theme anything.

    That is invisible in a dark theme — the hardcoded defaults ARE a dark
    palette — and total in a light one. This builds a real scene per theme and
    reads the colours back off the nodes, which is the only way to see it.
    """

    def _luminance(self, colour):
        def channel(v):
            v /= 255.0
            return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

        r, g, b = theme.rgb(colour)
        return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)

    def _contrast(self, a, b):
        la, lb = self._luminance(a), self._luminance(b)
        return (max(la, lb) + 0.05) / (min(la, lb) + 0.05)

    def test_no_shipped_theme_renders_text_it_cannot_show(self):
        from jellyfin_mpv_shim.mpvtk_browser import themes

        try:
            for _label, theme_id in themes.choices(force=True):
                # Construct FIRST: MpvtkBrowser.__init__ applies the theme
                # named in settings, so applying before it would be undone.
                # Then push to the toolkit, which is what production does and
                # what makes the widget defaults follow the palette at all.
                b = MpvtkBrowser(app=None, source=FakeSource())
                b._pool = _SyncPool()
                cfg = theme.apply(theme_id)
                theme.apply_to_toolkit(glow=cfg.get("glow", False))
                window_bg = cfg["palette"]["WINDOW_BG"]
                b.route["_data"] = {"libraries": b.source.libraries,
                                    "rows": []}
                nodes, _h = build_scene(b)
                texts = [n for n in nodes
                         if n.get("t") == "text" and n.get("c")]
                self.assertTrue(texts, "no text drawn at all?")
                for node in texts:
                    with self.subTest(theme=theme_id, text=node.get("text")):
                        self.assertGreaterEqual(
                            self._contrast(node["c"], window_bg), 3.0,
                            "%r drawn %s on %s" % (node.get("text"),
                                                   node["c"], window_bg))
        finally:
            theme.apply("default")
            theme.apply_to_toolkit(glow=False)


class TestThemeGradients(unittest.TestCase):
    """A theme's background gradients, in a real scene.

    The gradient primitive is verified against mpv by
    tools/gradient_fidelity.py; what this checks is the wiring — that a
    theme key actually reaches the window background and the top bar, and
    that a theme without one still gets a flat fill rather than an empty
    Stack."""

    def _scene(self, theme_id):
        b = MpvtkBrowser(app=None, source=FakeSource())
        b._pool = _SyncPool()
        cfg = theme.apply(theme_id)
        theme.apply_to_toolkit(glow=cfg.get("glow", False))
        b._theme_cfg = cfg
        b.route["_data"] = {"libraries": b.source.libraries, "rows": []}
        nodes, _h = build_scene(b)
        return nodes

    def tearDown(self):
        theme.apply("default")
        theme.apply_to_toolkit(glow=False)

    def test_a_theme_without_gradients_draws_none(self):
        nodes = self._scene("default")
        self.assertEqual([n for n in nodes if n.get("t") == "grad"], [])

    def test_a_window_gradient_spans_the_window_behind_everything(self):
        nodes = self._scene("jf-wmc")
        grads = [n for n in nodes if n.get("t") == "grad"]
        self.assertEqual(len(grads), 1)
        g = grads[0]
        self.assertEqual(g["axis"], "y")
        self.assertEqual((g["w"], g["h"]), (1280.0, 720.0))
        # Bottom of the paint order, or it would cover the UI.
        self.assertEqual(nodes.index(g), 0)

    def test_a_topbar_gradient_covers_the_bar_only(self):
        nodes = self._scene("jf-purplehaze")
        grads = [n for n in nodes if n.get("t") == "grad"]
        self.assertEqual(len(grads), 1)
        g = grads[0]
        self.assertEqual(g["axis"], "x")
        self.assertEqual(g["h"], 60)
        self.assertEqual(g["w"], 1280.0)

    def test_the_bar_drops_its_flat_fill_when_a_gradient_is_behind_it(self):
        """Otherwise the fill simply covers the gradient up."""
        flat = [n for n in self._scene("default")
                if n.get("t") == "rect" and n.get("h") == 60]
        self.assertTrue(flat, "the stock bar should have a fill")
        gradient_theme = [n for n in self._scene("jf-purplehaze")
                          if n.get("t") == "rect" and n.get("h") == 60
                          and n.get("fill")]
        self.assertEqual(gradient_theme, [])


class TestSortModes(unittest.TestCase):
    def test_critic_and_parental_rating_are_offered(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import SORTS

        labels = [s[0] for s in SORTS]
        self.assertIn("Critic Rating", labels)
        self.assertIn("Parental Rating", labels)


if __name__ == "__main__":
    unittest.main()


class TestPlayNext(unittest.TestCase):
    """"Play Next" on a tile: queue it after the current item, not last.

    The player has always been able to do this — `PlayNext` from a remote
    lands in `event_handler` and goes through `Media.insert_items` with
    `append=False` — so the gap was only the menu entry. jellyfin-web has
    both (`queue` and `queuenext`); we had only the append one.
    """

    def setUp(self):
        self.ctl = FakeController()
        self.b = MpvtkBrowser(app=None, source=FakeSource(),
                              controller=self.ctl)
        self.b._pool = _SyncPool()
        self.b.server = "srv1"
        self.b.navigate({"kind": "grid", "server": "srv1",
                         "parent_id": "lib1", "title": "Movies",
                         "collection_type": "movies"})

    def playing(self, yes=True):
        self.b._now_playing = {"title": "Something"} if yes else None

    def menu_labels(self, item=None):
        item = item or {"Id": "g0", "Name": "Item 0", "Type": "Movie"}
        return [label for label, _icon, _act
                in self.b._tile_menu_entries(item)]

    def choose(self, label, item=None):
        """Open the tile menu and pick `label`, as a click would."""
        item = item or {"Id": "g0", "Name": "Item 0", "Type": "Movie"}
        entries = self.b._tile_menu_entries(item)
        index = [l for l, _i, _a in entries].index(label)
        self.b._menu = {"item": item, "server": "srv1", "x": 0, "y": 0}
        self.b._menu_action(index, label)

    def test_the_entry_is_offered_while_something_plays(self):
        self.playing()
        labels = self.menu_labels()
        self.assertIn("Play Next", labels)
        self.assertIn("Add to Queue", labels)
        # Web's order, and the sensible one: the existing entry keeps its
        # place and the new one follows it.
        self.assertLess(labels.index("Add to Queue"), labels.index("Play Next"))

    def test_it_is_hidden_when_nothing_is_playing(self):
        """With an idle player both entries mean "start these items", and
        offering one action twice under two names is worse than offering it
        once."""
        self.playing(False)
        labels = self.menu_labels()
        self.assertIn("Add to Queue", labels)
        self.assertNotIn("Play Next", labels)

    def test_choosing_it_queues_next_not_last(self):
        self.playing()
        self.choose("Play Next")
        called = [name for name, _args in self.ctl.transport]
        self.assertIn("queue_next_items", called)
        self.assertNotIn("queue_items", called)

    def test_add_to_queue_still_appends(self):
        """The guard on the above: both entries must not collapse onto one
        call, which is the way a copy-paste of this would fail."""
        self.playing()
        self.choose("Add to Queue")
        called = [name for name, _args in self.ctl.transport]
        self.assertIn("queue_items", called)
        self.assertNotIn("queue_next_items", called)


class TestFilterGating(unittest.TestCase):
    """A filter is offered only where it can match something.

    Transcribed from jellyfin-web's ``FilterButton.tsx``, which gates by
    ``viewType``: ``isFiltersFeaturesEnabled`` (Movies/Series/Episodes),
    ``isFiltersLanguagesEnabled`` (those plus Mixed) and
    ``getVisibleFiltersStatus`` (no play state for Albums/Artists/Songs).

    Before this, every library type drew an identical panel -- a books
    library offered "Has Theme Song" and a music library "Has Subtitles".
    Neither is merely useless: a filter that cannot match returns an
    empty grid, which reads as a broken library rather than as a filter
    that never applied.
    """

    #: Everything a fully-stocked server answers with, so the "did the
    #: server offer options" gate never hides a picker this is asking
    #: about. Those two gates are independent and both have to be off
    #: for a section to be missing for the reason under test.
    VALS = {"genres": ["Action"], "years": [2020],
            "official_ratings": ["PG-13"], "tags": ["Heist"],
            "audio_languages": [("English (eng)", "eng")],
            "subtitle_languages": [("English (eng)", "eng")]}

    def _panel_keys(self, collection_type):
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": collection_type, "title": "Lib"})
        b.route["_filtervals"] = dict(self.VALS)
        _n, handlers = build_scene(b, size=(1280, 720))
        handlers["grid-filter"]["click"]()
        nodes, _h = build_scene(b)
        return {n["id"][4:] for n in nodes
                if str(n.get("id", "")).startswith("flt-")
                and n["id"] not in ("flt-body", "flt-clear", "flt-done")}

    FEATURES = ("has_subtitles", "has_trailer", "has_special_feature",
                "has_theme_song", "has_theme_video")

    def test_features_are_offered_on_movies_and_tv(self):
        for ctype in ("movies", "tvshows"):
            with self.subTest(ctype=ctype):
                keys = self._panel_keys(ctype)
                for f in self.FEATURES:
                    self.assertIn(f, keys)

    def test_and_nowhere_else(self):
        """They ask about a media item. A book has no subtitles and an
        album has no trailer, so these could only ever return nothing."""
        for ctype in ("music", "books", "boxsets", "playlists", None):
            with self.subTest(ctype=ctype):
                keys = self._panel_keys(ctype)
                for f in self.FEATURES:
                    self.assertNotIn(f, keys)

    def test_languages_follow_the_features_set_plus_mixed(self):
        for ctype in ("movies", "tvshows", None):
            with self.subTest(ctype=ctype, offered=True):
                keys = self._panel_keys(ctype)
                self.assertIn("audio_languages", keys)
                self.assertIn("subtitle_languages", keys)
        for ctype in ("music", "books", "boxsets"):
            with self.subTest(ctype=ctype, offered=False):
                keys = self._panel_keys(ctype)
                self.assertNotIn("audio_languages", keys)
                self.assertNotIn("subtitle_languages", keys)

    def test_a_music_library_has_no_play_state(self):
        """An album has no play position of its own -- web hides these
        for Albums, Artists and Songs alike."""
        keys = self._panel_keys("music")
        for k in ("played", "unplayed", "resumable"):
            self.assertNotIn(k, keys)

    def test_but_it_still_has_favorites(self):
        """Which web offers unconditionally, and is the reason Status is
        gated per box rather than per section."""
        self.assertIn("favorite", self._panel_keys("music"))

    def test_a_collection_type_nobody_listed_keeps_its_play_state(self):
        """The play-state gate is a DENY-list on purpose.

        Written as an allow-list, a collection type added to Jellyfin (or
        simply left out of the table) would quietly lose its Unplayed box
        -- a filter silently missing is much harder to notice than one
        that is present and matches nothing.
        """
        keys = self._panel_keys("somethingnewentirely")
        for k in ("played", "unplayed", "resumable", "favorite"):
            self.assertIn(k, keys)

    def test_a_section_with_every_row_gated_out_draws_no_heading(self):
        """Otherwise a music library shows a bold "Features" with
        nothing under it, which reads as a failed load."""
        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "music", "title": "Music"})
        b.route["_filtervals"] = dict(self.VALS)
        _n, handlers = build_scene(b, size=(1280, 720))
        handlers["grid-filter"]["click"]()
        nodes, _h = build_scene(b)
        texts = {n.get("text") for n in nodes if n.get("t") == "text"}
        self.assertNotIn("Features", texts)
        self.assertIn("Status", texts)      # ...and one that survives

    def test_the_gate_understands_both_forms(self):
        """``None`` everywhere, a set as an allow-list, an ("except",
        set) pair as a deny-list."""
        from jellyfin_mpv_shim.mpvtk_browser.dialogs import _applies
        self.assertTrue(_applies(None, "anything"))
        self.assertTrue(_applies(frozenset({"movies"}), "movies"))
        self.assertFalse(_applies(frozenset({"movies"}), "music"))
        self.assertFalse(_applies(("except", frozenset({"music"})), "music"))
        self.assertTrue(_applies(("except", frozenset({"music"})), "movies"))
        # None is a real collection type here -- an untyped library --
        # and both forms have to answer for it rather than treat it as
        # "no gate given".
        self.assertTrue(_applies(frozenset({"movies", None}), None))
        self.assertTrue(_applies(("except", frozenset({"music"})), None))


class _ShrinkingSource(FakeSource):
    """A library that answers differently once a filter is on, and with
    the SAME number of items -- 30 either way, but different ones.

    Same-length is the case that matters: `spread` clamps a list to
    `total`, so a filter that returns fewer items truncates the old tail
    by accident. Only a result set the same size (or larger) leaves slots
    past page 0 holding items of the previous query.
    """

    PAGE = 20
    TOTAL = 30

    def get_library_items(self, server_uuid, parent_id, start_index=0,
                          sort_by="SortName", sort_order="Ascending",
                          limit=100, filters=None, image_type=None,
                          collection_type=None):
        self.queries.append({"parent_id": parent_id,
                             "start_index": start_index,
                             "sort_by": sort_by, "sort_order": sort_order,
                             "filters": dict(filters or {}),
                             "image_type": image_type,
                             "collection_type": collection_type})
        tag = "Filtered" if (filters or {}).get("unplayed") else "All"
        items = [{"Id": "%s%d" % (tag, i), "Name": "%s %d" % (tag, i),
                  "Type": "Movie"} for i in range(self.TOTAL)]
        return items[start_index:start_index + self.PAGE], self.TOTAL


class TestFilterRevalidation(unittest.TestCase):
    """Changing a filter must not blank the library while it re-queries.

    ``render`` answers a missing ``_items`` with ``chrome.busy()`` for the
    **whole page** -- title, filter bar and A-Z rail included -- and
    ``_reload`` used to pop it. So every filter tick, sort change and
    letter press replaced the library with a spinner. Behind the filter
    panel, which covers the middle of the window, all that is visible of
    that is the page going empty: **[iw]** "it makes the page look dead
    behind a modal while re-querying".

    Stale-while-revalidate, the rule ``refresh_live_tv`` already argues
    for. The risk it introduces is the point of the second half of these
    tests: keeping the old list means the new one has to REPLACE it.
    """

    def _grid(self, source=None, pool=None):
        b = MpvtkBrowser(app=None, source=source or FakeSource(),
                         controller=FakeController())
        b._pool = pool or _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "movies", "title": "Movies"})
        return b

    @staticmethod
    def _toggle_unplayed(b):
        _n, handlers = build_scene(b, size=(1280, 720))
        handlers["grid-filter"]["click"]()
        _n2, panel = build_scene(b)
        panel["flt-unplayed"]["click"]()

    def test_the_page_survives_the_frame_the_query_runs_in(self):
        b = self._grid()
        loaded = {n.get("id") for n in build_scene(b, size=(1280, 720))[0]}
        self.assertIn("grid-sort", loaded)

        pool = _DeferredPool()
        b._pool = pool
        self._toggle_unplayed(b)
        # ...and now look at the frame BEFORE the results land.
        mid = {n.get("id") for n in build_scene(b, size=(1280, 720))[0]}
        for nid in ("grid-sort", "grid-filter", "grid-l-A"):
            self.assertIn(nid, mid, "the page blanked while re-querying")
        self.assertTrue(
            any(str(i).startswith("grid-0-g") for i in mid),
            "the tiles vanished while re-querying")

    def test_the_new_results_replace_the_old_ones_tail_and_all(self):
        """The hazard the fix introduces, and the reason it is not simply
        "stop popping _items".

        ``_install`` SPREADS its page over whatever is already there --
        which is right for a window landing mid-load and wrong for a new
        query. Only page 0 comes back on the first install, so without a
        clear, everything past it goes on showing the previous filter's
        items and no later load heals it: every slot is full, so nothing
        is ever asked for again.
        """
        src = _ShrinkingSource()
        b = self._grid(source=src)
        # As if the user had scrolled the whole library in, which is the
        # state a stale tail can hide in.
        b.route["_items"] = [{"Id": "All%d" % i, "Name": "All %d" % i,
                              "Type": "Movie"} for i in range(src.TOTAL)]

        self._toggle_unplayed(b)

        items = b.route.get("_items") or []
        self.assertEqual(len(items), src.TOTAL)
        left = [i["Name"] for i in items[:src.PAGE]]
        self.assertTrue(all(n.startswith("Filtered") for n in left), left)
        tail = items[src.PAGE:]
        self.assertTrue(all(i is None for i in tail),
                        "items from the previous filter survived: %r"
                        % [(i or {}).get("Name") for i in tail])

    def test_nothing_is_fetched_against_the_mixture_in_between(self):
        """A page-in issued while revalidating would be fetched with the
        NEW query -- ``_window`` binds it on the loop thread -- and
        spliced into the OLD list, leaving the grid holding a mixture of
        two filters that no later load heals.

        The scroll matters and the first version of this test had none:
        with every slot loaded there is nothing for ``window`` to fetch,
        so it passed with the guard removed. It needs the visible range
        to sit over HOLES, which means scrolling past the loaded head.
        """
        src = _ShrinkingSource()
        src.TOTAL = 400          # ...so the tail is holes, not items
        b = self._grid(source=src)
        route = b.route
        # Deep enough that what is on screen has never been fetched.
        grid_scroll(b, route, 4000, 20000)
        b._pool = _DeferredPool()
        # Prove the window WOULD fetch here, or the assertion below is
        # about nothing: same scroll, no revalidation in progress.
        grid_scroll(b, route, 4200, 20000)
        self.assertTrue(b._pool.queued,
                        "no page-in even without a reload in flight -- "
                        "this test cannot see what it is asserting")

        b._pool = _DeferredPool()
        # A sort change, which reloads with NO modal over the grid: the
        # panel would block the scroll that drives this.
        _n, handlers = build_scene(b, size=(1280, 720))
        handlers["grid-sort"]["select"](2, "Release Date")
        b._pool = _DeferredPool()
        grid_scroll(b, route, 4400, 20000)
        self.assertFalse(
            b._pool.queued,
            "a page-in was issued while the query was in flight")

    def test_repeated_filter_changes_do_not_walk(self):
        """Over several rounds, not one, and with the tail refilled
        between them -- which is what a user scrolling does.

        Both halves are needed and neither is decoration. The recurring
        bug shape in this codebase is state feeding back into the input
        that produced it, and this change deliberately feeds the old list
        forward into the frame that draws the new one; one toggle proves
        nothing about the second. And the FIRST toggle cannot see an
        aliasing bug at all: ``_bound_query`` answers ``route["_filters"]
        or {}``, so before any filter is set it hands back a fresh dict
        and a stored key cannot alias anything. From the second toggle on
        it is the route's own dict, mutated in place by every filter
        handler -- so a key that stored the reference changes along with
        the route and compares equal forever. Written without the refill,
        this test passed with exactly that bug in place: the stale tail it
        causes has nowhere to show when the tail is already holes.
        """
        src = _ShrinkingSource()
        b = self._grid(source=src)
        for i in range(4):
            with self.subTest(round=i):
                tag = "Filtered" if i % 2 == 0 else "All"
                stale = "All" if i % 2 == 0 else "Filtered"
                # As if the user had scrolled the rest of the library in
                # before touching the filter again.
                items = list(b.route.get("_items") or [])
                b.route["_items"] = [
                    it or {"Id": "%s%d" % (stale, n),
                           "Name": "%s %d" % (stale, n), "Type": "Movie"}
                    for n, it in enumerate(items)] or None

                self._toggle_unplayed(b)

                items = b.route.get("_items") or []
                self.assertEqual(len(items), src.TOTAL,
                                 "the list changed length on round %d" % i)
                real = [x for x in items if x]
                self.assertEqual(len({x["Id"] for x in real}), len(real),
                                 "duplicate items after round %d" % i)
                wrong = [x["Name"] for x in real
                         if not x["Name"].startswith(tag)]
                self.assertFalse(wrong,
                                 "round %d left items from the other "
                                 "filter: %r" % (i, wrong))
