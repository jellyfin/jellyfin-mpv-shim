"""Home-screen loading: fanned out, and asking for only what it renders.

The rows were fetched strictly serially — Continue Watching, then Next Up,
then one /Latest per library — so the home screen cost (2 + N) round trips
end to end before it could draw. jellyfin-web issues the same set
concurrently. The Latest rows also took get_recently_added's default field
set, a 28-field payload the row never renders.
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
import threading
import time
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

from jellyfin_mpv_shim.mpvtk_browser import home_sections as hs  # noqa: E402
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

    # Deliberately NO get_resume_items. The apiclient has one and it goes
    # to the generic item query, which does not apply the user's library
    # exclusions (#703) -- so a fake that answered it would let the
    # regression back in green. Production reaches the endpoint directly.
    def _get(self, handler, params=None):
        self._enter(handler, params)
        if handler == "UserItems/Resume":
            return {"Items": [{"Id": "r", "Name": "Resume"}]}
        raise AssertionError("unexpected endpoint: %s" % handler)

    def get_recently_added(self, **kwargs):
        self._enter("get_recently_added", kwargs)
        # /Latest answers with a bare list, not an Items envelope.
        return list(self.latest_items)

    def get_next(self, limit=1, fields=None, enable_image_types=None,
                 image_type_limit=None):
        self._enter("get_next", {"fields": fields,
                                 "enable_image_types": enable_image_types,
                                 "image_type_limit": image_type_limit})
        return {"Items": [{"Id": "n", "Name": "NextUp"}]}


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
        server call happens to answer first.

        The order is the default section layout's: Continue Watching,
        Continue Listening, Continue Reading, Next Up, then the per-library
        Latest rows.
        """
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        titles = [r["title"] for r in rows]
        self.assertEqual(titles[0], "Continue Watching")
        self.assertEqual(titles[1], "Continue Listening")
        self.assertEqual(titles[2], "Continue Reading")
        self.assertEqual(titles[3], "Next Up")
        self.assertIn("Movies", titles[4])
        self.assertIn("Shows", titles[5])

    def test_one_failing_row_does_not_lose_the_others(self):
        api = FakeApi()
        calls = {"n": 0}
        original = api.get_recently_added

        def flaky(**kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("server hiccup")
            return original(**kwargs)

        api.get_recently_added = flaky
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        titles = [r["title"] for r in rows]
        self.assertIn("Continue Watching", titles)
        self.assertIn("Next Up", titles)
        # 4 primary rows + 2 library Latest rows, one of which died.
        self.assertEqual(len(titles), 5, "a dead row cost the whole screen")

    def test_playlist_libraries_get_no_latest_row(self):
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        self.assertFalse(any("Mixes" in r["title"] for r in rows))

    def test_empty_rows_are_dropped(self):
        api = FakeApi(latest_items=[])
        rows = self._source(api).get_home_rows("srv", libraries=LIBS)
        self.assertFalse(any("Latest" in r["title"] for r in rows))


class LayoutTest(HomeRowsHarness):
    """The section layout drives which rows are fetched and in what order.

    Ordering matters across the two fetch batches: the user can put Recently
    Added above Continue Watching, so the caller merges by slot rather than
    concatenating primary + latest.
    """

    def test_layout_order_is_reflected_in_slots(self):
        api = FakeApi()
        layout = [hs.NEXT_UP, hs.RESUME] + [hs.NONE] * 8
        rows = self._source(api).get_home_rows("srv", libraries=LIBS,
                                               layout=layout)
        by_slot = sorted(rows, key=lambda r: r["slot"])
        self.assertEqual([r["title"] for r in by_slot],
                         ["Next Up", "Continue Watching"])

    def test_latest_above_resume_still_merges_in_order(self):
        """The regression the slot key exists for: concatenating the primary
        and latest batches would put Continue Watching first regardless."""
        api = FakeApi()
        src = self._source(api)
        layout = [hs.LATEST, hs.RESUME] + [hs.NONE] * 8
        primary = src.get_home_rows("srv", LIBS, sections=("primary",),
                                    layout=layout)
        latest = src.get_home_rows("srv", LIBS, sections=("latest",),
                                   layout=layout)
        merged = sorted(primary + latest, key=lambda r: r["slot"])
        self.assertIn("Recently Added in", merged[0]["title"])
        self.assertEqual(merged[-1]["title"], "Continue Watching")

    def test_sections_not_in_the_layout_are_not_fetched(self):
        api = FakeApi()
        rows = self._source(api).get_home_rows(
            "srv", libraries=LIBS, layout=[hs.RESUME] + [hs.NONE] * 9)
        self.assertEqual([r["title"] for r in rows], ["Continue Watching"])
        self.assertNotIn("get_next", api.calls)
        self.assertNotIn("get_recently_added", api.calls)

    def test_unsupported_sections_fetch_nothing(self):
        """A recognised but undrawable section must not turn into a request
        or an empty row.

        Live TV and Active Recordings are here because this FakeApi has no
        tuner, which is the gate they are behind; LIBRARY_BUTTONS is the one
        type the shim recognises and will not draw. (Books used to be in
        this list and are now a real row -- see test_home_sections for why
        the example keeps moving.)"""
        api = FakeApi()
        rows = self._source(api).get_home_rows(
            "srv", libraries=LIBS,
            layout=[hs.LIVE_TV, hs.LIBRARY_BUTTONS, hs.ACTIVE_RECORDINGS]
                   + [hs.NONE] * 7)
        self.assertEqual(rows, [])
        self.assertEqual(api.calls, [])

    def test_libraries_section_costs_no_request(self):
        """The Libraries row is rendered from get_libraries, which the loader
        already holds — it must not add a fetch task."""
        api = FakeApi()
        rows = self._source(api).get_home_rows(
            "srv", libraries=LIBS, layout=[hs.LIBRARIES] + [hs.NONE] * 9)
        self.assertEqual(rows, [])
        self.assertEqual(api.calls, [])

    def test_resume_audio_asks_for_audio_media(self):
        api = FakeApi()
        self._source(api).get_home_rows(
            "srv", libraries=LIBS, layout=[hs.RESUME_AUDIO] + [hs.NONE] * 9)
        params = api.params[0]
        self.assertEqual(params.get("MediaTypes"), "Audio")
        self.assertNotIn("IncludeItemTypes", params)

    def test_the_resume_rows_go_to_the_resume_endpoint(self):
        """#703. The user's per-library "Display in home screen sections"
        exclusion is applied by ``ItemsController.GetResumeItems`` and by
        nothing else -- so which route is asked *is* the feature, and the
        apiclient's ``get_resume_items`` is not it (it sends
        ``Users/{uid}/Items?Filters=IsResumable``, measured against a 12.0
        server as ignoring the exclusion entirely).

        All three rows, because they are three calls to one helper and the
        rule is per-call."""
        api = FakeApi()
        self._source(api).get_home_rows(
            "srv", libraries=LIBS,
            layout=[hs.RESUME, hs.RESUME_AUDIO, hs.RESUME_BOOK]
                   + [hs.NONE] * 7)
        self.assertEqual(api.calls, ["UserItems/Resume"] * 3)

    def test_resume_rows_never_carry_a_parent_id(self):
        """The other half of the same exclusion: the Resume handler applies
        it only to a query with no ParentId, so these rows must not scope by
        library. Being on the right endpoint is not enough."""
        api = FakeApi()
        self._source(api).get_home_rows(
            "srv", libraries=LIBS,
            layout=[hs.RESUME, hs.RESUME_AUDIO] + [hs.NONE] * 8)
        for params in api.params:
            self.assertIsNone(params.get("ParentId"))


class LatestExcludesTest(HomeRowsHarness):
    """Recently Added is one request per library, and passing ParentId
    bypasses the server's own exclusion handling — so it is applied here."""

    def _latest_titles(self, api, excludes):
        rows = self._source(api).get_home_rows(
            "srv", libraries=LIBS, sections=("latest",),
            latest_excludes=excludes)
        return [r["title"] for r in rows]

    def test_excluded_libraries_get_no_latest_row(self):
        api = FakeApi()
        titles = self._latest_titles(api, {"l1"})
        self.assertFalse(any("Movies" in t for t in titles))
        self.assertTrue(any("Shows" in t for t in titles))

    def test_excluded_libraries_are_not_even_requested(self):
        """Filtering the response instead of the task list would still pay
        the round trip."""
        api = FakeApi()
        self._latest_titles(api, {"l1", "l2"})
        self.assertNotIn("get_recently_added", api.calls)

    def test_no_excludes_keeps_every_library(self):
        api = FakeApi()
        self.assertEqual(len(self._latest_titles(api, None)), 2)


class LeanFieldsTest(HomeRowsHarness):
    def _latest_params(self, api):
        return [p for name, p in zip(api.calls, api.params)
                if name == "get_recently_added"]

    def test_latest_overrides_the_default_field_set(self):
        """get_recently_added defaults to info(), a 28-field payload including
        MediaSources/People that the home row never renders — the row has to
        ask for its own fields."""
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        self.assertIn("get_recently_added", api.calls)
        for params in self._latest_params(api):
            self.assertIsNotNone(params.get("fields"))

    def test_latest_asks_only_for_the_fields_it_renders(self):
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        for params in self._latest_params(api):
            self.assertEqual(params.get("fields"), LIST_FIELDS)
            self.assertNotIn("MediaSources", params.get("fields", ""))
            self.assertNotIn("People", params.get("fields", ""))

    def test_home_queries_skip_the_total_record_count(self):
        """Each row is capped, so a separate COUNT(*) over the library is
        pure waste — jellyfin-web passes this too."""
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        counted = [p for p in api.params
                   if "limit" in p
                   and p.get("enable_total_record_count") is not False]
        self.assertEqual(counted, [],
                         "a home query still asks for a total record count")

    def test_image_tags_are_capped_to_one_per_type(self):
        """Without this every backdrop tag comes back, often five to ten.

        Scoped to the queries we build ourselves. Next Up goes through the
        apiclient's get_next helper, whose signature has no image_type_limit
        parameter — capping it there would mean bypassing the helper for a
        single-row saving, which is not worth the extra surface.
        """
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        checked = 0
        for name, params in zip(api.calls, api.params):
            if name != "get_next" and params.get("enable_image_types"):
                self.assertEqual(params.get("image_type_limit"), 1)
                checked += 1
        self.assertGreater(checked, 0, "the assertion matched nothing")

    def test_next_up_asks_for_the_lean_fields_too(self):
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        nextup = [p for name, p in zip(api.calls, api.params)
                  if name == "get_next"][0]
        self.assertEqual(nextup.get("fields"), LIST_FIELDS)

    def test_next_up_caps_image_tags_like_every_other_home_query(self):
        """It was the one that did not, so a series with twenty backdrops
        sent twenty tags per card for the one the tile draws."""
        api = FakeApi()
        self._source(api).get_home_rows("srv", libraries=LIBS)
        nextup = [p for name, p in zip(api.calls, api.params)
                  if name == "get_next"][0]
        self.assertEqual(nextup.get("image_type_limit"), 1)


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
                         ["Continue Watching", "Continue Listening",
                          "Continue Reading", "Next Up"])
        self.assertNotIn("get_recently_added", api.calls,
                         "the first batch waited on the Latest fan-out")

    def test_latest_fetches_only_the_library_rows(self):
        api = FakeApi()
        rows = self._source(api).get_home_rows("srv", libraries=LIBS,
                                               sections=("latest",))
        self.assertTrue(all("Recently Added in" in r["title"]
                            for r in rows))
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


class ContinueWatchingStaysCurrentTest(unittest.TestCase):
    """#560: Continue Watching went stale. Removing a film from it in a
    browser left it on the shim's home screen offering to resume something
    already dealt with -- and so did finishing something in the shim itself,
    because Home only re-read on a Back press."""

    def _browser(self):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import FakeController, FakeSource, _SyncPool

        b = MpvtkBrowser(app=None, source=FakeSource(),
                         controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "home", "server": "srv1"})
        b._browsing = True
        loads = []
        b._load_route = lambda route, epoch=None: loads.append(route)
        return b, loads

    def test_the_event_is_bound_and_reaches_the_hook(self):
        from jellyfin_mpv_shim.event_handler import EventHandler, bindings

        self.assertIn("UserDataChanged", bindings)
        handler = EventHandler()
        seen = []
        handler.user_data_changed = lambda client: seen.append(client)
        handler.handle_event("client", "UserDataChanged", {})
        self.assertEqual(seen, ["client"])

    def test_a_broken_hook_does_not_kill_the_websocket_thread(self):
        from jellyfin_mpv_shim.event_handler import EventHandler

        handler = EventHandler()

        def boom(_client):
            raise OSError("no")

        handler.user_data_changed = boom
        handler.handle_event("client", "UserDataChanged", {})  # must not raise

    def test_a_burst_of_events_costs_one_re_read(self):
        """The server sends one per progress report, including for our own
        playback, so an undebounced hook would refetch Home every few
        seconds behind a film."""
        b, loads = self._browser()
        b.USERDATA_DEBOUNCE = 0.01
        for _ in range(20):
            b.refresh_home()
        for _ in range(200):
            if loads:
                break
            time.sleep(0.01)
        self.assertEqual(len(loads), 1, loads)

    def test_nothing_happens_off_the_home_screen(self):
        b, loads = self._browser()
        b.route["kind"] = "grid"
        b.refresh_home()
        b.refresh_home(now=True)
        self.assertEqual(loads, [])

    def test_nothing_happens_while_playback_owns_the_window(self):
        b, loads = self._browser()
        b._browsing = False
        b.refresh_home()
        self.assertEqual(loads, [])

    def test_an_open_menu_defers_it(self):
        """A refresh nobody asked for must not move what someone is acting
        on -- the rule refresh_live_tv established."""
        b, loads = self._browser()
        b._menu = {"kind": "history"}
        b.refresh_home(now=True)
        self.assertEqual(loads, [])
        b._menu = None
        b._dialog = lambda: None
        b.refresh_home(now=True)
        self.assertEqual(loads, [])

    def _live_browser(self):
        """Like ``_browser``, but with the real loader left in place: these
        are about what ``HomePage.load`` publishes, not about who calls it."""
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from tests._shell_harness import FakeController, FakeSource, _SyncPool

        src = FakeSource()
        b = MpvtkBrowser(app=None, source=src, controller=FakeController())
        b._pool = _SyncPool()
        b.server = "srv1"
        b.navigate({"kind": "home", "server": "srv1"})
        b._browsing = True
        return b, src

    def test_an_unchanged_re_read_does_not_republish(self):
        """A UserDataChanged burst re-reads this screen every few seconds and
        most of those change nothing it draws. Rebuilding the tree for that
        costs a full home layout, with the rows on screen while it happens.
        """
        b, _src = self._live_browser()
        first = b.route["_data"]
        self.assertIsNotNone(first, "home never loaded")
        b._load_route(b.route)
        self.assertIs(b.route["_data"], first,
                      "an identical re-read replaced the rows anyway")

    def test_a_changed_re_read_does_republish(self):
        """The other direction, or the check above is just a way of never
        updating anything."""
        b, src = self._live_browser()
        first = b.route["_data"]
        src.libraries = list(src.libraries) + [
            {"Id": "libnew", "Name": "New", "CollectionType": "movies"}]
        b._load_route(b.route)
        self.assertIsNot(b.route["_data"], first,
                         "a genuine change was swallowed")

    def test_a_refresh_does_not_take_the_latest_rows_away_first(self):
        """load() publishes a primary-only batch to get first paint up. On a
        refresh that would REMOVE the Latest rows for the length of one
        request per library before putting them back -- more visible than the
        spinner "load, not reload" exists to avoid.

        Sampled from inside the *latest* fetch, which is the one moment the
        partial publish is on screen.
        """
        b, src = self._live_browser()
        before = len(b.route["_data"]["rows"])
        self.assertGreater(before, 1, "the fixture has no Latest row to lose")
        mid = []
        real = src.get_home_rows

        def watched(server, libraries=None, sections=None, **kw):
            if sections and "latest" in sections:
                mid.append(len((b.route.get("_data") or {}).get("rows") or []))
            return real(server, libraries, sections=sections, **kw)

        src.get_home_rows = watched
        b._load_route(b.route)
        self.assertEqual(mid, [before],
                         "the Latest rows were taken away mid-refresh")

    def test_returning_from_playback_re_reads_immediately(self):
        """The local half: go_back has always re-read Home, but coming back
        from playback does not go through it."""
        b, loads = self._browser()
        b.enter_browse()
        self.assertEqual(loads, [b.route])


class LatestSeeAllSortTest(unittest.TestCase):
    """Where a Latest heading lands, per library kind.

    A Latest row on a TV library is the server's episode-grouped list --
    shows ordered by when each last gained an episode -- and "Date Added"
    on the destination orders those same shows by when the SERIES was
    created. Same items, different order, which is what the row's own
    heading promised it was showing more of (#688).

    Asserted as the resolved ``(SortBy, SortOrder)`` rather than as an
    index, because the index is the incidental half: it is a position in
    whichever list that library's screen offers, and the tables it comes
    from are edited by hand.
    """

    def _sort(self, collection_type):
        from jellyfin_mpv_shim.mpvtk_browser.pages.grid import sorts_for
        from jellyfin_mpv_shim.mpvtk_browser.pages.home import HomePage

        index = HomePage._latest_sort(collection_type)
        return sorts_for(collection_type)[index][1:]

    def test_a_tv_library_lands_on_date_episode_added(self):
        self.assertEqual(self._sort("tvshows"),
                         ("DateLastContentAdded", "Descending"))

    def test_everything_else_lands_on_date_added(self):
        for collection_type in ("movies", "music", "books", "homevideos",
                                "musicvideos", "mixed", None, ""):
            with self.subTest(collection_type=collection_type):
                self.assertEqual(self._sort(collection_type),
                                 ("DateCreated", "Descending"))

    def test_a_collection_type_nobody_has_heard_of_still_sorts(self):
        """A server growing a new collection type must not land on Name."""
        self.assertEqual(self._sort("holotapes"), ("DateCreated", "Descending"))

    def test_the_index_is_looked_up_rather_than_counted(self):
        """Inserting a base sort must not re-point the TV destination.

        The failure this guards is silent: ``EXTRA_SORTS`` is appended to
        ``SORTS``, so any arithmetic on ``len(SORTS)`` keeps returning a
        valid index that now names a different sort.
        """
        from jellyfin_mpv_shim.mpvtk_browser.pages import grid

        original = grid.SORTS
        try:
            grid.SORTS = [("Injected", "SomethingElse", "Ascending")] + original
            self.assertEqual(self._sort("tvshows"),
                             ("DateLastContentAdded", "Descending"))
            self.assertEqual(self._sort("movies"), ("DateCreated", "Descending"))
        finally:
            grid.SORTS = original
