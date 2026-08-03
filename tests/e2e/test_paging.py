"""Virtual scrolling over a thousand items, at real totals.

`#617` replaced append-on-approach with a true virtual scroll, and its own
hand-testing checklist says why this wants a real server:

    The tests use a synchronous pool, so pages arrive instantly and in order.
    Neither is true against a real server.

and, under **Least confident**:

    The thumbnail-request pressure from free scrollbar dragging ... only
    misbehaves at scale. If there is time for two things, those.

stdjflib's `Bulk *` libraries are ~1000 items each and exist for exactly this.
What they buy over the fake is a real `_total`: the fake's grids are dozens of
items, so a windowed list and a fully-loaded one look identical and the
arithmetic that separates them is never exercised.

The sharpest test here is `test_the_item_at_an_index_is_that_index`. A
windowed list is `_total` long and mostly holes, and an item's position in it
**is** its position in the library — that is what lets the scrollbar be the
right length before anything is fetched. Nothing in a fake-based test can
check that mapping is true, because the fake decides both sides of it. Asking
for `start_index=N, limit=1` and comparing can.

One fixture property worth knowing before writing anything here: **every bulk
item is created by the same scan**, so their `DateCreated` values tie and the
server falls back to name order. "Date Added" and "Name" return the identical
first item, which makes a resort look like a refetch that never happened.
"Release Date" genuinely reorders.
"""

import logging
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _e2e  # noqa: E402

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

SIZE = (1280, 720)
LIBRARY = "Bulk Movies"


class _SyncPool:
    def submit(self, fn, *a, **k):
        fn(*a, **k)

    def shutdown(self, *a, **k):
        pass


@_e2e.require_server
class WindowedGridTest(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser
        from jellyfin_mpv_shim.mpvtk_browser.pagination import Paginator
        from jellyfin_mpv_shim.mpvtk.layout import layout
        cls.MpvtkBrowser = MpvtkBrowser
        cls.Paginator = Paginator
        cls.layout = staticmethod(layout)
        cls.session = _e2e.Session()
        cls.source = cls.session.library_source()
        libs = cls.source.get_libraries(_e2e.SOURCE_UUID)
        match = [lib for lib in libs if lib["Name"] == LIBRARY]
        if not match:
            cls.source.stop()
            cls.session.stop()
            raise unittest.SkipTest(
                "%r is not on this server — build the library with the bulk "
                "tiers, or these tests are about nothing" % LIBRARY)
        cls.library = match[0]

    @classmethod
    def tearDownClass(cls):
        try:
            cls.source.stop()
        finally:
            cls.session.stop()

    def setUp(self):
        self.browser = self.MpvtkBrowser(
            app=None, source=self.source, server_uuid=_e2e.SOURCE_UUID)
        # See test_route_walk: __init__ starts the home load on the real
        # threaded pool, so drain it before making everything inline.
        self.browser._async._pool.shutdown(wait=True, cancel_futures=True)
        self.browser._pool = _SyncPool()
        self.browser._load_route(self.browser.route)
        self.addCleanup(self._shutdown)
        self.browser.navigate({
            "kind": "grid", "server": _e2e.SOURCE_UUID,
            "parent_id": self.library["Id"], "title": LIBRARY,
            "collection_type": self.library.get("CollectionType")})
        self.route = self.browser.route
        self._render()

    def _shutdown(self):
        try:
            self.browser.shutdown()
        except Exception:
            pass

    def _render(self):
        nodes, _handlers = self.layout(self.browser.build(SIZE), *SIZE)
        return nodes

    def _scroll_to(self, offset, maximum):
        """Move the scroller and let the render ask for what that brings in.

        The same two steps the view's own on_scroll does — rewindow, then
        render, because the render is what computes the visible range from
        the geometry it is about to composite.
        """
        self.Paginator.rewindow(self.route)
        self.browser._scroll.on_scroll("grid", offset, maximum)
        return self._render()

    def _items(self):
        return self.route.get("_items") or []

    def _filled(self):
        return [i for i, item in enumerate(self._items()) if item is not None]

    # -- the tests ---------------------------------------------------------

    def test_the_total_is_the_servers_not_what_is_loaded(self):
        """`f18088aa` — the scrollbar is full length from the first frame.

        A list sized by what has loaded grows as pages land, so the thumb
        shrinks and jumps under the cursor while you drag it.
        """
        total = self.route.get("_total")
        self.assertTrue(total, "the grid recorded no total")
        self.assertGreater(
            total, 500,
            "%r has only %s items, which is too few for windowing to differ "
            "from loading everything — these tests need the bulk library"
            % (LIBRARY, total))
        self.assertEqual(
            len(self._items()), total,
            "the item list is not the library's length, so the scrollbar "
            "cannot be the right size before everything is fetched")
        self.assertLess(
            len(self._filled()), total,
            "the whole library was loaded up front, which is the thing "
            "windowing exists to avoid")

    def test_jumping_to_the_middle_does_not_walk_from_the_top(self):
        """#617's core promise, and the drag the checklist calls the one to
        watch: parts of the library are reachable without loading everything
        in between."""
        total = self.route.get("_total")
        before = len(self._filled())
        self._scroll_to(20000, 40000)
        filled = self._filled()

        self.assertGreater(len(filled), before,
                           "scrolling to the middle loaded nothing new")
        self.assertGreater(
            max(filled), total // 4,
            "nothing from the middle of the library was fetched")
        # The point: far fewer items are loaded than the index reached. A
        # walk from the top would have filled every slot up to it.
        self.assertLess(
            len(filled), max(filled),
            "every slot up to the furthest one is loaded, so this walked the "
            "library from the top instead of windowing to the target")

    def test_the_item_at_an_index_is_that_index(self):
        """The mapping the whole design rests on, checked against the server.

        An item's position in `_items` is its position in the library. A fake
        cannot test this — it decides both sides. The server can be asked
        independently, with the same sort the grid requests.
        """
        self._scroll_to(20000, 40000)
        filled = self._filled()
        middle = [i for i in filled if i > 200]
        if not middle:
            self.skipTest("nothing beyond index 200 was fetched")
        index = middle[len(middle) // 2]

        got = self._items()[index]
        # Through the source's own query, deliberately. The grid's fetch is
        # typed and recursive from the collection type
        # (LIBRARY_ITEM_TYPES), and a hand-rolled query that omits that
        # describes a DIFFERENT set — index 500 came back "Midnight Yard"
        # untyped against the grid's "Midnight Zenith", which reads as an
        # off-by-something in the shim and is not one. What is under test
        # here is the window *placement*: slot N must hold what the same
        # query returns at start_index=N. Query construction is a separate
        # concern, covered by test_source_conformance.
        expected, _total = self.source.get_library_items(
            _e2e.SOURCE_UUID, self.library["Id"], start_index=index, limit=1,
            collection_type=self.library.get("CollectionType"))
        self.assertTrue(expected, "the server has no item at index %d" % index)
        self.assertEqual(
            got.get("Id"), expected[0].get("Id"),
            "slot %d holds %r but the library's item %d is %r — the window "
            "was placed at the wrong offset, which draws the right number of "
            "tiles with the wrong things in them"
            % (index, got.get("Name"), index, expected[0].get("Name")))

    def test_scrolling_back_does_not_lose_what_was_loaded(self):
        """Repeated drags are the checklist's "fast repeated drags" case.

        Each new window asks for a screenful or three of artwork, and the
        worry is thrash: a window that discards what it already had makes
        every drag a fresh round trip.
        """
        self._scroll_to(20000, 40000)
        far = set(self._filled())
        self._scroll_to(0, 40000)
        back = set(self._filled())
        self.assertTrue(
            far <= back,
            "scrolling back dropped %d slots that were already loaded, so "
            "coming back re-fetches them" % len(far - back))

    def test_a_resort_starts_over_rather_than_mixing_two_orders(self):
        """`4b0e3afd`'s neighbour: a refetch must drop the loaded list.

        Keeping it would leave slots filled from the previous sort — the
        right number of tiles, in two different orders, with no way to tell.
        """
        self._scroll_to(20000, 40000)
        self.assertGreater(len(self._filled()), 100)

        first_id = next((i.get("Id") for i in self._items() if i is not None),
                        None)

        # A route stores its sort as an INDEX into the page's SORTS table,
        # not as sort_by/sort_order keys — passing those to navigate() does
        # nothing at all and the grid comes back in the same order, which
        # looks like the refetch failing to take.
        # PremiereDate, not DateCreated. Every bulk item is created by the
        # same scan, so their DateCreated values tie and the server falls
        # back to name order — "Date Added" returns the identical first item
        # as "Name" and the test reads as a refetch that did not take.
        # Measured: SortName/Asc and DateCreated/Desc both start with
        # "!Exclamation First 00295"; PremiereDate/Desc starts with
        # "Ålesund Nordic 00369".
        from jellyfin_mpv_shim.mpvtk_browser.pages.grid import SORTS
        by_release = next(i for i, (_l, by, _o) in enumerate(SORTS)
                          if by == "PremiereDate")
        self.route["_sort"] = by_release
        for key in ("_items", "_total", "_win_tried", "_win_load"):
            self.route.pop(key, None)
        self.browser._load_route(self.route)
        self._render()

        items = self._items()
        self.assertTrue(items, "the resorted grid has no items")
        new_first = next((i for i in items if i is not None), None)
        self.assertIsNotNone(new_first, "nothing loaded after the resort")
        self.assertNotEqual(
            new_first.get("Id"), first_id,
            "the first item is unchanged after switching from Name to "
            "Release Date, so the grid is still showing the previous order")
        self.assertLess(
            len(self._filled()), len(items),
            "the resorted grid came back fully loaded rather than windowed")


if __name__ == "__main__":
    unittest.main()
