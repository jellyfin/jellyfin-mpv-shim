"""Unit tests for the mpvtk browser's strip compositor (mpvtk_browser.strips).
Headless (PIL only) — covers geometry/regions, content-keyed caching and
recompositing on decoration/poster changes, LRU eviction, and both the
file and in-memory storage backends.
"""

import os
import struct
import shutil
import tempfile
import threading
import unittest

from PIL import Image, ImageDraw

from jellyfin_mpv_shim.mpvtk.rawimage import MemoryStore
from jellyfin_mpv_shim.mpvtk_browser import theme
from jellyfin_mpv_shim.mpvtk_browser.strips import StripStore, Tile, TileGeom


def _poster(color=(120, 30, 30), size=(140, 210)):
    return Image.new("RGB", size, color)


class TestStripStore(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-strips-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _store(self, mem=False, geom=None):
        return StripStore(
            cache_dir=None if mem else self.tmp,
            mem_store=MemoryStore() if mem else None,
            geom=geom,
        )

    def test_regions_and_dimensions(self):
        s = self._store()
        g = TileGeom()
        tiles = [Tile(key="a", title="A", poster=_poster()),
                 Tile(key="b", title="B", poster=_poster())]
        out = s.strip(tiles)
        self.assertEqual(out["ih"], g.strip_h)
        self.assertEqual(out["iw"], 2 * g.tile_w + g.gap)
        self.assertEqual([r["key"] for r in out["regions"]], ["a", "b"])
        # second region starts a tile+gap over
        self.assertEqual(out["regions"][1]["x"], g.tile_w + g.gap)
        self.assertEqual(out["regions"][0]["h"], g.strip_h)

    def test_content_key_cache_hit(self):
        s = self._store()
        tiles = [Tile(key="a", title="A", poster=_poster(), poster_tag="p1")]
        a = s.strip(tiles)
        b = s.strip([Tile(key="a", title="A", poster=_poster(),
                          poster_tag="p1")])
        self.assertEqual(s.hits, 1)
        self.assertEqual(a["src"], b["src"])

    def test_decoration_change_recomposites(self):
        s = self._store()
        base = dict(key="a", title="A", poster=_poster(), poster_tag="p1")
        a = s.strip([Tile(**base)])
        b = s.strip([Tile(**dict(base, watched=True))])
        c = s.strip([Tile(**dict(base, progress=0.5))])
        srcs = {a["src"], b["src"], c["src"]}
        self.assertEqual(len(srcs), 3, "each decoration set is a distinct strip")
        self.assertEqual(s.misses, 3)

    def test_poster_arrival_recomposites(self):
        s = self._store()
        # No poster yet (placeholder), then the real poster lands.
        a = s.strip([Tile(key="a", title="A", poster=None, poster_tag="")])
        b = s.strip([Tile(key="a", title="A", poster=_poster(),
                          poster_tag="p1")])
        self.assertNotEqual(a["src"], b["src"])
        self.assertEqual(s.misses, 2)

    def test_lru_eviction_frees_files(self):
        s = self._store()
        s.MAX_ENTRIES = 3
        srcs = []
        for i in range(5):
            out = s.strip([Tile(key="k%d" % i, title="T%d" % i,
                                poster=_poster(), poster_tag="p%d" % i)])
            srcs.append(out["src"])
        # Only the last 3 survive on disk; the first 2 were evicted+removed.
        self.assertFalse(os.path.exists(srcs[0]))
        self.assertFalse(os.path.exists(srcs[1]))
        self.assertTrue(os.path.exists(srcs[-1]))

    def _row(self, s, key, tiles=1):
        return s.strip([Tile(key="%s%d" % (key, i), title=key,
                             poster=_poster(), poster_tag="p%s%d" % (key, i))
                        for i in range(tiles)])

    def test_the_cache_is_bounded_in_bytes_and_not_only_in_entries(self):
        """A strip is a whole ROW, so the entry count says nothing about the
        memory. A 50-tile carousel at 4K is ~31 MiB in one entry, and 80 of
        those is not a cache. Where it lands is the backend's business --
        ctypes buffers here, tmpfs files on jsonipc -- and both are RAM on
        the machines that can least afford it."""
        s = self._store()
        s.on_scene_pushed()
        one = self._row(s, "size")
        entry_bytes = one["iw"] * one["ih"] * 4
        s.MAX_BYTES = entry_bytes * 3
        srcs = []
        for i in range(8):
            s.on_scene_pushed()             # one row per frame, so each may age
            srcs.append(self._row(s, "k%d" % i)["src"])
        self.assertLessEqual(s._bytes, s.MAX_BYTES)
        self.assertFalse(os.path.exists(srcs[0]), "the oldest row survived")
        self.assertTrue(os.path.exists(srcs[-1]), "the newest row was freed")

    def test_an_owner_that_pushes_no_scenes_keeps_the_old_bound(self):
        # Not knowing what is on screen is a reason to free less, not more:
        # the byte pass would otherwise evict below the entry count that
        # exists to guarantee a whole scene fits.
        s = self._store()
        s.MAX_BYTES = 1
        srcs = [self._row(s, "u%d" % i)["src"] for i in range(4)]
        for src in srcs:
            self.assertTrue(os.path.exists(src))

    def test_the_live_scenes_rows_are_never_freed_under_byte_pressure(self):
        """The safety invariant the entry bound exists for. Freeing a bitmap
        the live scene references is a read of freed memory by mpv on the
        libmpv path -- not a missing picture. The byte pass stops at the
        first entry belonging to this frame or the one before it, so a
        window too full to fit the budget draws over budget rather than
        composing over a freed buffer."""
        s = self._store()
        s.MAX_BYTES = 1          # nothing at all would fit
        s.on_scene_pushed()
        srcs = [self._row(s, "row%d" % i)["src"] for i in range(6)]
        for src in srcs:
            self.assertTrue(os.path.exists(src),
                            "freed a strip the frame being built is using")

    def test_the_scene_before_is_protected_too(self):
        # The scene on screen is the last one PUSHED, and the renderer
        # re-issues its overlays without a new push besides.
        s = self._store()
        s.on_scene_pushed()
        old = self._row(s, "onscreen")["src"]
        s.on_scene_pushed()          # a build starts; the old scene is still up
        s.MAX_BYTES = 1
        self._row(s, "new")
        self.assertTrue(os.path.exists(old),
                        "freed the strip the renderer is still compositing")
        # ...and once a frame has gone by without asking for it, it may go.
        s.on_scene_pushed()
        s.on_scene_pushed()
        self._row(s, "newer")
        self.assertFalse(os.path.exists(old))

    def test_eviction_keeps_the_byte_count_honest(self):
        # Miscounting is silent: too high and the cache starves itself, too
        # low and the bound stops meaning anything.
        s = self._store()
        rows = [self._row(s, "b%d" % i) for i in range(4)]
        self.assertEqual(s._bytes,
                         sum(r["iw"] * r["ih"] * 4 for r in rows))
        s.MAX_ENTRIES = 2
        self._row(s, "b9")
        self.assertEqual(
            s._bytes,
            sum(r["iw"] * r["ih"] * 4
                for r in list(s._cache.values())))

    def test_a_second_build_for_one_push_does_not_free_the_live_scene(self):
        """MpvtkApp.render builds TWICE when a scene turns up glyphs whose
        widths were never measured -- which is what a new screen does. If
        the cache counted builds, the second one rotated the protected
        window off the scene mpv was compositing and freed it, before the
        new scene had been pushed at all."""
        s = self._store()
        s.MAX_BYTES = 1
        onscreen = self._row(s, "onscreen")["src"]
        s.on_scene_pushed()
        # The re-layout: two builds, no push between them.
        self._row(s, "next")
        self._row(s, "next")
        self.assertTrue(os.path.exists(onscreen),
                        "a re-laid-out build freed the scene on screen")

    def test_a_build_that_raises_does_not_free_the_live_scene(self):
        """A failed build keeps the previous frame up (views index into
        state that arrives asynchronously, so this is not theoretical) and
        pushes nothing. Counting builds, the displayed scene fell out of the
        protected window two failures later."""
        s = self._store()
        s.MAX_BYTES = 1
        onscreen = self._row(s, "onscreen")["src"]
        s.on_scene_pushed()
        for _ in range(5):
            pass            # builds that raised: no touches, and no push
        self._row(s, "elsewhere")
        self.assertTrue(os.path.exists(onscreen),
                        "the scene on screen was freed while it was up")

    def test_an_entry_held_across_frames_can_say_it_is_still_there(self):
        """The cast screen composites one full-window bitmap on a worker and
        renders from the parked entry forever. Nothing re-requests it, so it
        would age out of the protected window while being the only thing on
        screen -- and it is the biggest buffer the app makes."""
        s = self._store()
        s.MAX_BYTES = 1
        entry = s.bitmap("cast-backdrop", _poster(size=(400, 300)))
        for _ in range(6):
            s.keep(entry)
            self._row(s, "other")
            s.on_scene_pushed()
        self.assertTrue(os.path.exists(entry["src"]),
                        "freed the bitmap the cast screen is drawing")

    def test_the_same_key_arriving_twice_frees_one_and_counts_one(self):
        """Both insert paths drop the lock across the composite, and the
        same key really does arrive by two routes (a grid composites through
        the pool while the paginated view of the same items composites
        inline). Overwriting stranded a buffer with no cache reference to
        free it by, and added its size to a total nothing subtracts -- until
        the drift alone exceeded MAX_BYTES and the cache trimmed itself to
        nothing on every insert."""
        s = self._store()
        first = self._row(s, "dup")
        before = s._bytes
        # Compose the identical row again and hand it to the same key, as
        # the racing path does.
        again = s._compose([Tile(key="dup0", title="dup", poster=_poster(),
                                 poster_tag="pdup0")], s.geom)
        key = list(s._cache.keys())[-1]
        with s._lock:
            kept = s._insert(key, again)
        self.assertIs(kept, first, "the incumbent was replaced")
        self.assertFalse(os.path.exists(again["src"]),
                         "the loser's buffer was stranded")
        self.assertEqual(s._bytes, before, "the byte count was inflated")

    def test_clearing_resets_the_byte_count(self):
        s = self._store()
        self._row(s, "c")
        s.clear()
        self.assertEqual(s._bytes, 0)

    def test_a_deferred_trim_frees_the_screen_you_left(self):
        """The small-machine path: on a machine short of RAM the rows behind
        you are worth more as memory than as a fast trip back.

        The deferral is the whole design. When the screen changes, the scene
        mpv is compositing is STILL the old one -- the new one has not been
        pushed yet -- so the old strips are exactly the bitmaps that must
        not be freed at that instant. One frame later they are neither the
        live set nor the previous one and the ordinary gate lets them go.
        """
        s = self._store()
        s.on_scene_pushed()
        old = self._row(s, "old")["src"]          # the screen being left
        s.on_scene_pushed()
        new = self._row(s, "new")["src"]          # first frame of the next
        s.trim_soon()
        self.assertTrue(os.path.exists(old),
                        "freed the scene mpv is still compositing")
        s.on_scene_pushed()
        self.assertTrue(os.path.exists(old),
                        "freed a scene still inside the protection window")
        # ...and once it has aged out of that window, the booked trim runs.
        # Each frame re-requests what it draws, which is what keeps the
        # screen on show live -- exactly as a real build does.
        for _ in range(s.PROTECT_GENERATIONS + 1):
            self._row(s, "new")
            s.on_scene_pushed()
        self.assertFalse(os.path.exists(old), "the old screen was not freed")
        self.assertTrue(os.path.exists(new), "freed the screen now on show")

    def test_a_trim_is_armed_once_not_forever(self):
        # Otherwise every frame would free anything not drawn on the last
        # two, and scrolling a row off screen would cost a recomposite.
        s = self._store()
        s.on_scene_pushed()
        self._row(s, "a")
        s.trim_soon()
        for _ in range(6):
            s.on_scene_pushed()
        kept = self._row(s, "b")["src"]
        for _ in range(6):
            s.on_scene_pushed()
        self.assertTrue(os.path.exists(kept),
                        "a one-off trim kept trimming")

    def test_file_backend_writes_valid_bgra(self):
        g = TileGeom()
        s = self._store()
        out = s.strip([Tile(key="a", title="A", poster=_poster())])
        # premultiplied BGRA = 4 bytes/pixel; file size must match iw*ih*4.
        self.assertEqual(os.path.getsize(out["src"]),
                         out["iw"] * out["ih"] * 4)

    def test_memory_backend_uses_address_src(self):
        s = self._store(mem=True)
        out = s.strip([Tile(key="a", title="A", poster=_poster())])
        self.assertTrue(out["src"].startswith("&"),
                        "libmpv backend must use an &<addr> src")
        # clear() releases the buffer through the store without error
        s.clear()

    def test_placeholder_when_no_poster(self):
        # A tile with no poster still composites (no crash) and is clickable.
        s = self._store()
        out = s.strip([Tile(key="a", title="A", poster=None)])
        self.assertEqual(out["regions"][0]["key"], "a")

    # -- async composite path (grid) --------------------------------------

    def test_async_returns_placeholder_then_real(self):
        s = self._store()
        got = threading.Event()
        s.set_notify(got.set)
        tiles = [Tile(key="a", title="A", poster=_poster(), poster_tag="p1")]
        first = s.strip(tiles, async_=True)
        # Immediately: a placeholder with the real hit-regions/geometry.
        self.assertTrue(first.get("placeholder"))
        self.assertEqual([r["key"] for r in first["regions"]], ["a"])
        self.assertEqual(first["iw"], TileGeom().tile_w)
        # The worker composites off-thread and notifies.
        self.assertTrue(got.wait(5), "async composite should notify")
        second = s.strip(tiles, async_=True)
        self.assertFalse(second.get("placeholder"),
                         "the real strip replaces the placeholder")
        self.assertNotEqual(second["src"], first["src"])
        self.assertEqual(s.hits, 1)

    def test_async_placeholder_shares_blank_per_shape(self):
        s = self._store()
        p1 = s.strip([Tile(key="a", poster=_poster()),
                      Tile(key="b", poster=_poster())], async_=True)
        p2 = s.strip([Tile(key="c", poster=_poster()),
                      Tile(key="d", poster=_poster())], async_=True)
        self.assertTrue(p1.get("placeholder") and p2.get("placeholder"))
        # Same shape (2 tiles) -> one shared blank bitmap...
        self.assertEqual(p1["src"], p2["src"])
        # ...but each row keeps its own hit-regions.
        self.assertEqual([r["key"] for r in p2["regions"]], ["c", "d"])
        s.shutdown()

    def test_async_memory_backend(self):
        s = self._store(mem=True)
        got = threading.Event()
        s.set_notify(got.set)
        tiles = [Tile(key="a", poster=_poster())]
        ph = s.strip(tiles, async_=True)
        self.assertTrue(ph["src"].startswith("&"))
        self.assertTrue(got.wait(5))
        real = s.strip(tiles, async_=True)
        self.assertTrue(real["src"].startswith("&"))
        self.assertFalse(real.get("placeholder"))
        s.shutdown()
        s.clear()

    def test_closed_store_composites_inline(self):
        # After shutdown the pool is gone; an async request must still return a
        # finished strip (composited inline), never a stranded placeholder.
        s = self._store()
        s.shutdown()
        out = s.strip([Tile(key="z", poster=_poster())], async_=True)
        self.assertFalse(out.get("placeholder"))
        self.assertEqual(out["regions"][0]["key"], "z")

    def test_sync_default_is_unchanged(self):
        # async_ defaults off: the inline path returns the real strip at once.
        s = self._store()
        out = s.strip([Tile(key="a", poster=_poster())])
        self.assertFalse(out.get("placeholder"))
        self.assertIn("regions", out)

    def test_wide_geom_dimensions(self):
        s = self._store(geom=TileGeom(tile_w=240, tile_h=135, caption_h=44))
        out = s.strip([Tile(key="a", poster=_poster(size=(240, 135)))])
        self.assertEqual(out["iw"], 240)
        self.assertEqual(out["ih"], 135 + 44)


class TestCoverCrop(unittest.TestCase):
    """Artwork fills its tile in EVERY theme, not just the rounded ones.

    The crop used to ride on the theme's ``rounded`` flag, so the stock look
    letterboxed whatever the server had not already cropped for us -- offline
    artwork (a local file, no server-side resize) and chapter thumbnails
    (fetched by max width, so they keep the video's aspect). ``rounded`` now
    decides the card's SHAPE and nothing about the picture inside it.

    Painted with a wide poster in a portrait tile: whether it was cropped or
    letterboxed is the colour of the bands above and below it.
    """

    def _paint(self, rounded=False, **tile):
        g = TileGeom().physical()
        img = Image.new("RGBA", (g.tile_w, g.strip_h), (0, 0, 0, 0))
        store = StripStore(cache_dir=None, mem_store=None)
        orig = theme.active
        if rounded:
            theme.active = lambda: dict(orig() or {}, rounded=True)
        try:
            store._paint_poster(
                img, ImageDraw.Draw(img), 0,
                Tile(key="k", title="T", poster=_poster(size=(400, 100)),
                     **tile),
                g)
        finally:
            theme.active = orig
        return img, g

    def _bands(self, img, g):
        """The strips the letterbox would put above and below the art. Taken
        mid-width, which is inside a rounded card's silhouette as well as a
        square one's."""
        return [img.getpixel((g.tile_w // 2, 3))[:3],
                img.getpixel((g.tile_w // 2, g.tile_h - 4))[:3]]

    def test_the_stock_theme_crops_rather_than_letterboxing(self):
        img, g = self._paint()
        for got in self._bands(img, g):
            self.assertEqual(got, (120, 30, 30))
        # Square card, so the art reaches the corners too.
        self.assertEqual(img.getpixel((3, 3))[:3], (120, 30, 30))

    def test_the_rounded_theme_still_crops(self):
        img, g = self._paint(rounded=True)
        for got in self._bands(img, g):
            self.assertEqual(got, (120, 30, 30))

    def test_a_rounded_card_still_clips_the_art_to_its_corners(self):
        """Cropping the art to the tile and then pasting it square would fill
        the corner pixels the silhouette leaves transparent."""
        img, _g = self._paint(rounded=True)
        self.assertEqual(img.getpixel((1, 1))[3], 0)

    def test_contain_still_draws_the_artwork_whole(self):
        """A wordmark standing in for missing artwork: cropping it takes the
        name off both ends, and that is the caller's call, not the theme's."""
        img, g = self._paint(contain=True)
        card = theme.rgb(theme.CARD_BG)[:3]
        for got in self._bands(img, g):
            self.assertEqual(got, card)


class TestVersionCountBadge(unittest.TestCase):
    """The multi-version indicator -- jellyfin-web's `.mediaSourceIndicator`.

    It has the top-LEFT corner to itself, which is where web puts it, and
    the watched tick is on the RIGHT with the rest of `.cardIndicators`.
    That is a change: the two used to share this corner, on the reasoning
    that the right-hand stack was already three deep. What decided it is
    that the tick is the badge people scan a season *for*, and having it on
    the side no other client puts it costs a beat every time.

    So the assertions here are about the corner staying the version count's
    alone, and about the tick having left it.
    """

    ACCENT = theme.rgb(theme.ACCENT, 255)[:3]

    def _paint(self, **tile):
        g = TileGeom().physical()
        img = Image.new("RGBA", (g.tile_w, g.strip_h), (0, 0, 0, 0))
        store = StripStore(cache_dir=None, mem_store=None)
        store._paint_decorations(img, ImageDraw.Draw(img), 0,
                                 Tile(key="k", **tile), g)
        return img

    @staticmethod
    def _slot(img, n):
        """A point on the disc in the n'th left-hand slot: near its top edge,
        which is inside r=11 but clear of both the digit and the tick, so
        neither their ink nor their antialiased fringe is what gets sampled.
        """
        return img.getpixel((17 + 26 * n, 17 - 9))

    @staticmethod
    def _right_slot(img, n):
        """The same point on the n'th badge of the top-RIGHT stack, counted
        from the corner leftwards (which is the order it is filled in)."""
        return img.getpixel((img.width - 17 - 26 * n, 17 - 9))

    def test_two_versions_draw_a_badge_in_the_corner(self):
        self.assertEqual(self._slot(self._paint(sources=2), 0)[:3], self.ACCENT)

    def test_one_version_draws_nothing(self):
        """The server omits MediaSourceCount entirely at 1, so 0 and 1 are
        the same answer and neither is worth a chip on every tile."""
        for n in (0, 1):
            with self.subTest(n):
                self.assertEqual(self._slot(self._paint(sources=n), 0)[3], 0)

    def test_a_watched_multiversion_film_puts_them_in_facing_corners(self):
        """Both are drawn -- the change moved the tick, it did not drop it."""
        img = self._paint(sources=3, watched=True)
        self.assertEqual(self._slot(img, 0)[:3], self.ACCENT)
        self.assertEqual(self._right_slot(img, 0)[:3], self.ACCENT)

    def test_the_second_left_slot_is_never_used(self):
        """The version count is the whole of the left-hand corner now, so
        nothing is ever pitched beside it. A tick landing back here is the
        regression this guards."""
        for tile in ({"sources": 2}, {"sources": 2, "watched": True}):
            with self.subTest(**tile):
                self.assertEqual(self._slot(self._paint(**tile), 1)[3], 0)

    def test_the_tick_takes_the_right_corner_not_the_left(self):
        """jellyfin-web's `.cardIndicators` is top-right and its last child
        -- the played indicator -- is the one in the corner."""
        img = self._paint(watched=True)
        self.assertEqual(self._right_slot(img, 0)[:3], self.ACCENT)
        self.assertEqual(self._slot(img, 0)[3], 0)

    def test_the_count_is_part_of_the_cache_key(self):
        """It is baked into the composited strip like every other decoration,
        so an item that gains a version has to recomposite rather than serve
        the old bitmap."""
        s = StripStore(cache_dir=None, mem_store=MemoryStore())
        base = dict(key="a", title="A", poster=_poster(), poster_tag="p1")
        a = s.strip([Tile(**base)])
        b = s.strip([Tile(**dict(base, sources=2))])
        self.assertNotEqual(a["src"], b["src"])


class TestBitmapConcurrentMiss(unittest.TestCase):
    """bitmap() drops its lock across _store(), so two callers can both miss
    and both allocate. cast.py's compositor runs on the browser's shared pool
    and resubmits the same (data, size) on a resize tick, so this is reachable
    rather than theoretical -- and the loser's buffer used to be overwritten in
    the cache without being freed, stranding a full-window BGRA (~8 MB at
    1080p) that nothing would ever evict.

    _blank_strip already re-checked and freed; bitmap() did not.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-bitmap-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _race(self, store, n=6):
        """Fire n concurrent bitmap() calls for one key, all released at once
        so they overlap inside _store()."""
        img = Image.new("RGB", (64, 48), (10, 20, 30))
        go = threading.Barrier(n)
        out = []
        lock = threading.Lock()

        def work():
            go.wait()
            entry = store.bitmap("same-key", img)
            with lock:
                out.append(entry)

        threads = [threading.Thread(target=work) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        return out

    def test_file_backend_leaves_no_orphan_files(self):
        store = StripStore(cache_dir=self.tmp)
        entries = self._race(store)
        # Every caller gets the SAME entry -- the winner's.
        self.assertEqual(len({e["src"] for e in entries}), 1)
        # And exactly one bitmap exists on disk: the losers freed theirs.
        self.assertEqual(
            [n for n in os.listdir(self.tmp) if n.endswith(".bgra")],
            [os.path.basename(entries[0]["src"])])

    def test_memory_backend_leaves_no_orphan_buffers(self):
        mem = MemoryStore()
        store = StripStore(cache_dir=None, mem_store=mem)
        entries = self._race(store)
        self.assertEqual(len({e["src"] for e in entries}), 1)
        store.clear()
        # clear() frees what the cache holds. Anything still resident is a
        # buffer the race allocated and nothing owns.
        self.assertEqual(_live_buffers(mem), 0)

    def test_the_surviving_entry_is_the_cached_one(self):
        # The winner must be what a later lookup returns, or callers holding
        # the returned dict and callers re-requesting disagree about iw/ih --
        # which is the SIGBUS the renderer's crop bound warns about.
        store = StripStore(cache_dir=self.tmp)
        entries = self._race(store)
        again = store.bitmap("same-key", Image.new("RGB", (64, 48)))
        self.assertIs(again, entries[0])


def _live_buffers(mem):
    """Buffers a MemoryStore still holds, however it names its container."""
    for attr in ("_buffers", "_bufs", "_store", "_images"):
        got = getattr(mem, attr, None)
        if isinstance(got, dict):
            return len(got)
    raise AssertionError("MemoryStore's buffer container was renamed; "
                         "update this helper")


if __name__ == "__main__":
    unittest.main()


class TestMixedScriptCaptions(unittest.TestCase):
    """A caption that mixes scripts is drawn with a face per run.

    The end-to-end half of `test_mpvtk_pilfont`: this goes through the real
    compositor, because the bug reached users as "the year under my Japanese
    posters is a row of boxes" and the fix is only worth anything if
    `_paint_caption` is the thing doing it.

    Detection is by counting *distinct* glyph shapes. Tofu is one shape
    repeated, so "(2013)" drawn by a face without Latin coverage has far
    fewer distinct columns of ink than the six characters deserve — no
    reference render and no font names needed.
    """

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="strips-i18n-")
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.store = StripStore(cache_dir=self.dir, geom=TileGeom())
        from jellyfin_mpv_shim.mpvtk import pilfont
        self.pilfont = pilfont

    def caption_band(self, title):
        """The strip below the artwork, where the caption is drawn."""
        geom = TileGeom()
        out = self.store.strip([Tile(key="k", title=title, poster=_poster())])
        path = out["src"]
        if not isinstance(path, str) or not os.path.isfile(path):
            self.skipTest("this backend does not write a readable bitmap")
        with open(path, "rb") as fh:
            raw = fh.read()
        w, h = out["iw"], out["ih"]
        img = Image.frombytes("RGBA", (w, h), raw[-w * h * 4:], "raw", "BGRA")
        return img.crop((0, geom.tile_h, min(w, geom.tile_w), h))

    def test_a_mixed_caption_draws_its_latin_run(self):
        """Two titles differing only in the digits must draw differently.

        The sharpest available detector, and it needs no font names and no
        reference render: tofu is one shape repeated, so if the Latin run is
        being drawn by a face without Latin coverage, "(2013)" and "(2014)"
        composite to *identical* bitmaps. A counting heuristic does not
        catch this -- the CJK half supplies plenty of variety on its own,
        which is exactly how the first version of this test passed with the
        bug reintroduced.
        """
        a, b = "進撃の巨人 (2013)", "進撃の巨人 (2014)"
        if self.pilfont.font_for(a, 20) is self.pilfont.font("latin", 20):
            self.skipTest("no separate CJK face installed on this host")
        # assertTrue on the comparison, not assertNotEqual on the bytes:
        # these are ~50 KB bitmaps and unittest prints both operands.
        self.assertTrue(
            self.caption_band(a).tobytes() != self.caption_band(b).tobytes(),
            "changing the digits changed nothing on screen, so they are "
            "being drawn as identical .notdef boxes")

    def test_the_control_case_differs_too(self):
        """A guard on the detector: two plainly different Latin captions must
        of course differ, or the assertion above proves nothing."""
        self.assertTrue(self.caption_band("Blade 2013").tobytes()
                        != self.caption_band("Blade 2014").tobytes())

    def glyph_shapes(self, band):
        """How many distinct non-blank pixel columns the band contains."""
        grey = band.convert("L")
        cols = []
        for x in range(grey.width):
            col = bytes(grey.getpixel((x, y)) > 60
                        for y in range(grey.height))
            if any(col):
                cols.append(col)
        return len(set(cols))

    def test_a_latin_caption_still_draws(self):
        # The control: whatever the host has, plain Latin must be fine.
        self.assertGreater(self.glyph_shapes(self.caption_band("Blade 2049")),
                           12)


class TestTheBudgetFollowsTheMachine(unittest.TestCase):
    """These bytes are memory on both backends: ctypes buffers in-process on
    libmpv, and on mpv_ext files in a scratch directory that is RAM-backed
    wherever one exists. That configuration is what filled a VM's memory and
    started this work, and it is also the one a small machine lands in --
    /run/user is small there, so the cache goes to /dev/shm, which is RAM
    under another name."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="mpvtk-tight-")
        self.addCleanup(__import__("shutil").rmtree, self.tmp,
                        ignore_errors=True)
        self.store = StripStore(cache_dir=self.tmp)

    def test_a_short_machine_gets_the_smaller_cache(self):
        self.store.set_memory_pressure(True)
        self.assertEqual(self.store.MAX_BYTES, StripStore.TIGHT_MAX_BYTES)
        self.store.set_memory_pressure(False)
        self.assertEqual(self.store.MAX_BYTES, StripStore.MAX_BYTES)

    def test_lowering_it_evicts_at_once(self):
        # Otherwise the cache sits over its new budget until something else
        # happens to insert.
        s = self.store
        for i in range(6):
            s.strip([Tile(key="k%d" % i, title="T", poster=_poster(),
                          poster_tag="p%d" % i)])
            s.on_scene_pushed()
        # Let them all age past the protected window, so what is left is the
        # budget's doing and not the liveness gate's.
        for _ in range(s.PROTECT_GENERATIONS + 1):
            s.on_scene_pushed()
        held = s._bytes
        self.assertGreater(held, 0)
        s.TIGHT_MAX_BYTES = held // 4
        s.set_memory_pressure(True)
        self.assertLessEqual(s._bytes, held // 4)

    def test_it_still_cannot_free_what_is_on_screen(self):
        # However far the budget drops. _protected has the last word.
        s = self.store
        s.TIGHT_MAX_BYTES = 1
        live = s.strip([Tile(key="live", title="T", poster=_poster(),
                             poster_tag="plive")])["src"]
        s.on_scene_pushed()
        s.set_memory_pressure(True)
        self.assertTrue(os.path.exists(live),
                        "freed a bitmap the live scene is using")
