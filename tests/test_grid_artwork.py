"""Banner / Logo / Disc: the artwork a library view is *set* to.

The setting existed and the shapes were right, but the server was never asked
for the artwork -- ``EnableImageTypes`` was a fixed ``Primary,Thumb,Backdrop``
-- so ``ImageTags`` never carried a Banner and every banner tile fell through
to the item's thumbnail, letterboxed into a 5.4:1 frame. jellyfin-web asks for
the type the view is set to (``useFetchItems.ts``).
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import sys
import unittest

sys.argv = ["test"]

from jellyfin_mpv_shim.mpvtk_browser.strips import TileGeom  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    LibrarySource, browse_image_types)

from tests._shell_harness import (  # noqa: E402
    FakeController, FakeSource, _SyncPool)


class EnableImageTypesTest(unittest.TestCase):
    def test_the_browse_defaults_are_always_asked_for(self):
        """Our fallback chains are wider than web's: a Banner view still
        draws a poster where there is no banner, and a Thumb view still
        reaches a backdrop."""
        for value in (None, "", "Primary", "Thumb", "nonsense"):
            with self.subTest(value):
                self.assertEqual(browse_image_types(value),
                                 "Primary,Thumb,Backdrop")

    def test_the_chosen_type_is_added(self):
        for value in ("Banner", "Logo", "Disc"):
            with self.subTest(value):
                self.assertIn(value, browse_image_types(value).split(","))

    def test_the_whole_fallback_chain_is_asked_for(self):
        """Not just the name asked for: a Banner view falls back to the
        logo, and a query that left Logo out would be looking for a tag it
        told the server not to send."""
        self.assertEqual(browse_image_types("Banner"),
                         "Primary,Thumb,Backdrop,Banner,Logo")
        self.assertEqual(browse_image_types("Logo"),
                         "Primary,Thumb,Backdrop,Logo,Banner")
        self.assertEqual(browse_image_types("Disc"),
                         "Primary,Thumb,Backdrop,Disc")

    def test_the_stored_spelling_is_accepted(self):
        """view_prefs stores lower case; the server's enum is capitalised."""
        self.assertEqual(browse_image_types("banner"),
                         "Primary,Thumb,Backdrop,Banner,Logo")


class SpecTest(unittest.TestCase):
    """image_spec for the three by-name types. Web tries the tag, then the
    parent logo for a Logo request, then drops into the Primary chain."""

    def spec(self, item, image_type):
        return LibrarySource.__new__(LibrarySource).image_spec(item,
                                                               image_type)

    def test_a_banner_is_used_when_the_item_has_one(self):
        self.assertEqual(
            self.spec({"Id": "s1", "Type": "Series",
                       "ImageTags": {"Banner": "b", "Primary": "p"}},
                      "Banner"),
            ("s1", "Banner", "b"))

    def test_a_banner_view_borrows_the_logo_before_the_poster(self):
        """A banner and a logo are the same artwork at different margins,
        and about half a TV library has one and not the other (measured:
        107 banners against 156 logos over 200 series). Web goes straight
        to the poster here; a row of poster slices among title cards is
        what that looks like."""
        self.assertEqual(
            self.spec({"Id": "s1", "Type": "Series",
                       "ImageTags": {"Logo": "lg", "Thumb": "t",
                                     "Primary": "p"}}, "Banner"),
            ("s1", "Logo", "lg"))

    def test_a_logo_view_borrows_the_banner(self):
        self.assertEqual(
            self.spec({"Id": "s1", "Type": "Series",
                       "ImageTags": {"Banner": "b", "Primary": "p"}},
                      "Logo"),
            ("s1", "Banner", "b"))

    def test_with_neither_it_is_the_poster_not_the_thumbnail(self):
        """The original defect: the generic chain reaches ImageTags.Thumb
        before ImageTags.Primary, so those tiles came out as letterboxed
        stills. Web's card builder falls through to Primary."""
        self.assertEqual(
            self.spec({"Id": "s1", "Type": "Series",
                       "ImageTags": {"Thumb": "t", "Primary": "p"}},
                      "Banner"),
            ("s1", "Primary", "p"))

    def test_a_disc_has_no_stand_in(self):
        """Nothing else in a library is a round label, so it goes straight
        to the poster rather than borrowing a wordmark."""
        self.assertEqual(
            self.spec({"Id": "m1", "Type": "Movie",
                       "ImageTags": {"Logo": "lg", "Primary": "p"}}, "Disc"),
            ("m1", "Primary", "p"))

    def test_disc_falls_through_the_same_way(self):
        self.assertEqual(
            self.spec({"Id": "m1", "Type": "Movie",
                       "ImageTags": {"Thumb": "t", "Primary": "p"}}, "Disc"),
            ("m1", "Primary", "p"))

    def test_a_logo_borrows_its_parents(self):
        """url.ts:51 -- an episode has no logo of its own and its series
        does."""
        self.assertEqual(
            self.spec({"Id": "e1", "Type": "Episode",
                       "ParentLogoItemId": "s1", "ParentLogoImageTag": "lg",
                       "ImageTags": {"Primary": "p"}}, "Logo"),
            ("s1", "Logo", "lg"))

    def test_an_item_with_nothing_landscape_still_gets_its_thumb(self):
        """The fall-through is to the Primary *chain*, not to Primary alone
        -- an item carrying only a thumb still draws it."""
        self.assertEqual(
            self.spec({"Id": "s1", "Type": "Series",
                       "ImageTags": {"Thumb": "t"}}, "Banner"),
            ("s1", "Thumb", "t"))

    def test_a_thumb_request_is_untouched(self):
        """The preferThumb ladder still reaches the backdrop before the
        item's own poster."""
        self.assertEqual(
            self.spec({"Id": "e1", "Type": "Episode",
                       "BackdropImageTags": ["bd"],
                       "ImageTags": {"Primary": "p"}}, "Thumb"),
            ("e1", "Backdrop", "bd"))


class CoverOrContainTest(unittest.TestCase):
    """**Contain is the default. One pairing covers: a poster into a poster
    tile.**

    Cropping is only free where the artwork and the tile already agree, and
    the one place they reliably do is 2:3 key art in a 2:3 card. Everywhere
    else the crop is destructive in proportion to the disagreement, and the
    disagreement is per ITEM -- which is why ``auto_geom``'s per-row shape
    cannot be the whole answer.
    """

    POSTER = TileGeom(tile_w=150, tile_h=225)
    WIDE = TileGeom(tile_w=240, tile_h=135)
    SQUARE = TileGeom(tile_w=180, tile_h=180)

    def _contains(self, resolved, geom, ratio=None):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        item = {"Id": "i"}
        if ratio is not None:
            item["PrimaryImageAspectRatio"] = ratio
        return TileRenderer._contains(resolved, geom, item)

    # -- the one that covers ------------------------------------------------

    def test_a_poster_in_a_poster_tile_covers(self):
        self.assertFalse(self._contains("Primary", self.POSTER, 2 / 3))

    def test_artwork_with_no_measured_ratio_still_covers_in_a_poster_tile(self):
        """Most library items carry no ``PrimaryImageAspectRatio``. Reading
        that absence as "not a poster" would letterbox every ordinary film
        grid on the strength of a missing field."""
        self.assertFalse(self._contains("Primary", self.POSTER))

    # -- the cases this rule exists for -------------------------------------

    def test_a_film_in_a_row_of_episodes_is_drawn_whole(self):
        """``auto_geom`` shapes a row from the MEDIAN ratio, so a playlist
        that is mostly episodes comes out landscape — and the one film in it
        had the top and bottom taken off its poster."""
        self.assertTrue(self._contains("Primary", self.WIDE, 2 / 3))

    def test_home_video_footage_is_drawn_whole(self):
        """Arbitrary footage: 4:3, 16:9 and portrait phone video in one
        grid. Whatever shape the row takes, most of it is the wrong shape
        for what goes in it."""
        for ratio in (4 / 3, 9 / 16, 1.0, 2.35):
            with self.subTest(ratio=round(ratio, 2)):
                self.assertTrue(self._contains("Primary", self.WIDE, ratio))

    def test_a_still_in_a_poster_tile_is_drawn_whole(self):
        """The mismatch the other way round."""
        self.assertTrue(self._contains("Primary", self.POSTER, 16 / 9))

    # -- everything that is not a Primary -----------------------------------

    def test_nothing_but_a_primary_ever_covers(self):
        """A Thumb, a Backdrop, a Disc, a Logo and a Banner are all drawn
        whole now — the first three because they are never the shape of a
        poster tile, and the last two because they never were cropped."""
        for resolved in ("Thumb", "Backdrop", "Disc", "Logo", "Banner"):
            for geom in (self.POSTER, self.WIDE, self.SQUARE):
                with self.subTest(resolved=resolved, w=geom.tile_w):
                    self.assertTrue(self._contains(resolved, geom, 2 / 3))

    def test_a_square_tile_never_covers(self):
        """Album art is square and the tile is square, so contain and cover
        are the same picture — and where they are not, the artwork is not a
        poster and must not be cropped to look like one."""
        for ratio in (1.0, 2 / 3, 16 / 9, None):
            with self.subTest(ratio=ratio):
                self.assertTrue(self._contains("Primary", self.SQUARE, ratio))

    def test_an_unknown_tile_shape_is_the_safe_answer(self):
        self.assertTrue(self._contains("Primary", None, 2 / 3))

    def test_the_two_crops_are_cached_apart(self):
        """Fill and fit are different pictures of one source at one size,
        and the store keeps them on disk."""
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import make_key
        self.assertNotEqual(make_key("i", "Banner", "t", 240, 135),
                            make_key("i", "Banner", "t", 240, 135, fit="fit"))

    def test_existing_keys_are_unchanged(self):
        """Every bitmap already cached to disk stays addressable."""
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import make_key
        self.assertEqual(make_key("i", "Primary", "t", 150, 225),
                         make_key("i", "Primary", "t", 150, 225, fit=""))


class CoverDecodeTest(unittest.TestCase):
    """A tile that COVERS must be decoded covering, not fitted and then
    blown back up.

    The pieces were each right and the pair was not: the request asks the
    server to fill (``fill=not contain``), the paint cover-crops
    (``strips._paint_poster`` -> ``ImageOps.fit``), and in between the decode
    contained the result inside the tile -- throwing away precisely the
    overflow the crop was about to want, so the fit had to magnify what was
    left. Nothing here is visible to a size or a shape assertion: the answer
    is the same picture, softer.
    """

    TILE = (200, 300)

    #: What the shapes a cover tile actually sees cost, before this. Cover is
    #: reached for a Primary in a poster tile whose ratio is portrait, square
    #: or ABSENT -- and absent is the interesting one, because a
    #: ``BaseItemPerson`` (every Cast & Crew tile) carries no
    #: ``PrimaryImageAspectRatio`` at all, so a 16:9 headshot lands here.
    SHAPES = (("2:3 key art", (1000, 1500), 1.00),
              ("4:5 art", (1000, 1250), 1.20),
              ("a square headshot", (1000, 1000), 1.50),
              ("a 16:9 person still", (1920, 1080), 2.65))

    @staticmethod
    def _server_fill(source, box):
        """``fillWidth``/``fillHeight``: COVER the box, hand back the whole
        frame, never upscale past the file. Measured on a live 12.0 server
        -- no stand-in here can answer it."""
        iw, ih = source
        scale = min(1.0, max(box[0] / iw, box[1] / ih))
        return max(1, round(iw * scale)), max(1, round(ih * scale))

    @staticmethod
    def _paint_upscale(decoded, box):
        """What ``ImageOps.fit`` has to magnify to fill the tile."""
        return max(box[0] / decoded[0], box[1] / decoded[1])

    def _decoded(self, sent):
        from PIL import Image
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import _fit_into
        return _fit_into(Image.new("RGB", sent), self.TILE).size

    def test_the_paint_never_has_to_magnify_what_the_decode_kept(self):
        for name, source, _was in self.SHAPES:
            with self.subTest(art=name):
                sent = self._server_fill(source, self.TILE)
                up = self._paint_upscale(self._decoded(sent), self.TILE)
                self.assertLessEqual(
                    up, 1.01,
                    "%s: the tile is filled by magnifying the decode %.2fx"
                    % (name, up))

    def test_it_was_the_contain_that_was_costing_this(self):
        """The other half of the pair, so a failure above cannot be read as
        "the numbers were never real". Replays what the decode used to do
        -- ``Image.thumbnail``, contain -- and states what each shape paid.
        """
        from PIL import Image
        for name, source, was in self.SHAPES:
            with self.subTest(art=name):
                sent = self._server_fill(source, self.TILE)
                old = Image.new("RGB", sent)
                old.thumbnail(self.TILE, Image.LANCZOS)
                self.assertAlmostEqual(
                    self._paint_upscale(old.size, self.TILE), was, delta=0.02,
                    msg="%s no longer costs what this was written about"
                        % name)

    def test_it_never_upscales_into_the_cache(self):
        """A source too small to fill the tile is cropped to the SHAPE and
        left at its own resolution. Asking ``scale_to_cover`` for the box
        outright would be simpler and would park a magnified copy of a small
        image in a byte-bounded cache -- for no gain, since the paint-time
        fit resizes it from the same pixels by the same filter either way.
        """
        got = self._decoded((100, 100))
        self.assertLess(max(got), max(self.TILE))
        self.assertAlmostEqual(got[0] / got[1],
                               self.TILE[0] / self.TILE[1], delta=0.02)

    def test_artwork_already_the_right_size_is_left_alone(self):
        from PIL import Image
        from jellyfin_mpv_shim.mpvtk_browser.thumbnails import _fit_into
        src = Image.new("RGB", self.TILE)
        self.assertIs(_fit_into(src, self.TILE), src)

    # -- the wiring ---------------------------------------------------------

    def _asked(self, ratio):
        """``(box, cover)`` the renderer asks the store for, for a Primary
        going into a poster tile."""
        import types
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer

        asked = {}

        class _Thumbs:
            def get_cached(self, key):
                return None

            def is_gone(self, key):
                return False

            def request(self, key, url, box, callback, cover=False):
                asked["box"], asked["cover"] = tuple(box), bool(cover)

        class _Source:
            def image_spec(self, item, image_type, width, inherit=True):
                return item["Id"], "Primary", "tag"

            def image_url(self, server, item_id, itype, itag, w, h=None,
                          fill=False):
                asked["fill"] = fill
                return "http://s/art"

        art = types.SimpleNamespace(source=_Source(), server=object(),
                                    thumbs=_Thumbs())
        item = {"Id": "i"}
        if ratio is not None:
            item["PrimaryImageAspectRatio"] = ratio
        TileRenderer(art, None).poster_for(
            item, TileGeom(tile_w=150, tile_h=225))
        return asked

    def test_a_covering_tile_asks_the_store_to_cover(self):
        """The decision is ``_contains``'s and the store cannot infer it:
        the same box means two different pictures, and which one is right
        depends on the tile rather than on the artwork."""
        self.assertTrue(self._asked(2 / 3)["cover"])

    def test_a_contained_tile_does_not(self):
        self.assertFalse(self._asked(16 / 9)["cover"])

    def test_the_request_and_the_decode_still_agree(self):
        """``fill`` and ``cover`` are the same answer asked of the server
        and of the decode. They were allowed to disagree for one commit and
        the result was a picture cropped by neither."""
        for ratio in (2 / 3, 1.0, None, 16 / 9, 4 / 3):
            with self.subTest(ratio=ratio):
                asked = self._asked(ratio)
                self.assertEqual(asked["fill"], asked["cover"])


class GridAsksForItTest(unittest.TestCase):
    """The wiring: the grid's own query has to carry the view's image type,
    on the first page and on every page after it."""

    def _grid(self, image_type=None):
        src = FakeSource()
        if image_type is not None:
            src.view_settings = {"imageType": (image_type, None)}
        src.grid_items = [{"Id": "g%d" % i, "Name": "I", "Type": "Series"}
                          for i in range(60)]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "grid", "server": "srv1", "parent_id": "lib1",
                    "collection_type": "tvshows", "title": "Shows"})
        return b, src

    def test_a_banner_view_asks_the_server_for_banners(self):
        _b, src = self._grid("banner")
        self.assertEqual([q["image_type"] for q in src.queries], ["Banner"])

    def test_auto_asks_for_nothing_extra(self):
        _b, src = self._grid()
        self.assertEqual([q["image_type"] for q in src.queries], [None])

    def test_switching_to_banner_re_asks_the_server(self):
        """The reported bug. The setting only reshaped the grid, and the
        items in hand had been fetched WITHOUT Banner in EnableImageTypes --
        so every tile fell back to the poster it already had, and the debug
        log showed the query still asking for Primary,Thumb,Backdrop."""
        b, src = self._grid()
        b._page_for(b.route)._set_view("imageType", "banner")
        self.assertEqual(src.queries[-1]["image_type"], "Banner")

    def test_it_does_not_re_ask_when_the_query_is_the_same(self):
        """Titles, years and list-vs-grid change nothing the server was
        told; a refetch for those would be a round trip per checkbox. Nor
        does Poster, which asks for exactly what Auto asks for and differs
        only in the shape it draws at."""
        b, src = self._grid()
        before = len(src.queries)
        page = b._page_for(b.route)
        page._set_view("showTitle", False)
        page._set_view("showYear", False)
        page._set_view("imageType", "poster")
        self.assertEqual(len(src.queries), before)

    def test_the_refetch_reads_the_setting_it_just_changed(self):
        """The save is still in flight, so asking the source again would
        answer with the value the user changed away from -- refetching the
        old tags and drawing the grid straight back."""
        b, src = self._grid()
        # The source keeps insisting on "auto", as a server mid-save would.
        src.view_settings = {"imageType": ("primary", None)}
        b._page_for(b.route)._set_view("imageType", "banner")
        self.assertEqual(src.queries[-1]["image_type"], "Banner")

    def test_the_grid_does_not_blink_while_it_refetches(self):
        """In place: the items are not stale, only the tags on them."""
        b, _src = self._grid()
        b._page_for(b.route)._set_view("imageType", "banner")
        self.assertTrue(b.route.get("_items"))

    def test_a_scrolled_grid_refetches_all_of_it(self):
        """#617 made _install SPLICE its page in rather than replace the
        list, so a refetch refreshed items 0..99 and left every later window
        holding the tags fetched under the old EnableImageTypes -- and a
        filled slot is never re-requested, so the library stayed visibly
        mixed for as long as the route lived."""
        from tests._shell_harness import grid_scroll
        b, src = self._grid()
        grid_scroll(b, b.route, 10_000, 20_000)      # load past page 0
        items = list(b.route["_items"])
        marker = {"Id": "stale", "Name": "Stale", "Type": "Movie"}
        items[-1] = marker              # an object only the old fetch had
        b.route["_items"] = items
        b._page_for(b.route)._set_view("imageType", "banner")
        self.assertNotIn(
            marker, b.route.get("_items") or [],
            "the refetch kept items carrying the old artwork tags")

    def test_the_next_window_asks_for_it_too(self):
        """Page two used to arrive with no banner tags, so scrolling a
        Banner view turned it back into thumbnails halfway down."""
        from tests._shell_harness import grid_scroll
        b, src = self._grid("banner")
        grid_scroll(b, b.route, 10_000, 20_000)
        self.assertGreater(len(src.queries), 1, "no second window was fetched")
        self.assertEqual({q["image_type"] for q in src.queries}, {"Banner"})


class SettingsSurviveTheRestOfTheLoadTest(unittest.TestCase):
    """The load publishes twice -- items first, filter pickers after -- and
    the grid is on screen and its View settings reachable for the whole gap
    between them. That gap is the point of the split (Items/Filters is
    seconds against a real library), so what the user does inside it has to
    stick."""

    class _SlowFilters(FakeSource):
        """Runs a callback while the filter pickers are 'in flight'."""

        interrupt = None

        def get_filter_values(self, server_uuid, parent_id=None,
                              collection_type=None):
            if self.interrupt is not None:
                cb, self.interrupt = self.interrupt, None
                cb()
            return {"genres": [], "years": []}

    def _grid(self, during):
        src = self._SlowFilters()
        src.view_settings = {"imageType": ("primary", None),
                             "showTitle": (True, None),
                             "showYear": (True, None)}
        src.grid_items = [{"Id": "g1", "Name": "I", "Type": "Movie"}]
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "collection_type": "movies", "title": "Movies"}
        src.interrupt = lambda: during(b, route)
        b.navigate(route)
        return b, src, route

    def test_a_change_made_while_the_pickers_load_is_not_undone(self):
        """It was undone *silently*: the save had already reached the server,
        so the screen was left disagreeing with what is stored, with nothing
        on it to say so."""
        def during(b, route):
            self.assertIsNotNone(route.get("_items"),
                                 "the tiles had not been published yet")
            b._page_for(route)._set_view("showTitle", False)

        _b, src, route = self._grid(during)
        self.assertEqual([s[1:3] for s in src.saved_view_settings],
                         [("showTitle", False)])
        self.assertEqual(route["_view"]["showTitle"][0], False)

    def test_the_artwork_choice_survives_it_too(self):
        """Auto/Poster/Thumbnail all resolve to the same query, so they take
        the repaint branch and never bump the epoch -- which is what would
        otherwise have dropped the stale publish."""
        def during(b, route):
            b._page_for(route)._set_view("imageType", "poster")

        _b, _src, route = self._grid(during)
        self.assertEqual(route["_view"]["imageType"][0], "poster")

    def test_a_first_load_still_publishes_what_it_read(self):
        """The guard is "the route already has one", which is the same rule
        load() applies when it decides whether to ask the server at all."""
        _b, _src, route = self._grid(lambda _b, _r: None)
        self.assertEqual(route["_view"]["imageType"][0], "primary")


class AFailedSaveLandingLateTest(unittest.TestCase):
    """``on_error`` is deliberately not epoch-gated (see AsyncRunner), so a
    view save that fails can land after the user has walked away."""

    def _setup(self):
        from tests._shell_harness import _DeferredPool

        src = FakeSource()
        src.view_settings = {"imageType": ("primary", None)}
        src.grid_items = [{"Id": "g1", "Name": "I", "Type": "Movie"}]
        src.save_view_fails = True
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        left = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                "collection_type": "movies", "title": "Movies"}
        b.navigate(left)
        pool = _DeferredPool()
        b._pool = pool
        # A change the server will refuse, and one whose query differs, so
        # the rollback takes the refetch branch rather than the repaint one.
        b._page_for(left)._set_view("imageType", "banner")
        here = {"kind": "grid", "server": "srv1", "parent_id": "lib2",
                "collection_type": "movies", "title": "Other"}
        b.navigate(here)
        return b, pool, left, here

    def test_it_does_not_strand_the_screen_the_user_is_on(self):
        """The rollback reloaded the route it belonged to, and a reload bumps
        the epoch -- which cancelled the in-flight load of whatever was
        actually on screen. Nothing re-issues that load, so the library the
        user had just opened sat on a spinner for the rest of the session."""
        b, pool, _left, here = self._setup()
        epoch = b._epoch
        pool.drain()
        self.assertEqual(b._epoch, epoch, "the epoch was bumped from a route "
                                          "nobody is looking at")
        self.assertTrue(here.get("_items"), "the current screen never loaded")

    def test_the_route_it_belongs_to_is_still_put_back(self):
        """Quietly -- nav.load, which re-runs the loader without touching the
        epoch or repainting. The rollback is still real, it just does not get
        to interrupt a screen it is not on."""
        b, pool, left, _here = self._setup()
        pool.drain()
        self.assertEqual(left["_view"]["imageType"][0], "primary")

    def test_a_failure_on_the_CURRENT_route_still_reloads_it(self):
        """The guard must not cost the case it was always for."""
        from tests._shell_harness import _DeferredPool

        src = FakeSource()
        src.view_settings = {"imageType": ("primary", None)}
        src.grid_items = [{"Id": "g1", "Name": "I", "Type": "Movie"}]
        src.save_view_fails = True
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "collection_type": "movies", "title": "Movies"}
        b.navigate(route)
        b._pool = pool = _DeferredPool()
        b._page_for(route)._set_view("imageType", "banner")
        pool.drain()
        self.assertEqual(route["_view"]["imageType"][0], "primary")
        self.assertEqual(src.queries[-1]["image_type"], None,
                         "the rolled-back grid was not re-fetched")


if __name__ == "__main__":
    unittest.main()
