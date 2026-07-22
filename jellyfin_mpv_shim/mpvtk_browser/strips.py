"""Production strip compositor for the mpvtk browser.

A "strip" is one BGRA bitmap holding a whole row of poster tiles —
posters plus baked-in captions, year/subtitle, watched checkmarks,
unwatched-count badges and resume progress bars — declared to the
renderer as a single ``ImageMap`` with one transparent hit-region per
tile. This is what makes tiles scale (GUIDE §5/§6): a screenful is a
handful of overlays instead of one-per-poster, decorations dodge the
"bitmaps composite above ASS" z-order constraint, and scrolling is pure
crop math on cached bitmaps.

Strips are **content-keyed**: the key folds in every visible property
(poster identity, title, watched/badge/progress, geometry), so changing
any of them composites a new bitmap under a new src. The cache is
LRU-bounded so a long browse session doesn't grow without limit;
anything on screen was requested by the current build and is therefore
most-recent.

A new src alone does NOT guarantee the renderer refreshes, though: on
the libmpv path src is a malloc address, and addresses are recycled once
a freed buffer leaves MemoryStore's graveyard, so a new entry can be
handed a departed entry's exact src. Every entry therefore also carries
a monotonic ``v`` (see ``_store``), which is what actually keeps the
renderer's overlay cache from showing stale content.

Backends: on libmpv (in-process) strips go to a ``MemoryStore`` (ctypes
buffers, ``&<addr>`` src, no fs); on jsonipc they're BGRA files. The
view supplies decoded ``PIL`` posters (from ``thumbnails``); a tile with
no poster yet renders a placeholder and recomposites when the poster
arrives (its ``poster_tag`` changes the key).
"""

import logging
import os
import threading
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Optional

from ..mpvtk.rawimage import write_bgra
from . import theme

log = logging.getLogger("mpvtk_browser.strips")

def _px(v):
    """Logical -> physical, for constants baked into a strip bitmap."""
    from ..mpvtk.scaling import px

    return px(v)


def _font(size, bold=False, text=None):
    """Font for baked caption text. ``text`` selects the script-appropriate
    face — Pillow does no font fallback, so a Japanese title drawn with the
    Latin face is a row of tofu (see mpvtk.pilfont)."""
    from ..mpvtk import pilfont

    if text is None:
        return pilfont.font("latin", size, bold)
    return pilfont.font_for(text, size, bold)


@dataclass
class TileGeom:
    """Pixel geometry for a tile (poster + caption block below it)."""

    tile_w: int = 140
    tile_h: int = 210
    gap: int = 14
    caption_h: int = 46
    title_size: int = 15
    sub_size: int = 13
    badge_size: int = 14

    @property
    def strip_h(self):
        return self.tile_h + self.caption_h

    def physical(self):
        """A copy with every pixel field converted to physical px.

        TileGeom is authored in LOGICAL units like the rest of the view
        layer; only the compositor below works in physical pixels, because
        the renderer crops bitmaps rather than resampling them."""
        from ..mpvtk.scaling import px

        return TileGeom(
            tile_w=px(self.tile_w), tile_h=px(self.tile_h), gap=px(self.gap),
            caption_h=px(self.caption_h), title_size=px(self.title_size),
            sub_size=px(self.sub_size), badge_size=px(self.badge_size),
        )


# Tile shapes, matching the Tk browser's poster_box/thumb_box/square_box.
POSTER_GEOM = TileGeom(tile_w=150, tile_h=225, caption_h=46)        # 2:3
LANDSCAPE_GEOM = TileGeom(tile_w=240, tile_h=135, caption_h=44)     # 16:9
SQUARE_GEOM = TileGeom(tile_w=170, tile_h=170, caption_h=44)        # 1:1
WIDE_GEOM = LANDSCAPE_GEOM  # backwards-compatible alias


@dataclass
class Tile:
    """One tile's data. ``key`` is the stable identity (usually the
    Jellyfin item id) — it becomes the hit-region id and part of the
    cache key. ``poster`` is a decoded PIL image (any size; centered and
    letterboxed into the tile) or None for a placeholder. ``poster_tag``
    identifies the poster's content for cache keying ("" when absent, so
    the strip recomposites when the real poster lands). ``glyph`` is the
    placeholder character drawn when there's no poster (initial / ♪)."""

    key: str
    title: str = ""
    subtitle: str = ""
    poster: Optional[object] = None
    poster_tag: str = ""
    watched: bool = False
    badge: int = 0
    progress: float = 0.0
    downloaded: bool = False
    glyph: str = ""


class StripStore:
    # Must stay ABOVE renderer.lua's MAX_OVERLAYS (63), which bounds how many
    # bitmaps one scene may reference.
    #
    # The safety argument for freeing an evicted buffer is that an LRU whose
    # recency tracks the current build never drops anything visible — anything
    # on screen was just requested. That holds only while a scene fits in the
    # cache. At 48 it did not: a scene is allowed 63 overlays, so a dense one
    # evicted (and, on the libmpv path, FREED) buffers it was itself still
    # using, which is a read of freed memory by mpv rather than a missing
    # picture. tests/test_python_lua_constants.py pins the relationship so it
    # cannot drift back.
    MAX_ENTRIES = 80

    def __init__(self, cache_dir=None, mem_store=None, geom=None,
                 notify=None, workers=2):
        self.dir = cache_dir
        self.mem = mem_store  # MemoryStore for the libmpv backend, else None
        self.geom = geom or TileGeom()
        self._cache = OrderedDict()
        # The cache is built and read on the mpvtk loop thread, but clear()
        # is called from teardown paths that run on other threads (the
        # player's action thread via on_mpv_terminated, and whoever calls
        # stop()). Without this, clear() iterating while the loop inserts
        # raises "dict changed size during iteration" — caught and logged at
        # the call site, which left half the buffers freed and half not.
        #
        # The same lock now also guards inserts from the compositor pool:
        # strip() composites OFF the loop thread (a full row is 20-140ms at
        # 4K and that used to stall the build; see thumbnails' pipeline for
        # the same split) and returns a cheap placeholder, then swaps in the
        # real bitmap and notify()s when the worker lands it. Eviction still
        # frees from the LRU tail, so an on-screen strip — requested by the
        # current build, hence most-recent — is never the one freed.
        self._lock = threading.Lock()
        self._counter = 0
        self.hits = 0
        self.misses = 0
        # Called (thread-safe, no args) when an async composite lands, so the
        # owner can wake its render loop and rebuild with the real bitmap.
        self._notify = notify
        self._closed = False
        # Keys currently being composited, so a row that stays on screen
        # across several builds is only submitted once.
        self._inflight = set()
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="strip")

    def set_notify(self, notify):
        """Attach the wake-up callback after construction (mirrors
        ThumbnailStore.set_notify). A plain assignment: workers only read it."""
        self._notify = notify

    # -- keying -----------------------------------------------------------

    def _tile_key(self, t):
        return (
            t.key,
            t.poster_tag if t.poster is not None else "",
            t.title,
            t.subtitle,
            bool(t.watched),
            int(t.badge),
            round(float(t.progress), 2),
            bool(t.downloaded),
            t.glyph if t.poster is None else "",
        )

    @staticmethod
    def _geom_key(g):
        return (g.tile_w, g.tile_h, g.gap, g.caption_h,
                g.title_size, g.sub_size, g.badge_size)

    # -- public -----------------------------------------------------------

    def strip(self, tiles, geom=None, async_=False):
        """Composite ``tiles`` into one strip (in ``geom``'s shape, default
        the store's). Returns ``{"src", "iw", "ih", "v", "regions"}`` where each
        region is ``{"x", "y", "w", "h", "key"}`` in image-local coords (the
        view wraps these with on_click/on_context/id).

        ``async_=True`` composites on a worker pool instead of inline — a full
        4K row is 20-140ms and stalled the build. On a cache miss it returns a
        cheap placeholder (blank cards, real hit-regions) immediately and
        schedules the real bitmap; when the worker lands it, notify() wakes the
        loop and the next build gets the finished strip (marked
        ``"placeholder": True`` until then). Used for the virtualized grid,
        whose rows share one uniform (bounded) blank shape. Carousels keep the
        inline path (``async_=False``): each is a distinct, up-to-50-tile row,
        so a per-row blank would cost as much as the composite it defers."""
        g = geom or self.geom
        tiles = list(tiles)
        key = (self._geom_key(g), tuple(self._tile_key(t) for t in tiles))
        with self._lock:
            hit = self._cache.get(key)
            if hit is not None:
                self._cache.move_to_end(key)
                self.hits += 1
                return hit
            self.misses += 1
            do_async = async_ and not self._closed
            if do_async:
                submit = key not in self._inflight
                if submit:
                    self._inflight.add(key)
        if do_async:
            if submit:
                self._pool.submit(self._compose_task, key, tiles, g)
            return self._placeholder(tiles, g)
        # Inline path (carousels / headless / torn down): composite now.
        # Outside the lock — it is the expensive part and touches no cache.
        entry = self._compose(tiles, g)
        with self._lock:
            self._cache[key] = entry
            self._evict()
        return entry

    def _compose_task(self, key, tiles, g):
        """Pool worker: composite off the loop thread, then insert + notify."""
        entry = None
        try:
            entry = self._compose(tiles, g)
        except Exception:
            log.warning("strip composite failed", exc_info=True)
        with self._lock:
            self._inflight.discard(key)
            if entry is None:
                return
            if self._closed:
                # clear() already ran; don't resurrect a freed cache or strand
                # this buffer in it. Free it right back.
                self._free(entry["src"])
                return
            self._cache[key] = entry
            self._cache.move_to_end(key)
            self._evict()
        if self._notify is not None:
            try:
                self._notify()
            except Exception:
                log.debug("strip notify failed", exc_info=True)

    def _placeholder(self, tiles, g):
        """A stand-in entry for a not-yet-composited strip: a blank-card
        bitmap shared across every row of this shape, wrapped with the real
        per-tile hit-regions so clicks work before the artwork lands."""
        n = len(tiles)
        blank = self._blank_strip(g, n)
        regions = [
            {"x": col * (g.tile_w + g.gap), "y": 0,
             "w": g.tile_w, "h": g.strip_h, "key": t.key}
            for col, t in enumerate(tiles)
        ]
        lw = n * g.tile_w + (n - 1) * g.gap if n else 1
        return {"src": blank["src"], "iw": blank["iw"], "ih": blank["ih"],
                "lw": lw, "lh": g.strip_h, "v": blank["v"],
                "regions": regions, "placeholder": True}

    def _blank_strip(self, g, n):
        """Blank-card bitmap for a shape (geom, tile count), cached in the same
        LRU so it's freed like any strip. Shared by every pending row of that
        shape, so a screenful of placeholders is one composite, not one each."""
        bkey = ("blank", self._geom_key(g), n)
        with self._lock:
            hit = self._cache.get(bkey)
            if hit is not None:
                self._cache.move_to_end(bkey)
                return hit
        entry = self._compose_blank(n, g)
        with self._lock:
            # Another thread may have built the same shape while we composed.
            hit = self._cache.get(bkey)
            if hit is not None:
                self._free(entry["src"])
                self._cache.move_to_end(bkey)
                return hit
            self._cache[bkey] = entry
            self._evict()
        return entry

    def shutdown(self):
        """Stop the compositor pool. Call before clear() on teardown so no
        worker inserts into a cache that clear() is about to free."""
        with self._lock:
            self._closed = True
        self._pool.shutdown(wait=True)

    def bitmap(self, key, image, lsize=None):
        """Cache a single arbitrary image as BGRA (backdrops, logos, art) in
        the same store/LRU as strips. ``key`` must identify the content;
        ``image`` is a PIL image already at **physical** display size.
        ``lsize`` is the logical (w, h) box it was rasterized for — pass it
        whenever a logical box drove the size, so the Image widget can check
        the two agree. Returns ``{"src", "iw", "ih", "lw", "lh", "v"}``."""
        ck = ("bitmap", key)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None:
                self._cache.move_to_end(ck)
                self.hits += 1
                return hit
            self.misses += 1
        src, w, h, v = self._store(image)
        from ..mpvtk.scaling import dip

        lw, lh = lsize if lsize is not None else (dip(w), dip(h))
        entry = {"src": src, "iw": w, "ih": h, "lw": lw, "lh": lh, "v": v}
        with self._lock:
            self._cache[ck] = entry
            self._evict()
        return entry

    def clear(self):
        """Drop every cached bitmap and release its backing buffer.

        On the libmpv path those buffers are read BY ADDRESS by mpv, so this
        must only be called once mpv is genuinely dead — see
        mpvtk_browser.ui.on_mpv_terminated. Calling it while mpv is still
        compositing is a segfault, not a leak."""
        with self._lock:
            entries = list(self._cache.values())
            self._cache.clear()
        for entry in entries:
            self._free(entry["src"])

    # -- compositing ------------------------------------------------------

    def _compose(self, tiles, g):
        from PIL import Image as PILImage, ImageDraw

        from ..mpvtk.scaling import px, raster

        n = len(tiles)
        # Logical footprint first, then a canvas that is exactly raster() of
        # it — that identity is what widgets._check_raster verifies, and it
        # keeps the bitmap's stride in step with the box layout reserved.
        lw = n * g.tile_w + (n - 1) * g.gap if n else 1
        lh = g.strip_h
        pg = g.physical()
        img = PILImage.new("RGBA", raster(lw, lh), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        regions = []
        for col, t in enumerate(tiles):
            # Tile origin in logical units: regions are consumed by layout,
            # which runs logical. Painting converts, per tile, so rounding
            # never accumulates across a long strip.
            lx = col * (g.tile_w + g.gap)
            self._paint_poster(img, dr, px(lx), t, pg)
            self._paint_decorations(dr, px(lx), t, pg)
            self._paint_caption(dr, px(lx), t, pg)
            regions.append(
                {"x": lx, "y": 0, "w": g.tile_w, "h": g.strip_h, "key": t.key}
            )
        src, iw2, ih2, v = self._store(img)
        return {"src": src, "iw": iw2, "ih": ih2, "lw": lw, "lh": lh,
                "v": v, "regions": regions}

    def _compose_blank(self, n, g):
        """A row of empty cards — the placeholder bitmap shown while the real
        strip composites. Just the card fill + border per tile: no posters,
        decorations or captions, so it's the cheap part of _compose (the cost
        that remains, _store of a full-width buffer, is amortized because one
        blank serves every pending row of this shape)."""
        from PIL import Image as PILImage, ImageDraw

        from ..mpvtk.scaling import px, raster

        lw = n * g.tile_w + (n - 1) * g.gap if n else 1
        lh = g.strip_h
        pg = g.physical()
        img = PILImage.new("RGBA", raster(lw, lh), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        for col in range(n):
            x = px(col * (g.tile_w + g.gap))
            dr.rectangle([x, 0, x + pg.tile_w - 1, pg.tile_h - 1],
                         fill=theme.rgb(theme.PLACEHOLDER_BG, 255))
            dr.rectangle([x, 0, x + pg.tile_w - 1, pg.tile_h - 1],
                         outline=theme.rgb("101012", 255))
        src, iw2, ih2, v = self._store(img)
        return {"src": src, "iw": iw2, "ih": ih2, "lw": lw, "lh": lh, "v": v}

    def _paint_poster(self, img, dr, x, t, g):
        from PIL import Image as PILImage

        # Opaque card behind the poster (letterbox fill for odd aspects).
        dr.rectangle(
            [x, 0, x + g.tile_w - 1, g.tile_h - 1],
            fill=theme.rgb(theme.PLACEHOLDER_BG if t.poster is None
                           else theme.CARD_BG, 255),
        )
        if t.poster is not None:
            poster = t.poster
            if poster.size != (g.tile_w, g.tile_h):
                poster = poster.copy()
                poster.thumbnail((g.tile_w, g.tile_h), PILImage.LANCZOS)
            px = x + (g.tile_w - poster.width) // 2
            py = (g.tile_h - poster.height) // 2
            img.paste(poster, (px, py))
        elif t.glyph:
            # A muted centered glyph (first initial / ♪) so blank tiles read.
            gsize = max(_px(24), g.tile_h // 4)
            dr.text((x + g.tile_w / 2, g.tile_h / 2), t.glyph,
                    font=_font(gsize, bold=True, text=t.glyph), anchor="mm",
                    fill=theme.rgb(theme.SUBTLE_FG))
        dr.rectangle(
            [x, 0, x + g.tile_w - 1, g.tile_h - 1],
            outline=theme.rgb("101012", 255),
        )

    def _paint_decorations(self, dr, x, t, g):
        # NB every bare offset in here is a logical constant being drawn
        # into a physical bitmap, so it goes through _px(). g is already
        # physical (see _compose); mixing the two silently is how a scaled
        # tile ends up with 1x decorations pinned to its corner.
        lw = max(1, _px(2))          # decoration stroke width
        if t.watched:
            dr.ellipse([x + _px(6), _px(6), x + _px(28), _px(28)],
                       fill=theme.rgb(theme.ACCENT, 255))
            dr.line([(x + _px(12), _px(17)), (x + _px(16), _px(22)),
                     (x + _px(23), _px(12))],
                    fill=(255, 255, 255, 255), width=lw)
        # Top-right corner: downloaded badge takes priority over the
        # unplayed-count badge (they rarely coexist).
        if t.downloaded:
            cx, cy = x + g.tile_w - _px(17), _px(17)
            r = _px(11)
            dr.ellipse([cx - r, cy - r, cx + r, cy + r],
                       fill=theme.rgb(theme.ACCENT, 255))
            dr.line([(cx, cy - _px(5)), (cx, cy + _px(4))],
                    fill=(255, 255, 255, 255), width=lw)
            dr.line([(cx - _px(4), cy), (cx, cy + _px(4)), (cx + _px(4), cy)],
                    fill=(255, 255, 255, 255), width=lw)
            dr.line([(cx - _px(5), cy + _px(7)), (cx + _px(5), cy + _px(7))],
                    fill=(255, 255, 255, 255), width=lw)
        elif t.badge:
            bw = _px(26)
            dr.rounded_rectangle(
                [x + g.tile_w - bw - _px(5), _px(5),
                 x + g.tile_w - _px(5), _px(25)],
                radius=_px(6), fill=theme.rgb(theme.ACCENT, 255),
            )
            dr.text((x + g.tile_w - _px(5) - bw / 2, _px(15)), str(t.badge),
                    font=_font(g.badge_size, bold=True), anchor="mm",
                    fill=(255, 255, 255))
        if t.progress and t.progress > 0:
            frac = max(0.0, min(1.0, t.progress))
            bar = _px(6)
            dr.rectangle([x, g.tile_h - bar, x + g.tile_w - 1, g.tile_h - 1],
                         fill=theme.rgb(theme.PROGRESS_TRACK, 200))
            dr.rectangle(
                [x, g.tile_h - bar,
                 x + int((g.tile_w - 1) * frac), g.tile_h - 1],
                fill=theme.rgb(theme.ACCENT, 255),
            )

    def _paint_caption(self, dr, x, t, g):
        if t.title:
            fnt = _font(g.title_size, text=t.title)
            title = self._ellipsize(dr, t.title, fnt, g.tile_w)
            dr.text((x, g.tile_h + _px(6)), title, font=fnt,
                    fill=theme.rgb(theme.TEXT_FG))
        if t.subtitle:
            fnt = _font(g.sub_size, text=t.subtitle)
            sub = self._ellipsize(dr, t.subtitle, fnt, g.tile_w)
            dr.text((x, g.tile_h + _px(6) + g.title_size + _px(7)), sub,
                    font=fnt, fill=theme.rgb(theme.SUBTLE_FG))

    @staticmethod
    def _ellipsize(dr, text, font, max_w):
        if dr.textlength(text, font=font) <= max_w:
            return text
        while text and dr.textlength(text + "…", font=font) > max_w:
            text = text[:-1]
        return text + "…"

    # -- storage ----------------------------------------------------------

    def _store(self, img):
        """Returns (src, w, h, v). ``v`` is a monotonic content version.

        It is what keeps the libmpv path honest. There ``src`` is a raw
        malloc address, and addresses ARE recycled: once a freed buffer
        falls out of MemoryStore's graveyard, the next allocation can land
        on the exact address an evicted entry used (measured: 60 strip-sized
        allocations produced only 20 distinct addresses). The renderer skips
        re-issuing an overlay whose args are unchanged, so a recycled address
        made the NEW entry indistinguishable from the old one and mpv went on
        compositing the PREVIOUS buffer's contents under the new entry's
        identity -- a stale poster that never refreshes. mpv copies at
        overlay-add rather than reading the address live, so bumping v (which
        lands in the renderer's arg string) forces the re-issue and the copy.
        """
        # Under the lock even though the composite around it deliberately is
        # not: `+= 1` is a read-modify-write, and this runs on the pool
        # workers (the cast compositor) as well as the loop thread. Two
        # threads landing on the same value produced two live cache entries
        # sharing one path with DIFFERENT iw/ih — and the renderer bounds its
        # crop by iw/ih, so the entry describing the larger image would read
        # past the end of the smaller file. That is the SIGBUS the renderer's
        # own comment warns about. Evicting either entry also unlinked the
        # file still referenced by the other.
        with self._lock:
            self._counter += 1
            counter = self._counter
        if self.mem is not None:
            src, w, h = self.mem.add(img)
            return src, w, h, counter
        src = os.path.join(self.dir, "strip%d.bgra" % counter)
        w, h = write_bgra(img, src)
        return src, w, h, counter

    def _free(self, src):
        if self.mem is not None:
            self.mem.remove(src)
        else:
            try:
                os.remove(src)
            except OSError:
                pass

    def _evict(self):
        while len(self._cache) > self.MAX_ENTRIES:
            _key, old = self._cache.popitem(last=False)
            self._free(old["src"])
