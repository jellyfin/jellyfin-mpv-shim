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
any of them composites a new bitmap under a new src — the renderer's
overlay cache can never show stale content. The cache is LRU-bounded so
a long browse session doesn't grow without limit; anything on screen was
requested by the current build and is therefore most-recent.

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
from dataclasses import dataclass
from typing import Optional

from ..mpvtk.rawimage import write_bgra
from . import theme

log = logging.getLogger("mpvtk_browser.strips")

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
    MAX_ENTRIES = 48  # bounded footprint; evictions only hit off-window strips

    def __init__(self, cache_dir=None, mem_store=None, geom=None):
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
        self._lock = threading.Lock()
        self._counter = 0
        self.hits = 0
        self.misses = 0

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

    def strip(self, tiles, geom=None):
        """Composite ``tiles`` into one strip (in ``geom``'s shape, default
        the store's). Returns ``{"src", "iw", "ih", "regions"}`` where each
        region is ``{"x", "y", "w", "h", "key"}`` in image-local coords (the
        view wraps these with on_click/on_context/id)."""
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
        # Composited outside the lock: it is the expensive part and it does
        # not touch the cache.
        entry = self._compose(tiles, g)
        with self._lock:
            self._cache[key] = entry
            self._evict()
        return entry

    def bitmap(self, key, image):
        """Cache a single arbitrary image as BGRA (backdrops, logos, art) in
        the same store/LRU as strips. ``key`` must identify the content;
        ``image`` is a PIL image already at display size. Returns
        ``{"src", "iw", "ih"}``."""
        ck = ("bitmap", key)
        with self._lock:
            hit = self._cache.get(ck)
            if hit is not None:
                self._cache.move_to_end(ck)
                self.hits += 1
                return hit
            self.misses += 1
        src, w, h = self._store(image)
        entry = {"src": src, "iw": w, "ih": h}
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

        n = len(tiles)
        iw = n * g.tile_w + (n - 1) * g.gap if n else 1
        img = PILImage.new("RGBA", (iw, g.strip_h), (0, 0, 0, 0))
        dr = ImageDraw.Draw(img)
        regions = []
        for col, t in enumerate(tiles):
            x = col * (g.tile_w + g.gap)
            self._paint_poster(img, dr, x, t, g)
            self._paint_decorations(dr, x, t, g)
            self._paint_caption(dr, x, t, g)
            regions.append(
                {"x": x, "y": 0, "w": g.tile_w, "h": g.strip_h, "key": t.key}
            )
        src, iw2, ih2 = self._store(img)
        return {"src": src, "iw": iw2, "ih": ih2, "regions": regions}

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
            gsize = max(24, g.tile_h // 4)
            dr.text((x + g.tile_w / 2, g.tile_h / 2), t.glyph,
                    font=_font(gsize, bold=True, text=t.glyph), anchor="mm",
                    fill=theme.rgb(theme.SUBTLE_FG))
        dr.rectangle(
            [x, 0, x + g.tile_w - 1, g.tile_h - 1],
            outline=theme.rgb("101012", 255),
        )

    def _paint_decorations(self, dr, x, t, g):
        if t.watched:
            dr.ellipse([x + 6, 6, x + 28, 28],
                       fill=theme.rgb(theme.ACCENT, 255))
            dr.line([(x + 12, 17), (x + 16, 22), (x + 23, 12)],
                    fill=(255, 255, 255, 255), width=2)
        # Top-right corner: downloaded badge takes priority over the
        # unplayed-count badge (they rarely coexist).
        if t.downloaded:
            cx, cy = x + g.tile_w - 17, 17
            dr.ellipse([cx - 11, cy - 11, cx + 11, cy + 11],
                       fill=theme.rgb(theme.ACCENT, 255))
            dr.line([(cx, cy - 5), (cx, cy + 4)],
                    fill=(255, 255, 255, 255), width=2)
            dr.line([(cx - 4, cy), (cx, cy + 4), (cx + 4, cy)],
                    fill=(255, 255, 255, 255), width=2)
            dr.line([(cx - 5, cy + 7), (cx + 5, cy + 7)],
                    fill=(255, 255, 255, 255), width=2)
        elif t.badge:
            bw = 26
            dr.rounded_rectangle(
                [x + g.tile_w - bw - 5, 5, x + g.tile_w - 5, 25],
                radius=6, fill=theme.rgb(theme.ACCENT, 255),
            )
            dr.text((x + g.tile_w - 5 - bw / 2, 15), str(t.badge),
                    font=_font(g.badge_size, bold=True), anchor="mm",
                    fill=(255, 255, 255))
        if t.progress and t.progress > 0:
            frac = max(0.0, min(1.0, t.progress))
            dr.rectangle([x, g.tile_h - 6, x + g.tile_w - 1, g.tile_h - 1],
                         fill=theme.rgb(theme.PROGRESS_TRACK, 200))
            dr.rectangle(
                [x, g.tile_h - 6,
                 x + int((g.tile_w - 1) * frac), g.tile_h - 1],
                fill=theme.rgb(theme.ACCENT, 255),
            )

    def _paint_caption(self, dr, x, t, g):
        if t.title:
            fnt = _font(g.title_size, text=t.title)
            title = self._ellipsize(dr, t.title, fnt, g.tile_w)
            dr.text((x, g.tile_h + 6), title, font=fnt,
                    fill=theme.rgb(theme.TEXT_FG))
        if t.subtitle:
            fnt = _font(g.sub_size, text=t.subtitle)
            sub = self._ellipsize(dr, t.subtitle, fnt, g.tile_w)
            dr.text((x, g.tile_h + 6 + g.title_size + 7), sub,
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
        if self.mem is not None:
            return self.mem.add(img)
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
        src = os.path.join(self.dir, "strip%d.bgra" % counter)
        w, h = write_bgra(img, src)
        return src, w, h

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
