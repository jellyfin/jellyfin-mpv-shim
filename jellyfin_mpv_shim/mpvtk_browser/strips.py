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


def logo_plate(image, live=False):
    """The plate for transparent artwork, or ``None`` to leave it alone.

    ``imageutil.plate_for`` decides *whether* a picture is a logo on a
    transparent background and what a light plate would swallow; this is the
    one place that decides what the app then does about it, so the tile
    compositor and the table's art cells cannot drift apart.

    ``live`` says the artwork is a channel logo rather than a film's or
    series' own Logo art -- the caller's answer, from
    ``live_tv.is_channel_artwork``, because only it knows what it is drawing.
    The two are settings apart because they are conventions apart: channel
    logos are dark ink drawn for a white page and need the plate to be
    visible at all, film logos are white and do not. See ``conf.py``.

    Both answers are honest, which is why both are reachable: the light plate
    the artwork was drawn for, or the theme's own card grey with no shadow --
    which is what jellyfin-web puts behind the same logos (#637).

    Only the colour and the shadow move. Whether there is a plate at all is
    still ``plate_for``'s answer, because the callers read it for a second
    thing as well -- artwork on a transparent background is a mark rather
    than a photograph, so it is letterboxed rather than cover-cropped, and
    that is true whichever backing it gets.
    """
    from ..conf import settings
    from ..imageutil import Plate, plate_for

    key = "logo_legibility_live_tv" if live else "logo_legibility_library"
    # Fall back to the setting's own default rather than to True: a config
    # stand-in without the key must not silently plate a library.
    if getattr(settings, key, live):
        return plate_for(image)
    grey = theme.rgb(theme.CARD_BG)
    plate = plate_for(image, light=grey)
    # Not plate_for's shadow verdict recomputed against the grey: the point
    # of turning this off is that there are no drop shadows, and against a
    # dark plate the question would simply flip to the black ink instead.
    return None if plate is None else Plate(grey, False)


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

    def scaled(self, f):
        """A copy scaled by factor ``f`` — the active theme's cover size, or
        the user's Cover Size setting. The gap is kept so rows keep their
        rhythm as the art grows."""
        if not f or abs(f - 1.0) < 1e-6:
            return self
        def r(v):
            return max(1, int(round(v * f)))
        return TileGeom(
            tile_w=r(self.tile_w), tile_h=r(self.tile_h), gap=self.gap,
            caption_h=r(self.caption_h), title_size=r(self.title_size),
            sub_size=r(self.sub_size), badge_size=r(self.badge_size),
        )


# Tile shapes, matching the Tk browser's poster_box/thumb_box/square_box.
# A theme may scale these (and override the landscape shape) at the view
# layer; see MpvtkBrowser.__init__.
POSTER_GEOM = TileGeom(tile_w=150, tile_h=225, caption_h=46)        # 2:3
LANDSCAPE_GEOM = TileGeom(tile_w=240, tile_h=135, caption_h=44)     # 16:9
SQUARE_GEOM = TileGeom(tile_w=170, tile_h=170, caption_h=44)        # 1:1
# ~5.4:1, jellyfin-web's banner (card.scss's cardPadder-banner is
# padding-bottom: 18.5%). Only reachable by a user explicitly choosing the
# Banner image type for a library -- nothing infers it, and auto_geom folds
# its own >=3 bucket into landscape instead. Wider than the poster row is
# tall, so it is sized to sit two-across rather than to match the others.
BANNER_GEOM = TileGeom(tile_w=486, tile_h=90, caption_h=44)         # ~5.4:1
WIDE_GEOM = LANDSCAPE_GEOM  # backwards-compatible alias


@dataclass
class Tile:
    """One tile's data. ``key`` is the stable identity (usually the
    Jellyfin item id) — it becomes the hit-region id and part of the
    cache key. ``poster`` is a decoded PIL image (any size; cover-cropped to
    fill the tile, unless ``contain`` or ``logo_plate`` says this artwork is a
    mark rather than a photograph) or None for a placeholder. ``poster_tag``
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
    #: Material icon name marking what kind of thing this is ("folder",
    #: "photo", "videocam"...), or "" for the types that get no marker --
    #: see components.type_indicator_icon.
    kind: str = ""
    glyph: str = ""
    #: Being written to disk right now. Turns the progress bar (which for
    #: these is how far through the *broadcast* is, not how far through you
    #: are) red — the one state where the bar does not mean "where you left
    #: off", so it must not look like it does.
    recording: bool = False
    #: Which recording symbol to draw, if any: "timer"/"recording" for a
    #: single recording, "series"/"series_inactive" for one covered by a
    #: series rule. Separate from ``recording`` because the two questions
    #: differ — a series-covered programme airing now is both.
    record: str = ""
    #: Draw the artwork WHOLE rather than filling the tile. A wordmark
    #: standing in for artwork the item does not have -- a logo on a banner
    #: strip, a banner on a 16:9 card -- loses the name to a cover-crop.
    #: Set by TileRenderer.poster_for, which knows what the chain resolved to.
    contain: bool = False
    #: This tile's artwork is a CHANNEL LOGO rather than a film's or series'
    #: own Logo art. The two are opposite conventions and get separate
    #: settings -- see ``logo_plate`` and ``live_tv.is_channel_artwork``.
    #: Carried on the tile because the compositor has the picture and not
    #: the item it came from.
    live: bool = False


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
        #: Mixed into every cache key. Strips are composited with the theme's
        #: colours baked in (the resume bar, the watched tick, the accent),
        #: so a theme change has to make every existing entry unreachable.
        #: A tag rather than clear(): clear() frees the backing buffers, and
        #: on libmpv those are read BY ADDRESS by a compositor that is still
        #: running. Retagging leaves the old entries to age out through the
        #: ordinary LRU, which only ever frees the least-recently-used one --
        #: by definition not the one on screen.
        self.tag = ""
        #: The theme half of ``tag``, and how many times :meth:`retag` has
        #: run. Held apart so the two cannot corrupt each other -- a theme
        #: whose name happened to contain the joining character would
        #: otherwise be truncated by the next retag. The counter is its own
        #: rather than the LRU's ``_counter``, so a cache invalidation cannot
        #: reorder the eviction queue.
        self._tag_theme = ""
        self._retag = 0
        self._pool = ThreadPoolExecutor(max_workers=workers,
                                        thread_name_prefix="strip")

    def set_theme_tag(self, tag):
        """Invalidate every cached bitmap by changing what the keys look
        like. Safe to call while mpv is compositing; see ``tag``."""
        self._tag_theme = str(tag)
        self._apply_tag()

    def retag(self):
        """Invalidate every cached bitmap without changing theme.

        For a setting that is baked into a composited strip but is not the
        theme -- ``logo_legibility``, which decides what goes behind a
        transparent logo. Same mechanism and the same safety argument as
        :meth:`set_theme_tag`; a counter rather than a value because the
        caller has nothing to name the new state with, only the fact that it
        changed.
        """
        self._retag += 1
        self._apply_tag()

    def _apply_tag(self):
        self.tag = ("%s~%d" % (self._tag_theme, self._retag) if self._retag
                    else self._tag_theme)

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
            t.kind,
            bool(t.recording),
            t.record,
            t.glyph if t.poster is None else "",
            bool(t.contain),
            # In the key because it picks the card colour: the same picture
            # is plated in one place and not in another.
            bool(t.live),
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
        key = (self.tag, self._geom_key(g),
               tuple(self._tile_key(t) for t in tiles))
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
        bkey = ("blank", self.tag, self._geom_key(g), n)
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
        the two agree. Returns ``{"src", "iw", "ih", "lw", "lh", "v"}``.

        ``image`` may instead be a zero-argument callable, called only on a
        miss. For a bitmap that is asked for on most frames and drawn on
        almost none of them, rasterizing to hand the answer to a cache that
        already has it is the whole cost — the hovered tile's play chip is
        re-derived every time its strip is rebuilt, and supersamples a disc
        at 3x to do it."""
        ck = ("bitmap", self.tag, key)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None:
                self._cache.move_to_end(ck)
                self.hits += 1
                return hit
            self.misses += 1
        src, w, h, v = self._store(image() if callable(image) else image)
        from ..mpvtk.scaling import dip

        lw, lh = lsize if lsize is not None else (dip(w), dip(h))
        entry = {"src": src, "iw": w, "ih": h, "lw": lw, "lh": lh, "v": v}
        with self._lock:
            # Re-check, exactly as _blank_strip does. The lock is dropped
            # across _store() above, so two callers can both miss and both
            # allocate -- and cast.py's compositor runs on the browser's
            # shared 4-worker pool, which submits the same (data, size) again
            # on a resize tick. Overwriting the first entry without freeing it
            # stranded a full-window BGRA buffer (~8 MB at 1080p) that nothing
            # would ever evict: a ctypes buffer on libmpv, an unlinked
            # strip*.bgra on jsonipc.
            hit = self._cache.get(ck)
            if hit is not None:
                self._free(src)
                self._cache.move_to_end(ck)
                return hit
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
            self._paint_decorations(img, dr, px(lx), t, pg)
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
        rounded = bool((theme.active() or {}).get("rounded"))
        r = _px(14)
        for col in range(n):
            x = px(col * (g.tile_w + g.gap))
            box = [x, 0, x + pg.tile_w - 1, pg.tile_h - 1]
            if rounded:
                dr.rounded_rectangle(box, radius=r,
                                     fill=theme.rgb(theme.PLACEHOLDER_BG, 255))
                dr.rounded_rectangle(box, radius=r,
                                     outline=theme.rgb("101012", 255))
            else:
                dr.rectangle(box, fill=theme.rgb(theme.PLACEHOLDER_BG, 255))
                dr.rectangle(box, outline=theme.rgb("101012", 255))
        src, iw2, ih2, v = self._store(img)
        return {"src": src, "iw": iw2, "ih": ih2, "lw": lw, "lh": lh, "v": v}

    def _paint_poster(self, img, dr, x, t, g):
        from PIL import Image as PILImage, ImageDraw, ImageOps

        # The card's SHAPE is theme-driven -- a "rounded" theme draws
        # jellyfin-web-style rounded corners, the stock look is a square
        # opaque card. What the artwork does inside that shape is not: every
        # theme cover-crops, as jellyfin-web does in all of its. The two used
        # to be one flag, which left no way to say "square cards, cropped
        # art" and made a letterboxed tile look like a theme choice rather
        # than the accident it was.
        rounded = bool((theme.active() or {}).get("rounded"))
        r = _px(14)
        box = [x, 0, x + g.tile_w - 1, g.tile_h - 1]
        card = theme.rgb(theme.PLACEHOLDER_BG if t.poster is None
                         else theme.CARD_BG, 255)
        plate = None
        if t.poster is not None:
            # Artwork on a transparent background is composited onto the card,
            # so the card is what it has to read against — and a black-on-
            # transparent channel logo does not read against a near-black one.
            # Repainting the whole card in the light plate such artwork is
            # drawn for, rather than sliding one in behind just the art, keeps
            # the tile a single shape: the silhouette, the border and the
            # rounded corners are all still drawn once, by the code below.
            plate = logo_plate(t.poster, t.live)
            if plate is not None:
                card = tuple(plate.color) + (255,)
        if rounded:
            # The corners are left transparent so the window background shows
            # through — that is what gives the card its rounded silhouette.
            dr.rounded_rectangle(box, radius=r, fill=card)
        else:
            dr.rectangle(box, fill=card)
        if t.poster is not None:
            poster = t.poster
            if plate is not None and plate.shadow:
                # White ink meets the transparency, so the plate the rest of
                # the row gets would swallow the logo's outer edge. Shadow it
                # here, before the resize: with_shadow scales its blur to the
                # bitmap, so the halo comes out the same either way, and doing
                # it once covers both paths below.
                from ..imageutil import with_shadow

                poster = with_shadow(poster)
            # The checker is wrong below, not the code: PIL installs its
            # filter constants onto its own module with setattr() at
            # import time, so they exist at runtime and are invisible to
            # any static analysis. Spelling it Resampling.LANCZOS instead
            # would require Pillow >= 9.1, which this project does not pin.
            lanczos = PILImage.LANCZOS  # type: ignore[attr-defined]
            # Two kinds of artwork are never cover-cropped, and they are
            # asked about differently. `contain` is the CALLER's answer --
            # the fallback chain resolved to a wordmark this tile was not
            # shaped for, and cropping it would take the name off both ends.
            # `plate` is the PICTURE's: anything on a transparent background
            # is a mark rather than a photograph, which covers the channel
            # logos nothing declares a type for. Either is enough.
            if plate is None and not t.contain:
                if poster.size != (g.tile_w, g.tile_h):
                    # Cover-crop, like CSS object-fit: cover — scale to FILL
                    # the tile and crop the overflow, so odd-aspect art fills
                    # the frame edge to edge instead of letterboxing.
                    poster = ImageOps.fit(poster, (g.tile_w, g.tile_h),
                                          lanczos, centering=(0.5, 0.5))
                mask = None
                if rounded:
                    # Clip the art to the same rounded rect so its corners
                    # match. A square card needs no clip: the art is now
                    # exactly the card, edge to edge.
                    mask = PILImage.new("L", (g.tile_w, g.tile_h), 0)
                    ImageDraw.Draw(mask).rounded_rectangle(
                        [0, 0, g.tile_w - 1, g.tile_h - 1], radius=r, fill=255)
                if poster.mode == "RGBA":
                    # paste() takes ONE mask, so the art's own transparency has
                    # to be folded into the corner clip — passing the rounded
                    # rect alone would stamp the art opaquely over the card and
                    # put the black back.
                    from PIL import ImageChops

                    alpha = poster.getchannel("A")
                    mask = (alpha if mask is None
                            else ImageChops.multiply(mask, alpha))
                img.paste(poster, (x, 0), mask)
            else:
                if poster.size != (g.tile_w, g.tile_h):
                    poster = poster.copy()
                    poster.thumbnail((g.tile_w, g.tile_h), lanczos)
                px = x + (g.tile_w - poster.width) // 2
                py = (g.tile_h - poster.height) // 2
                img.paste(poster, (px, py),
                          poster if poster.mode == "RGBA" else None)
        elif t.glyph:
            # A muted centered glyph (first initial / ♪) so blank tiles read.
            gsize = max(_px(24), g.tile_h // 4)
            dr.text((x + g.tile_w / 2, g.tile_h / 2), t.glyph,
                    font=_font(gsize, bold=True, text=t.glyph), anchor="mm",
                    fill=theme.rgb(theme.SUBTLE_FG))
        if rounded:
            dr.rounded_rectangle(box, radius=r,
                                 outline=theme.rgb("101012", 255))
        else:
            dr.rectangle(box, outline=theme.rgb("101012", 255))

    def _paint_decorations(self, img, dr, x, t, g):
        # NB every bare offset in here is a logical constant being drawn
        # into a physical bitmap, so it goes through _px(). g is already
        # physical (see _compose); mixing the two silently is how a scaled
        # tile ends up with 1x decorations pinned to its corner.
        if t.watched:
            # A real Material `check`, not two hand-drawn strokes. Same
            # reason _paint_record gives for the record dot: this glyph is
            # drawn elsewhere in the app from `ui_icon_paths`, and a
            # hand-rolled second copy of one symbol drifts from the first.
            self._paint_glyph_badge(img, dr, x + _px(17), _px(17), "check",
                                    theme.ACCENT)
        # The top-right stack, filled from the corner leftwards. These used
        # to be an if/elif chain -- one badge won and the rest silently did
        # not draw, so a downloaded clip in a Home Videos library lost the
        # type marker that says it is a clip and not a folder. jellyfin-web
        # lays its indicators out in one flex row (`.cardIndicators`) for
        # the same reason: they answer different questions and any pair of
        # them can be true at once.
        #
        # Precedence is what sits IN the corner, not what draws at all: a
        # recording is the only one of these about something happening right
        # now, so it keeps the spot the eye goes to first.
        cy = _px(17)
        cx = x + g.tile_w - _px(17)
        if t.record:
            self._paint_record(img, cx, cy, t.record)
            cx -= _px(self.BADGE_PITCH)
        if t.downloaded:
            self._paint_glyph_badge(img, dr, cx, cy, "file_download",
                                    theme.ACCENT)
            cx -= _px(self.BADGE_PITCH)
        if t.kind:
            self._paint_kind(img, dr, cx, cy, t.kind)
            cx -= _px(self.BADGE_PITCH)
        if t.badge:
            bw = _px(26)
            dr.rounded_rectangle(
                [cx - bw // 2, _px(5), cx + bw // 2, _px(25)],
                radius=_px(6), fill=theme.rgb(theme.ACCENT, 255),
            )
            dr.text((cx, _px(15)), str(t.badge),
                    font=_font(g.badge_size, bold=True), anchor="mm",
                    fill=(255, 255, 255))
        if t.progress and t.progress > 0:
            frac = max(0.0, min(1.0, t.progress))
            bar = _px(6)
            # Red while recording: the bar means "how much of the broadcast
            # has gone", not "where you left off", and the accent colour is
            # spoken for by the second meaning everywhere else.
            fill = theme.FAV_RED if t.recording else theme.ACCENT
            if bool((theme.active() or {}).get("rounded")):
                from PIL import Image as PILImage, ImageChops, ImageDraw
                r = _px(14)
                # On a rounded card a square bar would fill the corner pixels
                # the silhouette leaves transparent, so it pokes visibly past
                # the curve. Draw it into its own layer and clip that to the
                # same rounded rect the poster uses.
                layer = PILImage.new("RGBA", (g.tile_w, g.tile_h),
                                     (0, 0, 0, 0))
                ld = ImageDraw.Draw(layer)
                ld.rectangle([0, g.tile_h - bar, g.tile_w - 1, g.tile_h - 1],
                             fill=theme.rgb(theme.PROGRESS_TRACK, 200))
                ld.rectangle(
                    [0, g.tile_h - bar,
                     int((g.tile_w - 1) * frac), g.tile_h - 1],
                    fill=theme.rgb(fill, 255),
                )
                mask = PILImage.new("L", (g.tile_w, g.tile_h), 0)
                ImageDraw.Draw(mask).rounded_rectangle(
                    [0, 0, g.tile_w - 1, g.tile_h - 1], radius=r, fill=255)
                layer.putalpha(ImageChops.multiply(layer.getchannel("A"),
                                                   mask))
                img.paste(layer, (x, 0), layer)
            else:
                dr.rectangle(
                    [x, g.tile_h - bar, x + g.tile_w - 1, g.tile_h - 1],
                    fill=theme.rgb(theme.PROGRESS_TRACK, 200))
                dr.rectangle(
                    [x, g.tile_h - bar,
                     x + int((g.tile_w - 1) * frac), g.tile_h - 1],
                    fill=theme.rgb(fill, 255),
                )

    #: Box the recording glyph is rasterized into. The Material record icons
    #: are a circle of radius 8 on a 24 canvas, so this gives a dot about
    #: 18px across at 1x — the size the other corner badges are.
    RECORD_BOX = 27

    #: Centre-to-centre spacing of the top-right badges. The discs are 22
    #: across, so this is them plus a 4px gap.
    BADGE_PITCH = 26

    #: Glyph box for a LINE icon on a badge (check, file_download), and for
    #: a SOLID one (folder, photo, videocam). Different on purpose, in a
    #: 22px disc that has to hold both.
    #:
    #: Material's 24-unit canvas carries its own padding, and the disc is
    #: more padding, so a glyph sized to the disc draws the symbol at about
    #: two thirds of it. That is invisible on a solid icon and ruinous on a
    #: line one: `check` is a ~2.2-unit stroke, which at a 15px box lands at
    #: 1.4 physical pixels -- thinner than the hand-drawn 2px strokes these
    #: replaced, which is exactly what they looked like. 20 puts the stroke
    #: back at ~1.8px. The solid icons stay smaller because their ink fills
    #: most of the canvas and 20 would have them touching the disc's edge.
    LINE_GLYPH = 20
    SOLID_GLYPH = 16

    @staticmethod
    def _paint_glyph_badge(img, dr, cx, cy, name, fill, size=None):
        """A Material glyph in white on a filled disc, centred on (cx, cy).

        The shape the watched check and the downloaded arrow share; the type
        marker (``_paint_kind``) is the same disc in the window colour,
        because it labels the item rather than reporting a state and should
        not read as another accent badge.
        """
        from ..mpvtk import vector

        r = _px(11)
        dr.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=theme.rgb(fill, 255))
        size = _px(size or StripStore.LINE_GLYPH)
        glyph = vector.icon_image(name, size, (255, 255, 255))
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    @staticmethod
    def _paint_kind(img, dr, cx, cy, name):
        """The type marker, centred on (cx, cy).

        Same disc as the state badges beside it but in the window colour,
        not the accent: this one says what the item *is*, permanently, and
        an accent chip on every tile in a Home Videos library reads as
        something having happened to all of them.
        """
        from ..mpvtk import vector

        r = _px(11)
        dr.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=theme.rgb(theme.WINDOW_BG, 210))
        size = _px(StripStore.SOLID_GLYPH)
        glyph = vector.icon_image(name, size, theme.rgb(theme.TEXT_FG))
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    @staticmethod
    def _paint_record(img, cx, cy, state):
        """The recording symbol, centred on (cx, cy).

        The **real Material glyphs**, rasterized from the same
        ``ui_icon_paths`` data the renderer draws icons from:
        ``fiber_manual_record`` for a single recording and
        ``fiber_smart_record`` for one covered by a series rule. Blitting
        rather than drawing by hand matters because the guide cell right
        next to this tile draws those same two icons as ASS — a hand-rolled
        approximation is a second drawing of the same symbol, and the two
        drift.

        Baked into the bitmap rather than drawn as a widget because mpv
        composites overlay bitmaps ABOVE all script ASS (GUIDE §6): an ASS
        glyph over a tile would vanish under the artwork.

        No outline. The red dot is the record symbol everywhere else in the
        app, and ringing this one in white made the same thing look like a
        different badge.
        """
        from ..mpvtk import vector

        name = ("fiber_smart_record" if state.startswith("series")
                else "fiber_manual_record")
        colour = theme.rgb(theme.SUBTLE_FG if state == "series_inactive"
                           else theme.FAV_RED)
        size = _px(StripStore.RECORD_BOX)
        glyph = vector.icon_image(name, size, colour)
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    def _paint_caption(self, dr, x, t, g):
        # The second line follows the first rather than sitting at a fixed
        # y: with "show titles" off and "show years" on there is no first
        # line, and the year used to be dropped entirely (#613). Leaving it
        # at the old offset instead would draw it a full line below the
        # artwork with a band of nothing between.
        y = g.tile_h + _px(6)
        # pilfont.draw_text, not dr.text: a caption mixing scripts needs a
        # face per run, or the Latin half of a Japanese title is tofu (see
        # mpvtk.pilfont.runs). Single-script captions -- almost all of them
        # -- take the same path they always did.
        from ..mpvtk import pilfont

        if t.title:
            fnt = _font(g.title_size, text=t.title)
            title = self._ellipsize(dr, t.title, fnt, g.tile_w)
            pilfont.draw_text(dr, (x, y), title, fnt,
                              fill=theme.rgb(theme.TEXT_FG))
            y += g.title_size + _px(7)
        if t.subtitle:
            fnt = _font(g.sub_size, text=t.subtitle)
            sub = self._ellipsize(dr, t.subtitle, fnt, g.tile_w)
            pilfont.draw_text(dr, (x, y), sub, fnt,
                              fill=theme.rgb(theme.SUBTLE_FG))

    @staticmethod
    def _ellipsize(dr, text, font, max_w):
        # Measured the way it will be drawn. The two disagree by a lot when
        # they disagree at all -- a face that cannot draw a run renders it as
        # .notdef boxes, which are wider than the digits they stand in for,
        # so measuring one way and drawing the other truncates a caption that
        # would have fitted.
        from ..mpvtk import pilfont

        if pilfont.text_length(dr, text, font) <= max_w:
            return text
        while text and pilfont.text_length(dr, text + "…", font) > max_w:
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
