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
                self.assertEqual(browse_image_types(value),
                                 "Primary,Thumb,Backdrop," + value)

    def test_the_stored_spelling_is_accepted(self):
        """view_prefs stores lower case; the server's enum is capitalised."""
        self.assertEqual(browse_image_types("banner"),
                         "Primary,Thumb,Backdrop,Banner")


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

    def test_without_a_banner_it_is_the_poster_not_the_thumbnail(self):
        """The defect: the generic chain reaches ImageTags.Thumb before
        ImageTags.Primary, so half a TV library came out as letterboxed
        stills. Web's card builder falls through to Primary."""
        self.assertEqual(
            self.spec({"Id": "s1", "Type": "Series",
                       "ImageTags": {"Thumb": "t", "Primary": "p"}},
                      "Banner"),
            ("s1", "Primary", "p"))

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

    def test_the_next_page_asks_for_it_too(self):
        """Page two used to arrive with no banner tags, so scrolling a
        Banner view turned it back into thumbnails halfway down."""
        b, src = self._grid("banner")
        page = b._page_for(b.route)
        page._on_scroll_end(10_000, 10_000)
        self.assertGreater(len(src.queries), 1, "no second page was fetched")
        self.assertEqual({q["image_type"] for q in src.queries}, {"Banner"})


if __name__ == "__main__":
    unittest.main()
