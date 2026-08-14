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

import dataclasses
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


#: Supersample factor for the badge shapes. PIL has no antialiased fill, so
#: the disc and the chip behind a badge came out with a visible staircase on
#: their curves -- next to a Material glyph that IS antialiased (vector._SS),
#: which is what made the marks read as thin and hand-drawn rather than as
#: the same furniture jellyfin-web puts there [iw]. 4 is enough at these
#: sizes and the masks are cached by shape, so the cost is paid once per
#: distinct badge geometry rather than per tile.
_AA_SS = 4

#: {(w, h, radius): L mask}. Bounded by the handful of badge shapes a theme
#: and a scale factor produce, so no eviction: a badge is a fixed logical
#: size, and the only thing that moves it is the HiDPI scale.
_aa_masks: dict = {}


def _aa_mask(w, h, radius=None):
    """An antialiased ``L`` coverage mask of an ellipse (``radius`` None) or
    a rounded rectangle, ``w`` x ``h``.

    Drawn at ``_AA_SS`` times the size and box-averaged down -- BOX rather
    than LANCZOS because the ratio is exact, which makes it the true area
    coverage; LANCZOS's negative lobes put a faint dark ring outside a shape
    whose mask should simply reach zero.
    """
    key = (int(w), int(h), radius if radius is None else int(radius))
    mask = _aa_masks.get(key)
    if mask is not None:
        return mask
    from PIL import Image as PILImage, ImageDraw

    w, h = max(1, int(w)), max(1, int(h))
    big = PILImage.new("L", (w * _AA_SS, h * _AA_SS), 0)
    box = [0, 0, w * _AA_SS - 1, h * _AA_SS - 1]
    draw = ImageDraw.Draw(big)
    if radius is None:
        draw.ellipse(box, fill=255)
    else:
        draw.rounded_rectangle(box, radius=int(radius) * _AA_SS, fill=255)
    mask = big.resize((w, h), PILImage.BOX)  # type: ignore[attr-defined]
    _aa_masks[key] = mask
    return mask


def _aa_fill(img, box, colour, radius=None):
    """Fill ``box`` (``[x0, y0, x1, y1]``, inclusive) in ``img`` with an
    antialiased ellipse or rounded rectangle.

    ``colour`` is an ``(r, g, b)`` or ``(r, g, b, a)`` tuple; an alpha there
    scales the coverage rather than replacing it, so a translucent badge
    keeps its soft edge. Uses ``paste``, not ``alpha_composite``, because a
    badge near the end of a strip may hang past its bounds and paste clips
    where the other raises.
    """
    x0, y0, x1, y1 = (int(v) for v in box)
    w, h = x1 - x0 + 1, y1 - y0 + 1
    if w <= 0 or h <= 0:
        return
    mask = _aa_mask(w, h, radius)
    alpha = colour[3] if len(colour) > 3 else 255
    if alpha < 255:
        mask = mask.point(lambda v, a=alpha: (v * a) // 255)
    img.paste(tuple(colour[:3]) + (255,), (x0, y0), mask)


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
    #: How many lines ``_paint_caption`` may draw, and how much room
    #: ``caption_h`` was sized for. Two everywhere except a Live TV listing
    #: drawn at poster width -- see :meth:`with_caption_lines`.
    caption_lines: int = 2

    #: Baseline-to-baseline slack under the third caption line. Tighter than
    #: the 7 between title and subtitle: the two subtitle lines are one
    #: thought split in half ("BBC Two" / "20:00 - 20:30"), so they read as a
    #: block, and the band is being made taller to fit them.
    SUB_GAP = 4

    def with_caption_lines(self, n):
        """A copy with room for ``n`` caption lines instead of two.

        **Idempotent** -- ``caption_lines`` is stored, so a geometry that
        already has three is returned unchanged. That is what lets the
        decision be made in more than one place (``auto_geom`` for a row that
        shapes itself, ``GridPage._grid_shape`` for one told what shape to
        be) without the band growing once per caller.

        The band, not just the permission: ``_paint_caption`` lays out inside
        a fixed ``caption_h`` and nothing clips it back, so a third line
        drawn into a two-line band draws over the top of the next grid row.
        """
        if n <= self.caption_lines:
            return self
        extra = (n - self.caption_lines) * (self.sub_size + self.SUB_GAP)
        return dataclasses.replace(self, caption_lines=n,
                                   caption_h=self.caption_h + extra)

    def with_text_scale(self, factor, minimum=0):
        """A copy with the caption text scaled, and room made for it.

        The caption sizes are **geometry**, not type-scale tiers: they are
        baked into the strip bitmap by Pillow, so the user's text preference
        has to be applied here separately -- it does not arrive through
        `theme.size()` like everything drawn as a text node, which is why
        turning text up left the tiles alone.

        This is the ONLY thing that moves a tile's type. Cover Size used to
        as well, silently; see :meth:`scaled` for why it no longer does.

        ``caption_h`` grows with them. It is a fixed band under the
        artwork and `_paint_caption` lays out inside it as title, a 7px
        gap, then subtitle -- 15 + 7 + 13 in 46px at stock. Scale the type
        without the band and the subtitle is simply clipped away.

        The band is measured against ``caption_lines``, not against two, or
        a three-line Live TV caption loses its air time to exactly the same
        clipping the moment somebody turns text up.
        """
        def scaled(value):
            return max(1, int(round(value * factor)), int(minimum or 0))

        title, sub = scaled(self.title_size), scaled(self.sub_size)
        subs = max(0, self.caption_lines - 1)
        # What the painter needs, plus the slack the stock band already
        # carries -- so a caption keeps the breathing room it was designed
        # with instead of being cropped to its own text.
        slack = self.caption_h - (self.title_size + self.sub_size * subs)
        return dataclasses.replace(
            self, title_size=title, sub_size=sub,
            badge_size=scaled(self.badge_size),
            caption_h=max(self.caption_h,
                          title + sub * subs + max(slack, 0)))

    @property
    def strip_h(self):
        return self.tile_h + self.caption_h

    def physical(self):
        """A copy with every pixel field converted to physical px.

        TileGeom is authored in LOGICAL units like the rest of the view
        layer; only the compositor below works in physical pixels, because
        the renderer crops bitmaps rather than resampling them.

        ``replace``, not a fresh TileGeom: the fields that are NOT pixels
        (``caption_lines``) have to survive the conversion, and spelling the
        pixel ones out in a constructor call is how one gets left behind."""
        from ..mpvtk.scaling import px

        return dataclasses.replace(
            self,
            tile_w=px(self.tile_w), tile_h=px(self.tile_h), gap=px(self.gap),
            caption_h=px(self.caption_h), title_size=px(self.title_size),
            sub_size=px(self.sub_size), badge_size=px(self.badge_size),
        )

    def scaled(self, f):
        """A copy whose ARTWORK is scaled by factor ``f`` — the active theme's
        cover size, or the user's Cover Size setting.

        **The type is not touched, and neither is the caption band.** Cover
        Size used to scale ``title_size``/``sub_size``/``badge_size``/
        ``caption_h`` along with the art, on the reasoning that a label under
        a bigger poster should be bigger. Three things were wrong with that:

        * it is a second, unlabelled text-size control, so a user who wanted
          bigger covers got bigger captions they did not ask for -- and one
          who wanted *smaller* covers (Extra Compact is 0.75) got captions
          below the floor ``ui_text_min`` exists to enforce;
        * the badge decorations do NOT scale with it. Every offset in
          ``_paint_decorations`` is a fixed logical constant (the 22px disc,
          ``BADGE_PITCH``, the 17px corner inset), so ``badge_size`` growing
          alone put a 24px numeral in a 22px disc at Extra Large;
        * text scaling already has two controls that do it properly and
          keep working here -- ``ui_scale`` through :meth:`physical`, which
          scales the whole interface including these, and ``ui_text_scale`` /
          ``ui_text_min`` through :meth:`with_text_scale`, which is applied
          *after* this and is what a theme's pinned caption size is measured
          against.

        The gap is kept too, so rows keep their rhythm as the art grows.
        """
        if not f or abs(f - 1.0) < 1e-6:
            return self

        def r(v):
            return max(1, int(round(v * f)))
        return dataclasses.replace(self, tile_w=r(self.tile_w),
                                   tile_h=r(self.tile_h))


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
    #: A THIRD caption line, drawn under ``subtitle``. Empty everywhere
    #: except a Live TV listing on a tile too narrow to carry "BBC Two ·
    #: 20:00 - 20:30" on one line -- see ``TileRenderer.caption_geom``. The
    #: geometry has to agree (``TileGeom.caption_lines``); this alone does
    #: not make room for it.
    subtitle2: str = ""
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
    #: How many versions of this item the library holds (MediaSourceCount).
    #: Drawn only above 1, which is also the only case the server sends it
    #: -- so 0 and 1 both mean "one version, nothing to say".
    sources: int = 0


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

    #: ...and a bound on the BYTES, because the count says nothing about
    #: them. A strip is a whole row, not a screenful of one: a 50-tile
    #: carousel at 4K with a 2x scale composites to 15372x512x4 = **31 MiB
    #: in one entry**, so eighty of those is not a cache, it is a leak with
    #: a ceiling. Where those bytes land depends on the backend and both are
    #: worth caring about -- ctypes buffers in this process on libmpv, BGRA
    #: files in the session's tmpfs on jsonipc -- and the machines that
    #: suffer are the ones with the least to spare.
    #:
    #: 128 MiB holds a 4K screenful several times over (one visible row is
    #: ~31 MiB at worst, an ordinary 1080p row ~1.6 MiB) and costs 1.6% of
    #: an 8 GiB laptop. A miss is a recomposite on the worker pool, not a
    #: refetch: 20-140ms, off the loop thread, with the placeholder path
    #: already built for exactly that wait.
    MAX_BYTES = 128 * 1024 * 1024

    #: ...and what that budget becomes on a machine short of RAM.
    #:
    #: These bytes ARE memory on both backends -- ctypes buffers in this
    #: process on libmpv, and on mpv_ext files in a scratch directory that
    #: is RAM-backed wherever one is available. That is not a corner case:
    #: it is the configuration that filled a VM's memory and started all of
    #: this. A small machine's /run/user is small too, so the cache lands in
    #: /dev/shm, which is RAM by another name -- 128 MiB of it is 6% of a
    #: 2 GiB box, for a cache. Enough for a screenful and a bit, which is
    #: what a cache has to be to be worth having at all; the rest of the
    #: trade is made by shedding on navigation.
    TIGHT_MAX_BYTES = 32 * 1024 * 1024

    #: How many pushed scenes back a bitmap is treated as possibly-bound.
    #: See _protected.
    PROTECT_GENERATIONS = 2

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
        #: Bytes of bitmap the cache is holding, and the pushed-scene
        #: generation that decides what may be freed -- see _protected.
        self._bytes = 0
        self._gen = 0
        self._marked = False
        #: Generation at which a requested full trim may run; see trim_soon.
        self._trim_at = None
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
            t.subtitle2,
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
            int(t.sources),
        )

    @staticmethod
    def _geom_key(g):
        return (g.tile_w, g.tile_h, g.gap, g.caption_h,
                g.title_size, g.sub_size, g.badge_size, g.caption_lines)

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
                self._touch(key)
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
            return self._insert(key, entry)

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
            self._insert(key, entry)
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
                self._touch(bkey)
                return hit
        entry = self._compose_blank(n, g)
        with self._lock:
            # Another thread may have built the same shape while we composed.
            return self._insert(bkey, entry)

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
                self._touch(ck)
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
            return self._insert(ck, entry)

    def clear(self):
        """Drop every cached bitmap and release its backing buffer.

        On the libmpv path those buffers are read BY ADDRESS by mpv, so this
        must only be called once mpv is genuinely dead — see
        mpvtk_browser.ui.on_mpv_terminated. Calling it while mpv is still
        compositing is a segfault, not a leak."""
        with self._lock:
            entries = list(self._cache.values())
            self._cache.clear()
            self._bytes = 0
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
            # A muted centred mark so a blank tile still reads. Usually a
            # Material icon -- jellyfin-web's own per-type default, so a
            # library with no artwork looks like it does in every other
            # client -- and a letter only where no icon fits (see
            # components.labels.placeholder_glyph).
            #
            # Told apart by looking the name up, not by length: "?" and a
            # one-letter initial are both legitimate answers, and an icon
            # name is never one character.
            from ..mpvtk import vector

            gsize = max(_px(24), g.tile_h // 4)
            if t.glyph in vector.ICON_PATHS:
                glyph = vector.icon_image(
                    t.glyph, gsize, theme.rgb(theme.SUBTLE_FG))
                img.paste(glyph,
                          (int(x + (g.tile_w - gsize) // 2),
                           int((g.tile_h - gsize) // 2)), glyph)
            else:
                dr.text((x + g.tile_w / 2, g.tile_h / 2), t.glyph,
                        font=_font(gsize, bold=True, text=t.glyph),
                        anchor="mm", fill=theme.rgb(theme.SUBTLE_FG))
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
        #
        # The two corners are jellyfin-web's, and they were not: the watched
        # tick used to share the top-LEFT one with the version count, on the
        # reasoning that the right-hand corner was already three deep. That
        # is the wrong trade. Web puts the played state top-right
        # (`.cardIndicators`) and the version count top-left
        # (`.mediaSourceIndicator`), and scanning a season for the episodes
        # you have NOT seen is the thing this badge is looked at for -- so
        # having it on the side no other client puts it costs a beat every
        # time, on the one screen where it is read in bulk [iw].
        inset = _px(17)
        # The top-LEFT corner: web's `.mediaSourceIndicator`, and it holds
        # this one thing.
        if t.sources > 1:
            # "This film is here twice" -- a 4K and a 1080p, a theatrical and
            # an extended. The count, not a symbol, because which of them
            # plays is a choice the detail page offers and the number is what
            # says there is one to make.
            self._paint_count_badge(img, dr, x + inset, inset,
                                    str(t.sources), g.badge_size)
        # The top-right stack, filled from the corner leftwards. These used
        # to be an if/elif chain -- one badge won and the rest silently did
        # not draw, so a downloaded clip in a Home Videos library lost the
        # type marker that says it is a clip and not a folder. jellyfin-web
        # lays its indicators out in one flex row (`.cardIndicators`) for
        # the same reason: they answer different questions and any pair of
        # them can be true at once.
        #
        # The ORDER is web's, read backwards: `.cardIndicators` is a
        # right-anchored flex row, so its last child is the one in the
        # corner, and cardBuilder emits sync, timer, type, played. From the
        # corner leftwards that is played, type, timer, sync -- below.
        cy = inset
        cx = x + g.tile_w - inset
        if t.badge:
            cx -= self._paint_count_chip(img, dr, cx, cy, str(t.badge),
                                         g.badge_size)
        elif t.watched:
            # The one place two of these DO share a slot, and the exception
            # is web's rather than a re-run of the bug above:
            # `getPlayedIndicatorHtml` returns the unplayed count *or* the
            # tick, never both, because they are two readings of one fact.
            # Nothing can set both here either -- a container is watched at
            # UnplayedItemCount == 0, which is exactly when there is no
            # count to draw -- so the elif is a statement of that, not a
            # precedence rule hiding a badge.
            #
            # A real Material `check`, not two hand-drawn strokes. Same
            # reason _paint_record gives for the record dot: this glyph is
            # drawn elsewhere in the app from `ui_icon_paths`, and a
            # hand-rolled second copy of one symbol drifts from the first.
            self._paint_glyph_badge(img, dr, cx, cy, "check", theme.ACCENT)
            cx -= _px(self.BADGE_PITCH)
        if t.kind:
            self._paint_kind(img, dr, cx, cy, t.kind)
            cx -= _px(self.BADGE_PITCH)
        if t.record:
            self._paint_record(img, cx, cy, t.record)
            cx -= _px(self.BADGE_PITCH)
        if t.downloaded:
            self._paint_glyph_badge(img, dr, cx, cy, "file_download",
                                    theme.ACCENT)
            cx -= _px(self.BADGE_PITCH)
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
                             fill=theme.rgb(theme.PROGRESS_TRACK,
                                            StripStore.PROGRESS_TRACK_ALPHA))
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
                    fill=theme.rgb(theme.PROGRESS_TRACK,
                                   StripStore.PROGRESS_TRACK_ALPHA))
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

    #: Alpha the resume bar's groove is drawn at -- jellyfin-web's
    #: `.itemProgressBar` is `rgba(51, 51, 51, .8)`, and both halves of that
    #: matter (see the palette's PROGRESS_TRACK).
    PROGRESS_TRACK_ALPHA = 204

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

    #: How much bigger a mark is drawn when the theme replaces its pill with
    #: a shadow. The pill's own 22px disc is doing part of the work of being
    #: *seen*; take it away and the mark has to carry that itself. Still
    #: inside BADGE_PITCH (26), so the stack does not collide.
    SHADOW_GLYPH = 1.15

    #: Blur radius and drop, as fractions of the MARK's own size (not of the
    #: padded layer's -- that would make them depend on the padding computed
    #: from them).
    SHADOW_BLUR = 9.0
    SHADOW_DROP = 11.0

    @staticmethod
    def shadow_pad(size):
        """Room to leave round a mark of ``size`` px for its shadow.

        Derived from the blur, not a constant. It was a flat 5px, and a
        Gaussian needs about three sigma before it has faded: at the badge
        sizes in play sigma is 2 or more, so the halo was still visibly dark
        where the layer ended and every numeric badge carried a squared-off
        corner of shadow. Under-padding a blur does not soften the edge, it
        *makes* one.
        """
        blur = max(1.0, size / StripStore.SHADOW_BLUR)
        return int(-(-blur * 3 // 1)) + max(1, round(size /
                                                     StripStore.SHADOW_DROP))

    @staticmethod
    def shadowed_badges():
        """Whether the active theme draws badges as a mark plus a drop shadow
        instead of a mark on a filled pill.

        Read per paint rather than captured: a theme change retags the strip
        store, so every strip recomposites through here with the new answer
        (see ``MpvtkBrowser.set_theme``).
        """
        return bool((theme.active() or {}).get("badge_shadow"))

    #: Gain applied to a badge's blurred silhouette before it is used as the
    #: shadow's alpha. Above 1, so the halo SATURATES near the mark and only
    #: falls off at its rim -- a plain blur of a 20px glyph is a faint grey
    #: smudge that a white poster swallows whole, which is precisely the case
    #: the pill was there for.
    SHADOW_GAIN = 2.4

    @staticmethod
    def _shadowed(img, layer, span, cx, cy):
        """Composite ``layer`` -- a mark on transparency, padded by
        :meth:`shadow_pad` -- centred on (cx, cy), over a dark halo of its
        own silhouette.

        ``span`` is the size the blur is derived from: the mark's own, NOT
        the padded layer's (which is computed from the blur, so measuring it
        here would grow one every time the other grew to hold it). For a
        glyph that is its box; for text it is the cap height, because what a
        shadow is proportional to is the weight of the ink -- taking the
        WIDTH made a three-digit count three times blurrier than a
        one-digit one and gave it a 36px layer to live in, which centred
        17px below the top of a card does not fit and was clipped.

        **Not ``imageutil.with_shadow``.** That one is tuned for a logo: a
        large mark with margins, wanting a hint of separation from a plate it
        is nearly the colour of, so its blur is a sixtieth of the image and
        its alpha is a straight multiply. This is a 20px glyph over
        *artwork*, which can be any colour including white, with nothing else
        holding it up -- the halo is not a hint, it is the entire reason the
        mark is visible. Hence a blur about a tenth of the mark, an offset
        big enough to read as a direction rather than a ring, and a gain that
        drives the middle of it opaque.
        """
        from PIL import Image as PILImage, ImageFilter

        drop = max(1, round(span / StripStore.SHADOW_DROP))
        silhouette = PILImage.new("L", layer.size, 0)
        silhouette.paste(layer.getchannel("A"), (0, drop))
        silhouette = silhouette.filter(
            ImageFilter.GaussianBlur(max(1.0, span / StripStore.SHADOW_BLUR)))
        gain = StripStore.SHADOW_GAIN
        silhouette = silhouette.point(
            lambda v: min(255, int(v * gain)))
        shadow = PILImage.new("RGBA", layer.size, (0, 0, 0, 0))
        shadow.putalpha(silhouette)
        layer = PILImage.alpha_composite(shadow, layer)
        img.paste(layer, (int(cx - layer.width // 2),
                          int(cy - layer.height // 2)), layer)

    @staticmethod
    def _mark_layer(size):
        """A transparent square with room round it for :meth:`_shadowed`."""
        from PIL import Image as PILImage

        pad = StripStore.shadow_pad(size)
        return PILImage.new("RGBA", (size + 2 * pad, size + 2 * pad),
                            (0, 0, 0, 0)), pad

    @staticmethod
    def _paint_glyph_badge(img, dr, cx, cy, name, fill, size=None):
        """A Material glyph in white on a filled disc, centred on (cx, cy).

        The shape the watched check and the downloaded arrow share; the type
        marker (``_paint_kind``) is the same disc in the window colour,
        because it labels the item rather than reporting a state and should
        not read as another accent badge.

        With ``badge_shadow`` the disc goes and a drop shadow takes over the
        job of separating the mark from the artwork. **The mark stays white**
        rather than adopting ``fill``: the pill is what makes an accent
        legible on a photograph, so an accent-coloured mark with no pill is
        legible only for themes whose accent happens to be bright, and
        nothing guarantees one is -- Super Dark, the first theme to ask for
        this, has a light grey accent, but a theme is free to set a dark
        one and the option must not depend on which. White reads on
        everything, which is the property being bought here.
        """
        from ..mpvtk import vector

        size = _px(size or StripStore.LINE_GLYPH)
        if StripStore.shadowed_badges():
            size = int(size * StripStore.SHADOW_GLYPH)
            layer, pad = StripStore._mark_layer(size)
            glyph = vector.icon_image(name, size, (255, 255, 255))
            layer.paste(glyph, (pad, pad), glyph)
            StripStore._shadowed(img, layer, size, cx, cy)
            return
        r = _px(11)
        _aa_fill(img, [cx - r, cy - r, cx + r, cy + r], theme.rgb(fill, 255))
        glyph = vector.icon_image(name, size, (255, 255, 255))
        img.paste(glyph, (int(cx - size // 2), int(cy - size // 2)), glyph)

    @staticmethod
    def _paint_count_chip(img, dr, cx, cy, text, size):
        """The unplayed-episode chip, centred on ``cx`` and pinned by its
        RIGHT edge. Returns the horizontal pitch the next badge must clear.

        Sized to the number it carries, not to a guess about how big numbers
        get. This was a fixed 26 logical px, which is three physical px
        NARROWER than "123" draws at the default badge size -- so a
        three-digit count (routine on an unwatched anime series, and
        reachable by anyone who adds a show and never starts it) hung out of
        both ends of its own chip. Two digits fitted, with 3px of padding
        against the single digit's 8.

        jellyfin-web's `.countIndicator` is the same shape and grows the same
        way: `padding: 0 .5em` over a min-width. Pinned by its right edge
        rather than its centre because that edge is the one lined up with the
        badge stack beside it -- growing from the middle would walk a wide
        chip off the corner of the card.

        **A rounded rectangle where every other badge is a disc**, which is
        web's split too: this one runs to three digits, while the version
        count beside it is a count of files on disk and stays at one.

        The returned pitch is the reason this is a function rather than four
        lines inline. It sits IN the corner now (see
        :meth:`_paint_decorations`), so unlike every disc in the stack the
        badge to its left has to clear a width that depends on the text --
        BADGE_PITCH is a floor here, not the answer.
        """
        font = _font(size, bold=True)
        right = cx + _px(13)
        if StripStore.shadowed_badges():
            # Same right edge, so it still lines up with the stack; the
            # chip's own 14px of horizontal padding goes with the chip.
            width = StripStore._shadowed_text(img, text, font, 0, cy,
                                              right=right)
            # The measured width, exactly as the pill branch below returns
            # one. Returning BADGE_PITCH here was the bug: this branch draws
            # a bare shadowed NUMBER, which is wider than the disc the pitch
            # describes as soon as it has two digits -- 5px of overlap at
            # the stock text size, 15px at three digits -- so the count was
            # drawn over the badge to its left, both in white, both haloed.
            # It could not fire until the chip moved into the corner and
            # gained a neighbour.
            return max(_px(StripStore.BADGE_PITCH), width + _px(4))
        bw = max(_px(26), int(dr.textlength(text, font=font)) + _px(14))
        _aa_fill(img, [right - bw, _px(5), right, _px(25)],
                 theme.rgb(theme.ACCENT, 255), radius=_px(6))
        dr.text((right - bw / 2, _px(15)), text, font=font, anchor="mm",
                fill=(255, 255, 255))
        # The chip's own width plus the gap a disc's pitch leaves round it
        # (BADGE_PITCH is 26 against a 22px disc), floored at that pitch so a
        # narrow chip lines the stack up exactly as a disc would.
        return max(_px(StripStore.BADGE_PITCH), bw + _px(4))

    @staticmethod
    def _paint_count_badge(img, dr, cx, cy, text, size):
        """A number in white on a filled accent disc, centred on (cx, cy).

        The same disc ``_paint_glyph_badge`` draws, carrying a digit instead
        of a glyph -- which is how jellyfin-web draws its version count too
        (`.mediaSourceIndicator` is a 2em circle in the primary colour). The
        unplayed-episode badge on the other corner is deliberately a rounded
        *rectangle*: it runs to three digits, where this one is a count of
        files on disk and stays one.
        """
        font = _font(size, bold=True)
        if StripStore.shadowed_badges():
            StripStore._shadowed_text(img, str(text), font, cx, cy)
            return
        r = _px(11)
        _aa_fill(img, [cx - r, cy - r, cx + r, cy + r],
                 theme.rgb(theme.ACCENT, 255))
        dr.text((cx, cy), text, font=font, anchor="mm",
                fill=(255, 255, 255))

    @staticmethod
    def _shadowed_text(img, text, font, cx, cy, right=None):
        """White text with a drop shadow, no chip behind it.

        Centred on (cx, cy), or pinned by its RIGHT edge when ``right`` is
        given -- which is what the episode count needs, for the reason
        ``_paint_decorations`` gives: that edge is the one lined up with the
        badge stack, and a wide chip growing from its middle walks off the
        corner of the card.
        """
        from PIL import Image as PILImage, ImageDraw

        probe = ImageDraw.Draw(PILImage.new("RGBA", (1, 1)))
        box = probe.textbbox((0, 0), text, font=font, anchor="lt")
        tw, th = max(1, box[2] - box[0]), max(1, box[3] - box[1])
        # Off the cap HEIGHT, so every count -- "2", "12", "128" -- carries
        # the same weight of shadow as the glyph badges beside it. Off the
        # width it grew with the digit count, which both looked wrong (a wide
        # soft blob next to tight little marks) and did not fit: see
        # _shadowed.
        pad = StripStore.shadow_pad(th)
        layer = PILImage.new("RGBA", (tw + 2 * pad, th + 2 * pad), (0, 0, 0, 0))
        ImageDraw.Draw(layer).text((pad - box[0], pad - box[1]), text,
                                   font=font, anchor="lt",
                                   fill=(255, 255, 255, 255))
        if right is not None:
            cx = right - layer.width // 2
        StripStore._shadowed(img, layer, th, cx, cy)
        # The drawn width, so a caller pinning by `right` can tell the badge
        # beside it how far to move. There is no other way to know: the
        # layer is sized from the rendered glyphs plus a shadow pad taken
        # off the cap height, neither of which the caller can compute.
        return layer.width

    @staticmethod
    def _paint_kind(img, dr, cx, cy, name):
        """The type marker, centred on (cx, cy).

        Same disc as the state badges beside it but in the window colour,
        not the accent: this one says what the item *is*, permanently, and
        an accent chip on every tile in a Home Videos library reads as
        something having happened to all of them.

        With ``badge_shadow`` it keeps TEXT_FG rather than going white with
        the state badges: the distinction this marker exists to make is that
        it is *not* one of them, and the pill going away is exactly when that
        distinction has the least left to stand on.
        """
        from ..mpvtk import vector

        size = _px(StripStore.SOLID_GLYPH)
        if StripStore.shadowed_badges():
            size = int(size * StripStore.SHADOW_GLYPH)
            layer, pad = StripStore._mark_layer(size)
            glyph = vector.icon_image(name, size, theme.rgb(theme.TEXT_FG))
            layer.paste(glyph, (pad, pad), glyph)
            StripStore._shadowed(img, layer, size, cx, cy)
            return
        r = _px(11)
        _aa_fill(img, [cx - r, cy - r, cx + r, cy + r],
                 theme.rgb(theme.WINDOW_BG, 210))
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
        if StripStore.shadowed_badges():
            # It keeps its COLOUR where the state badges go white -- the red
            # dot is the record symbol everywhere in the app and a white one
            # would read as a different badge -- but it takes the shadow.
            # This was the one mark in the corner that consulted neither, so
            # on a badge_shadow theme it was the only badge with nothing at
            # all separating it from the artwork: a red dot on red artwork.
            # A half-applied option is worse than an unapplied one.
            layer, pad = StripStore._mark_layer(size)
            glyph = vector.icon_image(name, size, colour)
            layer.paste(glyph, (pad, pad), glyph)
            StripStore._shadowed(img, layer, size, cx, cy)
            return
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
            y += g.sub_size + _px(TileGeom.SUB_GAP)
        # Third line, and ONLY where the geometry was widened for it. A tile
        # carrying one in a two-line band would draw it over the row below --
        # nothing crops a caption back to caption_h, the band is a promise
        # this painter keeps rather than a clip. The two are set together by
        # TileRenderer.caption_geom / _tile, so this is a belt-and-braces
        # guard against a Tile built for one geometry and drawn in another.
        if t.subtitle2 and g.caption_lines >= 3:
            fnt = _font(g.sub_size, text=t.subtitle2)
            sub = self._ellipsize(dr, t.subtitle2, fnt, g.tile_w)
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

    @staticmethod
    def _entry_bytes(entry):
        """What one cached bitmap costs, wherever it is kept.

        The same number either way: a ctypes buffer on libmpv and a
        strip*.bgra on jsonipc are both iw*ih*4 bytes of premultiplied BGRA.
        """
        try:
            return int(entry["iw"]) * int(entry["ih"]) * 4
        except Exception:
            return 0

    def _insert(self, key, entry):
        """Put ``entry`` in the cache under ``key``, or drop it for the one
        already there. Returns the entry the caller should use. Callers hold
        ``_lock``.

        The re-check is the point. Every insert site drops the lock across
        the composite, so two callers can miss the same key and both
        allocate -- and the same key really does arrive by two routes: a
        grid composites through the pool (async_=True) while the paginated
        view of the same items composites inline, and the keys are equal.
        Overwriting would strand the loser's buffer with no cache reference
        to free it by, and -- since this cache now counts bytes -- would add
        its size to a total nothing ever subtracts, until the drift alone
        exceeds MAX_BYTES and the cache trims itself to nothing on every
        insert.
        """
        hit = self._cache.get(key)
        if hit is not None:
            if hit is not entry:
                self._free(entry["src"])
            self._touch(key)
            return hit
        self._cache[key] = entry
        self._bytes += self._entry_bytes(entry)
        self._touch(key)
        self._evict()
        return entry

    def on_scene_pushed(self):
        """A scene has been handed to the renderer. Call once per PUSH.

        This is the clock the byte bound is safe against, and "a scene was
        pushed" is deliberately not "build() ran" -- they are different
        events and using the wrong one frees bitmaps mpv is compositing:

        * ``MpvtkApp.render`` builds a SECOND time when a scene turns up
          glyphs whose widths were never measured, which is exactly what a
          new screen does. Two builds, one push.
        * A build that raises keeps the previous frame on screen (by design
          -- views index into state that arrives asynchronously). No push at
          all, and the scene still up is the one before it.

        Counting builds, both of those slide the protected window off the
        scene that is actually on screen. Counting pushes cannot: the
        generation only advances when the renderer has been given something
        new.
        """
        with self._lock:
            self._gen += 1
            self._marked = True
            if self._trim_at is not None and self._gen >= self._trim_at:
                self._trim_at = None
                self._trim_to(0)

    def keep(self, entry):
        """Say that ``entry`` is still on screen, for a caller that holds one
        across frames instead of asking for it again.

        Almost everything re-requests what it draws every frame -- a strip
        row, a placeholder, an art cell -- so the ordinary cache hit is also
        the liveness signal. The cast screen does not: it composites one
        full-window bitmap on a worker, parks the entry, and renders from
        that entry forever after. Nothing would touch it again, so it would
        age out of the protected window while being the only thing on
        screen, and its src is the biggest single buffer this app makes.
        """
        if not entry:
            return
        src = entry.get("src") if isinstance(entry, dict) else None
        with self._lock:
            for key, cached in self._cache.items():
                if cached is entry or (src and cached.get("src") == src):
                    self._touch(key)
                    return

    def set_memory_pressure(self, tight):
        """Switch between the roomy and the small-machine byte budget.

        Asked per screen change rather than once at startup, for the same
        reason the trim is: "busy" is a state, not a property. Lowering the
        cap takes effect at the next eviction, and _protected still decides
        what may actually go -- so this can never free something on screen,
        however far the budget drops.
        """
        want = self.TIGHT_MAX_BYTES if tight else type(self).MAX_BYTES
        if want == self.MAX_BYTES:
            return
        with self._lock:
            self.MAX_BYTES = want
            self._evict()

    def trim_soon(self):
        """Free everything the live scene is not using, at the first moment
        that is safe -- which is not now.

        For the small-machine path (see MpvtkBrowser._shed_caches_on_screen_
        change): on a machine short of RAM, the rows of the screen you just
        left are worth more as memory than as a fast trip back.

        Deferred to the next push, and then still filtered by _protected.
        When a screen changes the scene mpv is compositing is STILL the old
        one, so the old strips are precisely the ones that must not be freed
        at that instant; a few generations later they are nobody's and the
        ordinary gate lets them go.
        """
        with self._lock:
            # Not "the next push": the screen being left has to age out of
            # the protected window first, and _trim_to would simply refuse
            # to free it before then -- consuming the request and leaving
            # the memory held. Book it for the generation it can actually
            # happen in. Re-arming while one is pending just moves it out,
            # which is right: the newest screen change is the one that
            # decides what is behind you.
            self._trim_at = self._gen + self.PROTECT_GENERATIONS + 1

    def _touch(self, key):
        """Move ``key`` to the LRU tail and stamp it with the current
        generation. Callers hold ``_lock``."""
        self._cache.move_to_end(key)
        entry = self._cache.get(key)
        if entry is not None:
            entry["gen"] = self._gen

    def _protected(self, entry):
        """Whether mpv may still have this bitmap bound. Callers hold
        ``_lock``.

        Anything touched during the current build, the scene on screen, or
        the one it replaced. Two generations rather than one because a push
        is not the moment the renderer acts on it -- the scene message is
        processed on mpv's own loop, and the renderer re-issues overlays
        without a new push besides (a scroll, a slot renumber). One spare
        generation covers both lags.
        """
        return entry.get("gen", 0) >= self._gen - self.PROTECT_GENERATIONS

    def _evict(self):
        while len(self._cache) > self.MAX_ENTRIES:
            key = next(iter(self._cache))
            if self._marked and self._protected(self._cache[key]):
                # Every entry behind the head is protected too (the LRU is
                # ordered by last touch and the protected span is the most
                # recent generations), so there is nothing left to take.
                # Over the count with nothing droppable is a dense screen:
                # draw over it rather than free what is being composited.
                break
            old = self._cache.pop(key)
            self._bytes -= self._entry_bytes(old)
            self._free(old["src"])
        if not self._marked:
            # Nobody is telling us when a scene reaches the renderer, so
            # nothing can say what is on screen. Leave the byte bound off
            # and keep the count bound above, which is where this started.
            return
        self._trim_to(self.MAX_BYTES)

    def _trim_to(self, target):
        """Free from the LRU head until ``target`` bytes, stopping dead at
        the first entry the live scene is using. Callers hold ``_lock``.

        This is the refcount, such as it is: mpv holds overlay bindings to
        the bitmaps in the scene it was last pushed, and freeing one of
        those is a read of freed memory on the libmpv path rather than a
        missing picture. Approximated by generation -- see _protected and
        on_scene_pushed.
        """
        while self._bytes > target and self._cache:
            key = next(iter(self._cache))
            if self._protected(self._cache[key]):
                # The head of the LRU is on screen, so everything behind it
                # is too. Over budget with nothing droppable is a big window
                # full of big rows: keep them and draw, rather than free a
                # bitmap mpv is compositing.
                break
            old = self._cache.pop(key)
            self._bytes -= self._entry_bytes(old)
            self._free(old["src"])
