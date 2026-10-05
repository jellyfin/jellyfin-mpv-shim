"""The home screen with more artwork than the decoded cache holds.

Two users with many libraries saw the second row flash between placeholders
and posters for seconds at a time. Every build asked for every poster on the
page, rows far below the fold included; past the 96 MiB budget each build's
fetches evicted what the previous build drew with, and the oldest entries --
the first row with art -- lost every time.

Two fixes, tested apart: only rows near the viewport are built
(`TileRenderer.window_rows`), and what the screen is drawing with cannot be
evicted (`MemoryCache`, once it hears of scene pushes).
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

sys.argv = [sys.argv[0]]      # importing the shell reaches args.get_args()

from jellyfin_mpv_shim.mpvtk.layout import measure  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.app import MpvtkBrowser  # noqa: E402
from jellyfin_mpv_shim.mpvtk_browser.thumbnails import MemoryCache  # noqa: E402
from tests._shell_harness import (  # noqa: E402
    FakeSource, FakeThumbs, _SyncPool, build_scene)

ROWS, PER_ROW = 30, 16


class LruThumbs(FakeThumbs):
    """FakeThumbs reading through a real MemoryCache, one unit per image."""

    def __init__(self, budget):
        super().__init__()
        self.mem = MemoryCache(budget, lambda _v: 1)

    def get_cached(self, key):
        return self.mem.get(key)

    def resolve(self, key, image):
        if image is not None:
            self.mem.put(key, image)
        self._cbs.pop(key)(image)

    def on_scene_pushed(self):
        self.mem.on_scene_pushed()

    def deliver_all(self):
        from PIL import Image

        for key in list(self._cbs):
            self.resolve(key, Image.new("RGB", (8, 12), (90, 90, 90)))


def many_libraries():
    src = FakeSource()
    src.home_rows = []
    src.home_latest_rows = [
        {"title": "Latest %d" % r, "kind": "latest", "slot": r + 1,
         "collection_type": "movies",
         "items": [{"Id": "r%di%d" % (r, i), "Name": "Film %d" % i,
                    "Type": "Movie", "ImageTags": {"Primary": "t"},
                    "PrimaryImageAspectRatio": 2 / 3}
                   for i in range(PER_ROW)]}
        for r in range(ROWS)]
    src.has_poster = True
    # The item id in the url, so a test can tell which row asked.
    src.image_url = (lambda server, item_id, *a, **k:
                     "http://fake/%s/img.jpg" % item_id)
    return src


def home(thumbs):
    b = MpvtkBrowser(app=None, source=many_libraries(), thumbs=thumbs)
    b._pool = _SyncPool()
    b.server = "srv1"
    b.navigate({"kind": "home", "server": "srv1"})
    return b


def requested_rows(thumbs, since=0):
    """Which rows' posters were asked for, by the item ids in the URLs."""
    rows = set()
    for _key, url in thumbs.requests[since:]:
        for r in range(ROWS):
            if "/r%di" % r in url:
                rows.add(r)
    return rows


class TheWindowTest(unittest.TestCase):

    def test_rows_far_below_the_fold_ask_for_nothing(self):
        thumbs = FakeThumbs()
        b = home(thumbs)
        build_scene(b)
        asked = requested_rows(thumbs)
        self.assertIn(0, asked)
        self.assertLess(max(asked), ROWS // 2,
                        "the whole page's posters were asked for")

    def test_scrolling_brings_rows_in(self):
        thumbs = FakeThumbs()
        b = home(thumbs)
        build_scene(b)
        before = len(thumbs.requests)
        # Well down the page. on_scroll is the VScroll's watch; it is what
        # asks for the repaint in the app.
        b._scroll.on_scroll("home", 6000.0, 20000.0)
        build_scene(b)
        self.assertTrue(requested_rows(thumbs, since=before) - {0, 1, 2},
                        "scrolling down built no new rows")

    def test_a_stand_in_is_exactly_the_size_of_its_row(self):
        """window_rows positions rows by measuring the stand-ins, and the
        snap points and the scroll extent come from the same list -- a row
        that grew when it became real would move everything below it."""
        b = home(FakeThumbs())
        tiles = b.tiles
        items = many_libraries().home_latest_rows[0]["items"]
        for geom in (tiles.art.geom, tiles.art.geom_wide,
                     tiles.art.geom_square):
            for n in (1, 5, PER_ROW):
                for see_all in (None, lambda: None):
                    lazy = tiles.tile_row("T", items[:n], "t", geom=geom,
                                          see_all=see_all, lazy=True)
                    real = tiles.tile_row("T", items[:n], "t", geom=geom,
                                          see_all=see_all)
                    self.assertEqual(measure(lazy), measure(real),
                                     (geom, n, see_all))


class NothingOnScreenIsEvictedTest(unittest.TestCase):
    """The loop the report describes, with a budget smaller than even the
    window: without protection each build re-fetches what the last evicted."""

    def test_the_screen_stops_fetching_once_it_is_drawn(self):
        thumbs = LruThumbs(budget=20)
        b = home(thumbs)
        fetched = []
        for _ in range(6):
            before = len(thumbs.requests)
            build_scene(b)
            b._on_scene_pushed()
            thumbs.deliver_all()
            fetched.append(len(thumbs.requests) - before)
        self.assertGreater(fetched[0], 20, "the setup no longer overflows")
        self.assertEqual(fetched[2:], [0, 0, 0, 0],
                         "each build re-fetched what the last one evicted: "
                         "%r" % fetched)


class MemoryCacheProtectionTest(unittest.TestCase):

    def test_what_this_build_touched_survives_an_overflow(self):
        c = MemoryCache(3, lambda _v: 1)
        c.on_scene_pushed()
        for k in "abc":
            c.put(k, k)
        c.on_scene_pushed()
        c.on_scene_pushed()          # a, b, c are two scenes old now
        c.get("a")                   # ...and this build draws with a
        c.put("d", "d")
        c.put("e", "e")
        self.assertEqual(c.get("a"), "a")
        self.assertIsNone(c.get("b"))
        self.assertIsNone(c.get("c"))

    def test_the_budget_holds_again_once_the_screen_moves_on(self):
        c = MemoryCache(2, lambda _v: 1)
        c.on_scene_pushed()
        for k in "abcd":
            c.put(k, k)
        self.assertEqual(len(c), 4, "an in-use entry was evicted")
        for _ in range(3):
            c.on_scene_pushed()
        c.put("e", "e")
        self.assertEqual(len(c), 2)

    def test_without_scene_pushes_it_is_a_plain_lru(self):
        """Nothing says what is on screen, so nothing is protected -- the
        policy every other test of this cache pins."""
        c = MemoryCache(2, lambda _v: 1)
        for k in "abc":
            c.put(k, k)
        self.assertIsNone(c.get("a"))


if __name__ == "__main__":
    unittest.main()
