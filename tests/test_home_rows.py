"""Home-screen loading: fanned out, and asking for only what it renders.

The rows were fetched strictly serially — Continue Watching, then Next Up,
then one /Latest per library — so the home screen cost (2 + N) round trips
end to end before it could draw. jellyfin-web issues the same set
concurrently. The Latest rows also went through the apiclient's
get_recently_added helper, which hardcodes a 28-field payload the row never
renders.
"""

import sys
import threading
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser.repository import (  # noqa: E402
    LIST_FIELDS,
    LibrarySource,
)


class FakeApi:
    """Records every call, with a configurable per-call delay."""

    def __init__(self, delay=0.0, latest_items=None):
        self.delay = delay
        self.calls = []
        self.params = []
        self._lock = threading.Lock()
        self.concurrent = 0
        self.peak_concurrent = 0
        self.latest_items = (latest_items if latest_items is not None
                             else [{"Id": "x", "Name": "Item"}])

    def _enter(self, name, params):
        with self._lock:
            self.calls.append(name)
            self.params.append(params or {})
            self.concurrent += 1
            self.peak_concurrent = max(self.peak_concurrent, self.concurrent)
        if self.delay:
            time.sleep(self.delay)
        with self._lock:
            self.concurrent -= 1

    def user_items(self, handler="", params=None):
        self._enter("user_items%s" % (handler or ""), params)
        if handler == "/Latest":
            return list(self.latest_items)
        return {"Items": [{"Id": "r", "Name": "Resume"}]}

    def get_next(self, limit=1, fields=None, enable_image_types=None):
        self._enter("get_next", {"Fields": fields,
                                 "EnableImageTypes": enable_image_types})
        return {"Items": [{"Id": "n", "Name": "NextUp"}]}

    def get_recently_added(self, *a, **kw):     # must not be used any more
        raise AssertionError(
            "get_recently_added hardcodes Fields=info() — a 28-field payload "
            "including MediaSources/People that the home row never renders")


LIBS = [
    {"Id": "l1", "Name": "Movies", "CollectionType": "movies"},
    {"Id": "l2", "Name": "Shows", "CollectionType": "tvshows"},
    {"Id": "l3", "Name": "Mixes", "CollectionType": "playlists"},
]


class HomeRowsHarness(unittest.TestCase):
    def _source(self, api):
        src = LibrarySource.__new__(LibrarySource)
        src._conn = lambda _uuid: type("C", (), {"api": api})()
        return src


class FanOutTest(HomeRowsHarness):
    def test_rows_are_fetched_concurrently(self):
        api = FakeApi(delay=0.2)
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)

        self.assertGreater(api.peak_concurrent, 1,
                           "the home rows are still fetched one at a time")
        self.assertTrue(rows)

    def test_wall_clock_is_one_wave_not_a_sum(self):
        api = FakeApi(delay=0.2)
        started = time.time()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        elapsed = time.time() - started
        # Four calls (resume, next-up, two non-playlist libraries) at 0.2s.
        # Serial would be 0.8s.
        self.assertLess(elapsed, 0.5,
                        "the rows were walked rather than fanned out")

    def test_row_order_survives_the_fan_out(self):
        """Collected in submit order, so rows do not shuffle by whichever
        server call happens to answer first."""
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        titles = [r["title"] for r in rows]
        self.assertEqual(titles[0], "Continue Watching")
        self.assertEqual(titles[1], "Next Up")
        self.assertIn("Movies", titles[2])
        self.assertIn("Shows", titles[3])

    def test_one_failing_row_does_not_lose_the_others(self):
        api = FakeApi()
        calls = {"n": 0}
        original = api.user_items

        def flaky(handler="", params=None):
            if handler == "/Latest":
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("server hiccup")
            return original(handler, params)

        api.user_items = flaky
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        titles = [r["title"] for r in rows]
        self.assertIn("Continue Watching", titles)
        self.assertIn("Next Up", titles)
        self.assertEqual(len(titles), 3, "a dead row cost the whole screen")

    def test_playlist_libraries_get_no_latest_row(self):
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        self.assertFalse(any("Mixes" in r["title"] for r in rows))

    def test_empty_rows_are_dropped(self):
        api = FakeApi(latest_items=[])
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        self.assertFalse(any("Latest" in r["title"] for r in rows))


class LeanFieldsTest(HomeRowsHarness):
    def _latest_params(self, api):
        return [p for name, p in zip(api.calls, api.params)
                if name == "user_items/Latest"]

    def test_latest_does_not_use_the_heavy_helper(self):
        """FakeApi.get_recently_added raises: reaching it is the failure."""
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        self.assertIn("user_items/Latest", api.calls)

    def test_latest_asks_only_for_the_fields_it_renders(self):
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        for params in self._latest_params(api):
            self.assertEqual(params.get("Fields"), LIST_FIELDS)
            self.assertNotIn("MediaSources", params.get("Fields", ""))
            self.assertNotIn("People", params.get("Fields", ""))

    def test_home_queries_skip_the_total_record_count(self):
        """Each row is capped, so a separate COUNT(*) over the library is
        pure waste — jellyfin-web passes this too."""
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        counted = [p for p in api.params
                   if "Limit" in p and p.get("EnableTotalRecordCount") is not False]
        self.assertEqual(counted, [],
                         "a home query still asks for a total record count")

    def test_image_tags_are_capped_to_one_per_type(self):
        """Without this every backdrop tag comes back, often five to ten.

        Scoped to the queries we build ourselves. Next Up goes through the
        apiclient's get_next helper, whose signature has no ImageTypeLimit
        parameter — capping it there would mean bypassing the helper for a
        single-row saving, which is not worth the extra surface.
        """
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        checked = 0
        for name, params in zip(api.calls, api.params):
            if name.startswith("user_items") and params.get("EnableImageTypes"):
                self.assertEqual(params.get("ImageTypeLimit"), 1)
                checked += 1
        self.assertGreater(checked, 0, "the assertion matched nothing")

    def test_next_up_asks_for_the_lean_fields_too(self):
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        nextup = [p for name, p in zip(api.calls, api.params)
                  if name == "get_next"][0]
        self.assertEqual(nextup.get("Fields"), LIST_FIELDS)


if __name__ == "__main__":
    unittest.main()


class SectionsTest(HomeRowsHarness):
    """_load_home fetches in two batches so first paint is not gated on the
    per-library Latest rows, which are one request each and below the fold."""

    def test_primary_fetches_only_the_above_the_fold_rows(self):
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS,
                                               sections=("primary",))
        self.assertEqual([r["title"] for r in rows],
                         ["Continue Watching", "Next Up"])
        self.assertNotIn("user_items/Latest", api.calls,
                         "the first batch waited on the Latest fan-out")

    def test_latest_fetches_only_the_library_rows(self):
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS,
                                               sections=("latest",))
        self.assertTrue(all("Latest" in r["title"] for r in rows))
        self.assertNotIn("get_next", api.calls)

    def test_the_two_batches_reconstruct_the_whole_page(self):
        api = FakeApi()
        src = self._source(api)
        both = [r["title"] for r in src.get_home_rows("srv", libraries=LIBS)]
        split = [r["title"] for r in
                 src.get_home_rows("srv", LIBS, sections=("primary",))]
        split += [r["title"] for r in
                  src.get_home_rows("srv", LIBS, sections=("latest",))]
        self.assertEqual(both, split,
                         "splitting the fetch changed the page")

    def test_an_unknown_section_asks_for_nothing(self):
        api = FakeApi()
        self.assertEqual(
            self._source(api).get_home_rows("srv", LIBS, sections=()),
            [])
        self.assertEqual(api.calls, [])


class OfflineSignatureParityTest(unittest.TestCase):
    """The offline source is what a failed home load falls back TO.

    If it cannot accept the same call _load_home makes, the fallback itself
    raises — and the offline home screen never loads at all. Signature parity
    is load-bearing, not tidiness.
    """

    def test_offline_accepts_the_same_call_as_the_live_source(self):
        import inspect

        from jellyfin_mpv_shim.mpvtk_browser.repository import (
            LibrarySource, OfflineLibrarySource)

        live = inspect.signature(LibrarySource.get_home_rows).parameters
        offline = inspect.signature(
            OfflineLibrarySource.get_home_rows).parameters
        self.assertEqual(set(live), set(offline),
                         "the offline fallback cannot answer the call "
                         "_load_home makes")
