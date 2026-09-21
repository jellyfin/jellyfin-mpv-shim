"""A library remembers how it was sorted, where jellyfin-web looks.

#758. The sort lived in `route["_sort"]` as an **index into the menu** and died
with the navigation, so every visit to a library came back in name order.

Two things about the shape matter more than the persistence itself:

* **the names are stored, never the index.** `grid.EXTRA_SORTS` appends per
  collection type and its own comment records that a stored index re-points
  every route already carrying one -- persisting the index would make that trap
  outlive the session and cross libraries;
* **the keys are web's** (`<key>-sortby`, `<key>-sortorder`), written back to
  the key the read came from. So changing the sort here changes it in the
  user's web client. That is what parity was asked for, and it is worth saying
  out loud because somebody will report it as a bug.

The second test is the one that matters. A scene assertion is not a persistence
assertion: the dropdown showing the right label proves nothing about what the
next navigation asks the server for.
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

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource, _SyncPool, build_scene  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser         # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.pages import grid               # noqa: E402

#: Index of "Date Added" in the base menu, resolved from the table rather
#: than written as a number -- a number here would be the very thing this
#: feature refuses to store.
DATE_ADDED = [i for i, s in enumerate(grid.SORTS)
              if s[1] == "DateCreated"][0]


class SortPersistenceTest(unittest.TestCase):
    def _browser(self, view_settings=None, ctype=None):
        src = FakeSource()
        src.grid_items = [{"Id": "g%d" % i, "Name": "Item %d" % i,
                           "Type": "Movie"} for i in range(4)]
        if view_settings is not None:
            src.view_settings = view_settings
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "title": "Lib"}
        if ctype:
            route["collection_type"] = ctype
        b.navigate(route)
        return b, src

    def _page(self, b):
        return b._page_for(b.route)

    # -- writing -----------------------------------------------------------

    def test_changing_the_sort_saves_both_halves(self):
        b, src = self._browser()

        self._page(b)._set_sort(DATE_ADDED)

        saved = {(setting, value)
                 for _parent, setting, value, _key in src.saved_view_settings}
        self.assertIn(("sortby", "DateCreated"), saved)
        self.assertIn(("sortorder", "Descending"), saved)

    def test_it_stores_the_name_and_not_the_index(self):
        """The trap `EXTRA_SORTS` documents: a TV library's menu is longer, so
        an index means a different sort depending on where it was written."""
        b, src = self._browser()

        self._page(b)._set_sort(DATE_ADDED)

        values = [value for _p, _s, value, _k in src.saved_view_settings]
        self.assertNotIn(DATE_ADDED, values)
        self.assertNotIn(str(DATE_ADDED), values)

    def test_a_save_lands_on_the_key_it_was_read_from(self):
        """Otherwise the user's web client goes on reading the old value from
        the key it wrote, while this client reads its own."""
        b, src = self._browser(view_settings={
            "sortby": ("PlayCount", "items-lib1-Movie-sortby"),
            "sortorder": ("Descending", "items-lib1-Movie-sortorder")})

        self._page(b)._set_sort(DATE_ADDED)

        keys = {setting: key
                for _p, setting, _v, key in src.saved_view_settings}
        self.assertEqual("items-lib1-Movie-sortby", keys["sortby"])
        self.assertEqual("items-lib1-Movie-sortorder", keys["sortorder"])

    # -- reading -----------------------------------------------------------

    def test_a_stored_sort_is_what_the_next_visit_asks_for(self):
        """The half a scene assertion cannot make."""
        b, src = self._browser(view_settings={
            "sortby": ("DateCreated", "items-lib1-sortby"),
            "sortorder": ("Descending", "items-lib1-sortorder")})

        query = src.queries[-1]

        self.assertEqual("DateCreated", query["sort_by"])
        self.assertEqual("Descending", query["sort_order"])

    def test_an_untouched_library_still_asks_for_the_default(self):
        """The control: a resolver that answered something for everybody
        would pass the test above and re-sort every library nobody has
        touched."""
        b, src = self._browser()

        query = src.queries[-1]

        self.assertEqual("SortName", query["sort_by"])
        self.assertEqual("Ascending", query["sort_order"])

    def test_the_dropdown_shows_the_stored_sort(self):
        b, _src = self._browser(view_settings={
            "sortby": ("DateCreated", "items-lib1-sortby"),
            "sortorder": ("Descending", "items-lib1-sortorder")})

        nodes, _h = build_scene(b)
        picker = [n for n in nodes if n.get("id") == "grid-sort"]

        self.assertTrue(picker, "the sort dropdown is not on screen")
        self.assertEqual(DATE_ADDED, picker[0].get("sel"))

    def test_a_sort_this_library_does_not_offer_falls_back(self):
        """`DateLastContentAdded` is the TV-only entry. Stored against a
        movies library -- which is exactly what happens to somebody who set it
        in web on their shows and then opened their films -- it must not
        select something arbitrary."""
        b, src = self._browser(view_settings={
            "sortby": ("DateLastContentAdded", "items-lib1-sortby"),
            "sortorder": ("Descending", "items-lib1-sortorder")})

        self.assertEqual("SortName", src.queries[-1]["sort_by"])

    def test_the_tv_only_sort_is_honoured_on_a_tv_library(self):
        """The other half of the fallback: it is a fallback, not a filter."""
        b, src = self._browser(ctype="tvshows", view_settings={
            "sortby": ("DateLastContentAdded", "items-lib1-sortby"),
            "sortorder": ("Descending", "items-lib1-sortorder")})

        self.assertEqual("DateLastContentAdded", src.queries[-1]["sort_by"])

    def test_a_direction_the_menu_cannot_express_keeps_the_field(self):
        """Our menu couples a direction to each field and web does not, so
        `SortName`/`Descending` has no entry here. The field is the bigger
        half of what was asked for."""
        b, src = self._browser(view_settings={
            "sortby": ("CommunityRating", "items-lib1-sortby"),
            "sortorder": ("Ascending", "items-lib1-sortorder")})

        self.assertEqual("CommunityRating", src.queries[-1]["sort_by"])

    def test_a_stored_sort_resolving_to_index_zero_is_still_recorded(self):
        """Index 0 is the one answer `_sort_index` can give that is falsy.

        `_sort_index` returns None rather than 0 so that "nothing stored" is
        distinguishable from "stored", and a caller testing the index for
        truthiness throws that distinction away again. Name/Descending is the
        case that reaches it from real use: web can store it, our menu couples
        a direction to each field and has no entry for it, so the loose match
        lands on entry 0 -- Name/Ascending.

        The query is the same either way today, because the route's own
        default is also entry 0. What the route carries is not: unrecorded,
        the resolution re-runs on every reload, and the day `SORTS[0]` stops
        being the route default it stops agreeing too.
        """
        b, _src = self._browser(view_settings={
            "sortby": ("SortName", "items-lib1-sortby"),
            "sortorder": ("Descending", "items-lib1-sortorder")})

        self.assertEqual(0, b.route.get("_sort"))


if __name__ == "__main__":
    unittest.main()
