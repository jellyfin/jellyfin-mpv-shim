"""Banner / Logo / Disc: the artwork a library view is *set* to.

The setting existed and the shapes were right, but the server was never asked
for the artwork -- ``EnableImageTypes`` was a fixed ``Primary,Thumb,Backdrop``
-- so ``ImageTags`` never carried a Banner and every banner tile fell through
to the item's thumbnail, letterboxed into a 5.4:1 frame. jellyfin-web asks for
the type the view is set to (``useFetchItems.ts``).
"""

import sys
import unittest

sys.argv = ["test"]

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
    """A poster, a still and a backdrop are photographs of a frame: cropping
    one takes scenery off the edges. A wordmark loses the name."""

    def _contains(self, resolved, requested):
        from jellyfin_mpv_shim.mpvtk_browser.tile_renderer import TileRenderer
        return TileRenderer._contains(resolved, requested)

    def test_a_banner_fills_its_own_strip(self):
        """Asked for and delivered: the artwork was cut for this shape, and
        the user asked for it to cover rather than letterbox."""
        self.assertFalse(self._contains("Banner", "Banner"))

    def test_a_poster_standing_in_still_fills_the_strip(self):
        self.assertFalse(self._contains("Primary", "Banner"))

    def test_a_borrowed_banner_is_drawn_whole(self):
        """On a 16:9 logo card, cropping the banner would trim exactly the
        title it was borrowed for."""
        self.assertTrue(self._contains("Banner", "Logo"))

    def test_a_logo_is_never_cropped(self):
        for requested in ("Logo", "Banner", "Primary", "Thumb"):
            with self.subTest(requested):
                self.assertTrue(self._contains("Logo", requested))

    def test_photographs_are_untouched(self):
        for resolved in ("Primary", "Thumb", "Backdrop", "Disc"):
            with self.subTest(resolved):
                self.assertFalse(self._contains(resolved, "Primary"))

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

    def test_the_next_page_asks_for_it_too(self):
        """Page two used to arrive with no banner tags, so scrolling a
        Banner view turned it back into thumbnails halfway down."""
        b, src = self._grid("banner")
        page = b._page_for(b.route)
        page._on_scroll_end(10_000, 10_000)
        self.assertGreater(len(src.queries), 1, "no second page was fetched")
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
