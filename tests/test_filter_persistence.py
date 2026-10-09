"""A library remembers its filters across a relaunch.

The filters lived in ``route["_filters"]`` and died with the process: close
the app and every library came back unfiltered.

Unlike the sort, the key is OURS. No jellyfin-web client stores a filter
setting on the server -- the legacy menu writes a scatter of per-setting
``-filter-*`` keys, the one JSON-blob ``-filter`` key is localStorage-only,
and the modern app keeps its whole view settings per browser -- so this is
not a cross-client write. It is the same DisplayPreferences document and
the same read-it-back-to-where-you-found-it contract as the sort, applied
to a setting only this client reads. See ``view_prefs.FILTERS_SETTING``.

The query tests are the ones that matter. A ticked box or a Filter badge
proves nothing about what the next visit asks the server for.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import json
import sys
import unittest

sys.argv = [sys.argv[0]]

from tests._shell_harness import FakeSource, _SyncPool, build_scene  # noqa: E402

from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser         # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser import view_prefs               # noqa: E402

#: The stored shape is the page's own filter dict, so the tests build
#: stored values the same way the page does -- through the resolver, not
#: by hand, so a change to the shape shows up here as a failure to save
#: rather than as a silent mismatch.
STORED = {"genre": "Action", "unplayed": True}


class FilterPersistenceTest(unittest.TestCase):
    def _browser(self, view_settings=None, route_extra=None, supported=None):
        src = FakeSource()
        src.grid_items = [{"Id": "g%d" % i, "Name": "Item %d" % i,
                           "Type": "Movie"} for i in range(4)]
        if view_settings is not None:
            src.view_settings = view_settings
        if supported is not None:
            src.supported_filters = frozenset(supported)
        b = MpvtkBrowser(app=None, source=src)
        b._pool = _SyncPool()
        b.server = "srv1"
        route = {"kind": "grid", "server": "srv1", "parent_id": "lib1",
                 "title": "Lib"}
        route.update(route_extra or {})
        b.navigate(route)
        return b, src

    def _page(self, b):
        return b._page_for(b.route)

    def _stored_view(self, filters, key="items-lib1-mpvshim-filters"):
        return {"filters": (dict(filters), key)}

    # -- writing -----------------------------------------------------------

    def test_ticking_a_filter_saves_the_whole_dict(self):
        b, src = self._browser()

        self._page(b)._toggle_filter("unplayed")

        saved = {(setting, value)
                 for _parent, setting, value, _key in src.saved_view_settings}
        # `played: false` rides along because the toggle clears the
        # mutually-exclusive partner into the same dict -- which is the
        # whole-dict claim: what is saved is the page's own filter state,
        # not a translation of one tick.
        self.assertIn((view_prefs.FILTERS_SETTING,
                       json.dumps({"played": False, "unplayed": True},
                                  sort_keys=True)),
                      saved)

    def test_the_az_rail_is_saved_too(self):
        """The letter is one of `_filters`, and a filter that does not
        survive a relaunch is the bug this exists to fix."""
        b, src = self._browser()

        self._page(b)._set_filter("letter", "S")

        _p, _s, value, _k = src.saved_view_settings[-1]
        self.assertEqual("S", json.loads(value)["letter"])

    def test_a_save_lands_on_the_key_it_was_read_from(self):
        b, src = self._browser(view_settings=self._stored_view(
            STORED, key="items-lib1-Movie-mpvshim-filters"))

        self._page(b)._toggle_filter("favorite")

        _p, _s, _v, key = src.saved_view_settings[-1]
        self.assertEqual("items-lib1-Movie-mpvshim-filters", key)

    def test_clearing_saves_an_empty_dict_not_nothing(self):
        """The empty dict is what keeps a cleared library cleared: the load
        seeds the stored filters only where the route carries none at all."""
        b, src = self._browser(view_settings=self._stored_view(STORED))

        self._page(b)._clear_filters()

        _p, _s, value, _k = src.saved_view_settings[-1]
        self.assertEqual({}, json.loads(value))

    def test_a_refused_save_leaves_the_screen_as_it_is(self):
        """The filters are already applied and the reload already asked for;
        rolling either back would change the grid under somebody looking
        at it. A failure reports, like the sort's does."""
        b, src = self._browser()
        src.save_view_fails = True

        self._page(b)._toggle_filter("unplayed")

        self.assertTrue(b.route["_filters"]["unplayed"])
        self.assertIn("could not be saved", b.status.lower())

    def test_a_tick_while_the_view_is_still_in_flight_keeps_the_real_view(self):
        """The Filter button is live while the first load runs (the shell
        stays; only the tiles blank), so a save can happen before `done`
        has published the view settings. A save that published a DEFAULT
        `_view` in that window would stop `_install` from ever publishing
        the real one -- it only writes where the route has none -- and
        the stored image type and list view would be lost for the rest
        of the session."""
        b, src = self._browser(view_settings=self._stored_view(STORED))
        page = self._page(b)
        b.route["_view"] = None

        page._toggle_filter("unplayed")

        # The save still happened, to the first candidate (key=None).
        self.assertTrue(src.saved_view_settings)
        # And the reload's load re-fetched and installed the real view.
        self.assertEqual(self._stored_view(STORED), b.route["_view"])

    # -- reading -----------------------------------------------------------

    def test_a_stored_filter_is_what_the_next_visit_asks_for(self):
        """The half a scene assertion cannot make."""
        b, src = self._browser(view_settings=self._stored_view(STORED))

        query = src.queries[-1]

        self.assertEqual(STORED, query["filters"])

    def test_an_untouched_library_still_asks_unfiltered(self):
        """The control: a resolver that answered something for everybody
        would pass the test above and filter every library nobody has
        touched."""
        b, src = self._browser()

        self.assertEqual({}, src.queries[-1]["filters"])

    def test_a_route_with_its_own_filters_keeps_them(self):
        """The same rule the sort and the view settings follow: only a
        first load asks the server."""
        b, src = self._browser(
            view_settings=self._stored_view(STORED),
            route_extra={"_filters": {"genre": "Comedy"}})

        self.assertEqual({"genre": "Comedy"}, src.queries[-1]["filters"])

    def test_a_filter_the_source_cannot_apply_is_not_seeded(self):
        """The fallback judgement `_sort_index` makes about a sort this
        library's menu does not offer, on the filter axis: a downloaded
        library seeds nothing it cannot honour, rather than drawing a
        Filter badge for filtering that is not happening."""
        b, src = self._browser(view_settings=self._stored_view(STORED),
                               supported=["genre", "letter"])

        self.assertEqual({"genre": "Action"}, src.queries[-1]["filters"])

    def test_cleared_filters_are_not_resurrected_by_a_reload(self):
        """`is None`, not falsy: an empty dict is a real answer, and a
        falsy test would seed the stored filters back over what the user
        just cleared on every reload."""
        b, src = self._browser(view_settings=self._stored_view(STORED))

        self._page(b)._clear_filters()
        self._page(b)._reload()

        self.assertEqual({}, src.queries[-1]["filters"])

    def test_the_filter_button_counts_the_stored_filters(self):
        """The cheap half, kept honest by the query test above it."""
        b, _src = self._browser(view_settings=self._stored_view(STORED))

        nodes, _h = build_scene(b)

        texts = [n.get("text") for n in nodes if n.get("text")]
        self.assertIn("Filter (2)", texts)


if __name__ == "__main__":
    unittest.main()
