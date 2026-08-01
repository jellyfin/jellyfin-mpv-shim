"""``TileRenderer`` — artwork plumbing and tile/row/grid construction.

Step 6b of ``docs/ARCHITECTURE_TARGET.md`` §3.1, the larger half. These are
the helpers that made page conversion impossible: measuring the five
remaining ``ViewsMixin`` renderers against ``PageContext`` gave 50 uses of
the ``ctx.shell`` escape hatch against a budget of 9, and this class is what
most of them were reaching for.

They are components by §1.4's test — they need render resources and
callbacks, never ``nav``, ``source`` or ``route`` — they were simply not
*pure*, so step 1 could not take them. What they need instead is:

``art``
    Render resources: the strip store, the thumbnail store, and the three
    tile geometries. Held live rather than snapshotted, because the browser
    swaps its stores when mpv is re-created.
``scroll``
    A :class:`~.scroll_state.ScrollState`. Virtualized rows are windowed
    against it, which is why the two extractions had to happen together —
    doing them apart means threading a callback between two half-built
    objects.

Plus three callbacks for things only the shell can answer: what a tile click
should do, whether keyboard navigation is engaged (carousel arrows hide
under the mouse), and the live app handle (page-scroll buttons drive the
renderer directly).

The downloaded-id sets live here rather than on the browser: the badge is
the only thing that reads them, and ``set_downloaded`` is called from the
shell's catalog refresh.

What deliberately did NOT move is the tile context menu. It needs ``route``,
``navigate``, ``run_async`` and the gateway — it is page work, not
rendering, and pulling it in would have made this class the god object with
a new name.
"""

import logging
import time

from ..i18n import _
from ..mpvtk.scaling import px, raster
from ..mpvtk.widgets import (
    Box,
    Button,
    Column,
    HScroll,
    Icon,
    Image,
    ImageMap,
    Row,
    Spacer,
    Stack,
    Table,
    Text,
    virtual_window,
)
from . import components, theme
from .components import chrome
from .strips import Tile
from .thumbnails import make_key

log = logging.getLogger("mpvtk_browser.tile_renderer")

#: Gap between grid cells.
GRID_GAP = 12
#: Width of a carousel's page arrow.
ARROW_W = 38
#: Height of one row in a track table.
TRACK_ROW_H = 34

#: Padding around a focus ring, so it is not clipped by its container.
RING_PAD = 5

#: Inset for the round page arrow (see _arrow_bitmap). Wider than RING_PAD so
#: the circle sits clear of the window edge on a bleed row.
ARROW_INSET = 8

#: Fallback arrow fill, for the (test-only) case of drawing before a theme has
#: been applied. Themes carry their own; see themes.DEFAULT["arrow_bg"].
ARROW_BG = "202020"
ARROW_ALPHA = 180

#: Opacity multiplier for a page button that has run out of row to page.
ARROW_DISABLED_ALPHA = 0.35

#: How long a page button takes to ease the row to its new offset (ms).
#: Long enough to read as movement and show which way the row went. It is
#: deliberately longer than the renderer's hold-repeat interval: a held button
#: re-aims mid-slide, and because the renderer reports the *destination* of a
#: slide rather than its current position, each repeat pages a clean whole
#: page and the row simply keeps gliding.
PAGE_SLIDE_MS = 180

#: Slack when comparing an offset against an end stop. The renderer clamps to
#: its own physical max and hands the value back through a logical round-trip,
#: so "at the end" is never exactly equal.
_EDGE_EPS = 1.0


#: Canonical aspect ratios a measured median is rounded onto, with the
#: tolerance jellyfin-web uses for each (``imageLoader.js:209-233``). Order
#: matters -- the first match wins, and the bands overlap.
#:
#: This is not cosmetic. The buckets are decided *after* snapping, so a
#: library whose median lands at 1.19 -- inside 4:3's band and outside
#: square's -- is landscape in web and would be square without this step.
#: Real libraries sit near these numbers rather than on them, which is the
#: whole reason web rounds.
_SNAP_RATIOS = (
    (2 / 3, 0.15),      # poster
    (16 / 9, 0.2),      # still / backdrop
    (1.0, 0.15),        # square
    (4 / 3, 0.15),      # 4:3 video
)


def _snap_ratio(value):
    """Round a measured median onto the nearest canonical ratio, or leave it
    alone. jellyfin-web's ``getPrimaryImageAspectRatio`` tail."""
    for target, tolerance in _SNAP_RATIOS:
        if abs(target - value) <= tolerance:
            return target
    return value


def page_geometry(view, count, geom):
    """``(pitch, per_page, max_offset)`` for a carousel of ``count`` tiles of
    ``geom`` laid out in a ``view``-wide viewport. All logical px.

    ``pitch`` is tile-to-tile distance. ``per_page`` is how many tiles are
    *fully* visible at an item-aligned offset — the paging step. ``max_offset``
    is the offset at which the last tile sits flush against the right edge.

    The offset convention is the one :meth:`TileRenderer.hscroll_row` sets up:
    the strip is inset by ``RING_PAD`` so a hover ring has room, so an offset
    of ``k * pitch`` puts tile ``k`` at the left edge *with* that room. Aligning
    to ``RING_PAD + k * pitch`` instead would shave the ring off.
    """
    pitch = geom.tile_w + geom.gap
    # Tile k is fully visible at offset j*pitch while
    # RING_PAD + (k - j) * pitch + tile_w <= view.
    per = max(1, int((view - RING_PAD - geom.tile_w) // pitch) + 1)
    total = 2 * RING_PAD + count * pitch - geom.gap
    return pitch, per, max(0.0, total - view)


def page_target(offset, direction, view, count, geom):
    """Where a page button should scroll to, or ``None`` if the row is already
    against that end (which is also what disables the button).

    This is jellyfin-web's ``scrollerItemSlideIntoView`` behaviour: a page
    advances by whole tiles, and a tile left half-cut at the trailing edge
    becomes the leading tile of the next page rather than being skipped. Ported
    by behaviour, not line by line — jf-web's index arithmetic assumes a bare
    grid with no gap and no leading pad, and carries a compensating ``-1`` that
    does not survive the translation.
    """
    pitch, per, max_offset = page_geometry(view, count, geom)
    if direction < 0 and offset <= _EDGE_EPS:
        return None
    if direction > 0 and offset >= max_offset - _EDGE_EPS:
        return None
    # The leading tile: the leftmost one at least partially in view. Paging
    # forward from it by a full page therefore lands on the first tile that
    # was NOT fully visible — the half-cut one at the trailing edge.
    leading = int((offset + _EDGE_EPS) // pitch)
    target = (leading + direction * per) * pitch
    return min(max(0.0, float(target)), max_offset)


#: Diameter of the hover play chip, as a fraction of the tile's SHORT side,
#: and the bounds it is clamped to. Proportional because the tiles it lands
#: on run from a 90px banner strip to a 225px poster, and a fixed size is
#: either lost on one or covering the artwork on the other.
CHIP_FRACTION = 0.34
CHIP_MIN = 28
CHIP_MAX = 56

#: How much the chip grows when the pointer is on the chip ITSELF rather than
#: just on the tile. The tile-level state says "there is something to press
#: here"; this one says "you are on it", which is the answer every other
#: control in the app gives -- a HUD button takes an accent wash on hover and
#: this is the same gesture, baked into a bitmap because it has to composite
#: over artwork (see _play_chip_bitmap).
CHIP_HOT_SCALE = 1.18

#: Opacity of the accent wash behind the lit chip, 0-255. The number the HUD
#: buttons use (widgets.Button's flat mode -> renderer draw_rect a=70): a
#: translucent accent disc under a solid accent glyph. Copied rather than
#: approximated so the two controls answer the pointer identically.
CHIP_HOT_ALPHA = 70


def _play_chip_bitmap(size, hot=False):
    """The round play button that appears on a hovered tile, at physical size.

    A BITMAP for the same reason the carousel arrows are one: mpv composites
    overlay bitmaps above all script ASS, so anything drawn as a node would
    be *under* the poster strip it is meant to sit on (GUIDE §6). Same
    supersample-and-shrink, which is what makes the circle read as round
    rather than as a notch cut out of the artwork.

    ``hot`` is the pointer being on the chip: the HUD button's hover, which
    is a translucent accent wash under a solid accent glyph. The renderer
    cannot do this for us -- it restyles nodes, and a bitmap has no style to
    change -- so the state is baked into a second bitmap and cached under its
    own key.
    """
    from PIL import Image as PILImage, ImageDraw

    active = theme.active() or {}
    w = px(size)
    ss = 3
    big_w = w * ss
    big = PILImage.new("RGBA", (big_w, big_w), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    fill = (theme.rgb(theme.ACCENT, CHIP_HOT_ALPHA) if hot
            else theme.rgb(active.get("arrow_bg", ARROW_BG),
                           int(active.get("arrow_alpha", ARROW_ALPHA))))
    d.ellipse([0, 0, big_w - 1, big_w - 1], fill=fill)
    # A filled triangle, nudged right of centre: a play glyph centred on its
    # bounding box reads as sitting left of centre in a circle.
    c = big_w / 2.0
    r = big_w * 0.22
    d.polygon([(c - r * 0.75 + r * 0.25, c - r),
               (c - r * 0.75 + r * 0.25, c + r),
               (c + r * 0.95, c)],
              # Solid accent on the wash, which is the HUD's icon tint --
              # the glyph is what has to read, and the disc behind it is
              # only there to say the pointer is on a control.
              fill=theme.rgb(theme.ACCENT if hot else theme.TEXT_FG, 255))
    lanczos = PILImage.LANCZOS  # type: ignore[attr-defined]
    return big.resize((w, w), lanczos)


def _arrow_bitmap(direction, disabled=False):
    """A round, translucent carousel page button as a PIL bitmap at physical
    size, for the ``overlay`` arrow mode.

    Drawn as a BITMAP (not an ASS box) so it composites above the poster strip
    and alpha-blends — its transparent corners show the artwork underneath,
    with no occlusion rect whose corners would reveal the dark window
    background. Supersampled and downscaled: the circle's antialiased edge is
    what sells it as round rather than as a notch cut out of the strip.

    ``disabled`` fades the whole button rather than dropping it, so the row's
    controls do not jump around as it reaches an end. It has to be baked in:
    the renderer has no disabled state, and could not restyle a bitmap anyway.
    """
    from PIL import Image as PILImage, ImageDraw

    active = theme.active() or {}
    fade = ARROW_DISABLED_ALPHA if disabled else 1.0
    w = px(ARROW_W)
    ss = 3  # supersample then downscale for smooth, anti-aliased edges
    big_w = w * ss
    big = PILImage.new("RGBA", (big_w, big_w), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.ellipse([0, 0, big_w - 1, big_w - 1],
              fill=theme.rgb(active.get("arrow_bg", ARROW_BG),
                             int(active.get("arrow_alpha", ARROW_ALPHA)
                                 * fade)))
    s = big_w * 0.20
    c = big_w / 2.0
    lw = max(2, int(round(big_w * 0.09)))
    base = c + s * 0.5 if direction < 0 else c - s * 0.5
    tip = c - s * 0.5 if direction < 0 else c + s * 0.5
    d.line([(base, c - s), (tip, c), (base, c + s)],
           fill=theme.rgb(theme.TEXT_FG, int(255 * fade)), width=lw,
           joint="curve")
    lanczos = PILImage.LANCZOS  # type: ignore[attr-defined]
    return big.resize((w, w), lanczos)


class TileRenderer:
    """Builds tiles, rows and grids. Owns the decoded-poster cache."""

    # A thumbnail fetch that fails transiently is retried on a later repaint,
    # but not immediately: a server that is down or slow would otherwise get a
    # fresh burst on every scroll frame. Attempts are capped so a permanently
    # broken URL settles instead of retrying for the life of the session.
    IMG_RETRY_BACKOFF = 5.0    # seconds, doubled per attempt
    IMG_MAX_ATTEMPTS = 4

    # A banner is a wide crop, not a 16:9 frame — two-thirds the height of
    # the equivalent 16:9 box, which is roughly 2.4:1.
    BANNER_RATIO = 9 / 16 * 2 / 3

    def __init__(self, art, scroll,
                 on_open=None, nav_mode=None, get_app=None,
                 on_play=None, can_play=None):
        #: Live resource provider. Read through rather than snapshotted:
        #: `source` and `server` are swapped by set_source at arbitrary
        #: moments (a reconnect, a user switch) and the stores are replaced
        #: when mpv is re-created, so a captured copy goes stale.
        self.art = art
        self.scroll = scroll
        #: What a tile click does. Takes the item dict.
        self.on_open = on_open or (lambda _item: None)
        #: True while keyboard/remote navigation is driving, which hides the
        #: carousel's mouse-only arrows.
        self.nav_mode = nav_mode or (lambda: False)
        #: The live MpvtkApp, for the page-scroll buttons.
        self.get_app = get_app or (lambda: None)
        #: What the hover play chip does, and which items get one. Separate
        #: from ``on_open``: a tile click opens the page, the chip starts
        #: playback -- and for a container those are genuinely different
        #: (see MpvtkBrowser._play_tile).
        self.on_play = on_play
        self.can_play = can_play or (lambda _item: False)
        #: Which tile the pointer is on, and which NODE it is actually on --
        #: the tile or the chip drawn inside it. The second is what a leave
        #: event is matched against; the first is what gets a chip. Set from
        #: the renderer's enter/leave events (see set_hover).
        self._hover = None
        self._hover_node = None

        # Decoded posters and the dedup/backoff bookkeeping around fetching
        # them. Private: nothing outside tile rendering reads these.
        self._posters = {}
        self._requested = set()
        self._img_retry = {}

        # Downloaded ids behind the tile badge. Replaced wholesale by
        # set_downloaded rather than mutated, so a reader never sees a
        # half-updated set.
        self._downloaded = set()
        self._downloaded_series = set()
        self._downloaded_seasons = set()
        self._downloaded_playlists = set()

    #: Suffix marking the chip's own node. The chip sits INSIDE the tile it
    #: belongs to, so the pointer moving onto it is the pointer leaving the
    #: tile as far as the renderer is concerned; both ids resolve back to the
    #: same tile here, or the chip would take itself away on contact.
    CHIP_SUFFIX = "-play"

    def set_hover(self, node_id):
        """Pointer entered a tile, or the chip drawn on one. Repaints if the
        TILE changed -- moving from a tile onto its own chip is still the
        same tile, and repainting there would take the chip away under the
        pointer that had just reached it. The chip's own appearance does
        change on arrival -- it lights up, like every other control -- so the
        repaint happens for either."""
        was_node, was_tile = self._hover_node, self._hover
        self._hover_node = node_id
        self._hover = self._tile_of(node_id)
        if (was_node, was_tile) == (self._hover_node, self._hover):
            return
        invalidate = getattr(self.art, "invalidate", None)
        if invalidate is not None:
            invalidate()

    def end_hover(self, node_id):
        """Pointer left ``node_id``. Ignored unless that is still where the
        pointer was last known to be: the renderer reports the arrival before
        the departure (see update_slider_hover), so a leave for something
        already moved on from describes a state that is over -- and taking it
        at face value would blank the chip that had just been asked for."""
        if node_id == self._hover_node:
            self.set_hover(None)

    def _tile_of(self, node_id):
        """The tile a hovered node belongs to. The chip is its own node
        inside the tile it is drawn on, so both answer the same tile."""
        if node_id and node_id.endswith(self.CHIP_SUFFIX):
            return node_id[:-len(self.CHIP_SUFFIX)]
        return node_id

    def set_downloaded(self, items, series, seasons, playlists):
        """Install fresh downloaded-id sets (from the sync catalog)."""
        self._downloaded = items
        self._downloaded_series = series
        self._downloaded_seasons = seasons
        self._downloaded_playlists = playlists

    def body_w(self, w):
        return chrome.body_width(w, chrome.CONTENT_PAD)

    #: Item types whose artwork is square, not a 2:3 poster: music, and
    #: playlists (whose own Primary image is a square). Rendering them in a
    #: poster frame pillarboxes the art.
    SQUARE_TYPES = {"Playlist", "MusicAlbum", "MusicArtist", "Audio",
                    "MusicGenre"}

    #: Aspect-ratio thresholds jellyfin-web's ``cardBuilder.setCardData``
    #: uses to pick a row's shape. Its ``>= 3`` banner case is folded into
    #: landscape: a banner is a shape this browser does not have, and a 3:1
    #: image in a 16:9 frame is a far smaller lie than one in a 2:3 frame.
    LANDSCAPE_RATIO = 1.33
    SQUARE_RATIO = 0.8

    def auto_geom(self, items, default=None, default_type="Primary"):
        """``(geom, image_type)`` for a row, chosen the way jellyfin-web does.

        Its card builder resolves a shape from the **median**
        ``PrimaryImageAspectRatio`` across the row and applies it to every
        card — so a Live TV row whose programmes carry 2:3 posters renders
        as posters, and one carrying 16:9 stills renders as landscape. That
        is why the same screen shows both, and it is a *per row* decision,
        which is exactly what a strip can reproduce (one strip is composited
        at one tile size).

        The median, not the mean: one oddly-shaped entry in a row of twenty
        must not reshape the row.

        ``image_type`` follows the same rule jellyfin-web's ``preferThumb:
        'auto'`` does — thumbs only where the row came out landscape.
        Items with no ratio at all fall back to ``default``.
        """
        ratios = sorted(r for r in (i.get("PrimaryImageAspectRatio")
                                    for i in items or ())
                        if isinstance(r, (int, float)) and r > 0)
        if not ratios:
            return (default or self.art.geom), default_type
        middle = len(ratios) // 2
        median = (ratios[middle] if len(ratios) % 2
                  else (ratios[middle - 1] + ratios[middle]) / 2.0)
        median = _snap_ratio(median)
        if median >= self.LANDSCAPE_RATIO:
            return self.art.geom_wide, "Thumb"
        if median > self.SQUARE_RATIO:
            return self.art.geom_square, "Primary"
        return self.art.geom, "Primary"

    def square_geom(self, items):
        """``geom_square`` when every item's art is square, else None.

        A strip is composited at one tile size, so this is a per-grid
        decision, not per-tile — hence "every item".

        Was ``ViewsMixin._square_geom``; it is a question about artwork
        geometry, and the geometries live here.
        """
        types = {i.get("Type") for i in items or ()}
        if types and types <= self.SQUARE_TYPES:
            return self.art.geom_square
        return None

    def _request_image(self, key, url, box):
        """Return a cached decoded PIL image for ``key`` (poster/backdrop/…),
        or None while it loads — requesting it once from the thumbnail pool.
        The next repaint (woken by the pool's notify) picks it up."""
        img = self._posters.get(key)
        if img is not None or self.art.thumbs is None or not url:
            return img
        if key in self._requested:
            return None
        retry_at = self._img_retry.get(key)
        if retry_at is not None and time.time() < retry_at[1]:
            return None            # cooling off after a failed attempt
        self._requested.add(key)
        self.art.thumbs.request(key, url, box,
                            lambda im, k=key: self._image_done(k, im))
        return None
    def _image_done(self, key, image):
        """Thumbnail delivery, on the loop thread.

        ``image`` is None when the fetch failed. Releasing the dedup marker
        is the whole point: it used to be set before dispatch and never
        cleared, so one timed-out poster stayed blank for the rest of the
        process — no navigation, scroll or re-open would ask again. A
        permanent miss (the server says there's no such image) keeps the
        marker, so it isn't asked for again either."""
        if image is not None:
            self._posters[key] = image
            self._img_retry.pop(key, None)
            return
        if self.art.thumbs is not None and self.art.thumbs.is_gone(key):
            return                 # no such image; stop asking
        attempts = self._img_retry.get(key, (0, 0.0))[0] + 1
        if attempts > self.IMG_MAX_ATTEMPTS:
            return                 # give up, keeping the marker set
        self._img_retry[key] = (
            attempts,
            time.time() + self.IMG_RETRY_BACKOFF * (2 ** (attempts - 1)))
        self._requested.discard(key)
    #: Artwork that must be shown WHOLE, by the type it resolved to and the
    #: type that was asked for. Everything else is cover-cropped: a poster, a
    #: still and a backdrop are all photographs of a frame, and a crop takes
    #: scenery off the edges. A wordmark loses the name.
    #:
    #: A Logo never covers -- it is a wordmark on transparency and there is no
    #: frame it was cut for. A Banner covers only its own strip: standing in
    #: for a missing logo on a 16:9 card, cropping it to that card would trim
    #: exactly the title it was borrowed for.
    @staticmethod
    def _contains(resolved, requested):
        return resolved == "Logo" or (resolved == "Banner"
                                      and requested != "Banner")

    def poster_for(self, item, geom, image_type="Primary", inherit=True):
        """Return (PIL image or None, cache tag, contain). Requests the
        poster once if absent; the strip recomposites when it arrives (tag
        changes). ``contain`` says the artwork must be drawn whole rather
        than filling the tile -- see :meth:`_contains`.

        ``inherit`` is passed straight to ``image_spec`` — see there. It is
        not part of the cache key because it does not need to be: the key is
        built from the spec the chain *resolved to*, so two rows asking about
        the same item with different inherit settings land on different keys
        whenever they resolve to different images, and share one when they
        do not."""
        # Art is fetched at physical size: the renderer crops rather than
        # resamples, so a 1x poster under a 2x UI would render as a corner
        # fragment. Jellyfin resizes server-side, so this costs nothing but
        # bandwidth.
        w, h = raster(geom.tile_w, geom.tile_h)
        if "_image_url" in item:
            # A pseudo-item (a chapter) carrying its own spec+url: chapter
            # art is indexed, so it isn't addressable through image_spec.
            spec, url = item.get("_image_spec"), item.get("_image_url")
            if not spec or not url:
                return None, "", False
            key = make_key(spec[0], spec[1], spec[2], w, h)
            return self._request_image(key, url, (w, h)), key, False
        spec = self.art.source.image_spec(item, image_type, w, inherit=inherit)
        if not spec or self.art.server is None:
            return None, "", False
        item_id, itype, itag = spec
        contain = self._contains(itype, image_type)
        # In the key as well as in the request: fill and fit are two different
        # pictures of one source at one size, and the store caches them to
        # disk.
        key = make_key(item_id, itype, itag, w, h,
                       fit="fit" if contain else "")
        url = self.art.source.image_url(self.art.server, item_id, itype, itag,
                                        w, h, fill=not contain)
        return self._request_image(key, url, (w, h)), key, contain
    def art_cell(self, item, size=28):
        """Small square album-art bitmap for a table cell (track lists);
        a placeholder box while it loads or when the item has none.
        Each cell is its own overlay, so only use in virtualized or
        short tables (the 63-overlay budget is shared)."""
        ps = px(size)
        spec = self.art.source.image_spec(item, "Primary", ps)
        if spec and self.art.server is not None:
            item_id, itype, itag = spec
            key = make_key(item_id, itype, itag, ps, ps)
            url = self.art.source.image_url(self.art.server, item_id, itype,
                                        itag, ps, ps, fill=True)
            img = self._request_image(key, url, (ps, ps))
            if img is not None:
                img = self._plated(img)
                # No lsize: this is decoded artwork, not a canvas we sized.
                # The server preserves aspect, so a "square" request comes
                # back e.g. 56x52 and the logical footprint is whatever the
                # bitmap actually is. What must be scaled here is the
                # *request* above, not the result.
                b = self.art.strips.bitmap(key, img)
                return Image(b["src"], b["iw"], b["ih"], v=b.get("v", 0),
                             w=b["lw"], h=b["lh"])
        return self._art_placeholder(size)
    @staticmethod
    def _plated(img):
        """``img``, given the light backing transparent artwork is drawn for.

        An art cell is its own overlay with nothing drawn behind it, so a
        transparent logo composites straight onto ``WINDOW_BG`` — which is
        where the guide's channel column draws every logo it has. A tile gets
        the same treatment by recolouring its card (see
        ``StripStore._paint_poster``); this is the no-card version of it.
        """
        from ..imageutil import flatten_onto, plate_for, with_shadow

        plate = plate_for(img)
        if plate is None:
            return img
        if plate.shadow:
            img = with_shadow(img)
        # Same corner radius _art_placeholder uses, so a plated logo and the
        # box standing in for a missing one are the same shape.
        return flatten_onto(img, plate.color, radius=px(4))

    @staticmethod
    def _art_placeholder(size=28):
        """Same-sized stand-in for an art cell — while it loads, when the
        item has none, and for rows outside the virtual window (which must
        not composite: see _track_list)."""
        return Box(w=size, h=size, bg=theme.PLACEHOLDER_BG, radius=4)
    def is_downloaded(self, item):
        iid, t = item.get("Id"), item.get("Type")
        if iid in self._downloaded:
            return True
        if t == "Series":
            return iid in self._downloaded_series
        # Neither a season nor a playlist is ever itself a downloads row: a
        # season is expanded into its episodes, and a playlist lives in its
        # own table. Without these two a downloaded season showed "Download"
        # forever, never got the tile badge, and the Season branch of
        # _remove_download was unreachable — the same shape as the playlist
        # bug this line was added for.
        if t == "Season":
            return iid in self._downloaded_seasons
        return t == "Playlist" and iid in self._downloaded_playlists
    #: Banner widths are rounded up to a multiple of this before they reach
    #: the artwork cache.
    #:
    #: The cache keys on exact pixel dimensions, and the banner used to be a
    #: *continuous* function of the window width -- so dragging a window edge
    #: across 400px asked the server for up to 400 different backdrops, decoded
    #: 400 bitmaps and kept them all resident until the LRU pushed them out.
    #: That is issue #592, and it is why a resize both hammered the access log
    #: and ballooned memory.
    #:
    #: jellyfin-web does the same thing for the same stated reason -- it rounds
    #: the screen width down to a multiple of 100 "to improve cache hits"
    #: (cardBuilder.js:126-129). 128 rather than 100 because these are pixels
    #: in a cache key, not a CSS breakpoint: it makes at most nine distinct
    #: banner widths between a small window and the 1100 cap.
    #:
    #: Rounding UP, not down, so the bitmap is never asked to upscale -- a
    #: banner drawn slightly wider than requested is cropped by the compositor,
    #: which is invisible; one drawn narrower is soft.
    BANNER_STEP = 128

    def banner_box(self, width):
        """The banner's LAID-OUT size: exactly the space it has.

        Deliberately not quantised. The header spans the content width, so
        rounding this up overhangs the scrollbar and rounding it down leaves
        a gap beside content that does reach the edge. What gets quantised is
        the *fetch* -- see ``_banner_fetch_w``.
        """
        bw = min(width - 2 * chrome.CONTENT_PAD, 1100)
        return bw, int(bw * self.BANNER_RATIO)

    def _banner_fetch_w(self, physical_w):
        """Physical width to ask the server for, given the drawn width.

        Rounded UP to a step so a resize stops minting a new request per
        pixel (issue #592) -- the artwork cache keys on exact dimensions, so
        a continuous width meant a continuous stream of misses. Up rather
        than down because the fetched image is *cropped* into the banner by
        compose_banner: larger than needed costs nothing, smaller would have
        to be upscaled.

        Only the fetch. The composed banner is still built at the exact drawn
        size, because that one has to match the box it is drawn in.
        """
        step = self.BANNER_STEP
        if physical_w <= 0:
            return 0
        return int(-(-physical_w // step) * step)
    def backdrop_node(self, item, box, node_id, title=None, meta=None,
                       context=None):
        """A backdrop banner for detail/series headers.

        With ``title`` the heading is *baked into the bitmap* over a bottom
        gradient, like the Tk browser did — text drawn as ASS would sit
        under the image (bitmaps composite above all script ASS), and the
        occlude punch would show the window background rather than the
        artwork. Returns a placeholder Box while the art loads or if the
        item has none, in which case the caller still draws its own heading."""
        spec = None
        if self.art.server is not None:
            spec = self.art.source.backdrop_spec(item)
        if spec:
            owner_id, tag = spec
            # box is logical (it came off the surface width); the bitmap and
            # everything baked into it are physical.
            pbox = raster(*box)
            key = make_key(owner_id, "Backdrop", tag, pbox[0], pbox[1])
            if title:
                key += "|" + make_key(title, meta or "", context or "",
                                      pbox[0], pbox[1])
            # Request at the BANNER's aspect, at a quantised width so that
            # dragging the window edge does not ask for a new image every
            # pixel.
            #
            # The height is not incidental. `fill=True` is fillWidth +
            # fillHeight, i.e. the server *crops* to the shape asked for
            # (it does not squash), and compose_banner's scale_to_cover
            # then centre-crops to the same shape — so asking for the
            # banner's own aspect is the identical picture for a third of
            # the pixels. Asking for a square instead, as this briefly
            # did, hands back the centre square of a 16:9 backdrop, which
            # cover then blows up to the full width: every header zoomed
            # ~1.8x, in the commit whose point was making banners cheaper.
            fetch_w = self._banner_fetch_w(pbox[0])
            fetch_h = max(1, int(fetch_w * self.BANNER_RATIO))
            fetch_key = make_key(owner_id, "Backdrop", tag, fetch_w, fetch_h)
            url = self.art.source.backdrop_url(self.art.server, item,
                                               width=fetch_w, height=fetch_h,
                                               fill=True)
            img = self._request_image(fetch_key, url, (fetch_w, fetch_h))
            if img is not None:
                b = self.art.strips.bitmap(key, components.compose_banner(
                    img, pbox, title, meta, context), lsize=box)
                return Image(b["src"], b["iw"], b["ih"], id=node_id,
                             v=b.get("v", 0), w=b["lw"], h=b["lh"])
        return Box(w=box[0], h=box[1], bg=theme.PLACEHOLDER_BG, radius=6,
                   id=node_id)
    def _tile(self, item, geom, image_type="Primary", parent_item=False,
              inherit=True, labels=None):
        """One tile. ``parent_item`` draws an Episode as its *series* —
        the show's name and the show's poster instead of the episode's name
        over the episode still — which is what a Latest-TV row is a list of.
        It changes which image is fetched, not the tile's shape: the caller's
        ``geom`` still decides that."""
        ud = item.get("UserData") or {}
        pos = ud.get("PlaybackPositionTicks") or 0
        rt = item.get("RunTimeTicks") or 0
        from . import live_tv

        progress = (pos / rt) if (pos and rt) else 0.0
        recording = live_tv.is_recording_now(item)
        record = live_tv.timer_state(item) or ""
        if item.get("_recording") and not record:
            # A recording DTO carries no timer state; the query it came from
            # is what knows (see LibrarySource.get_recordings), and it still
            # deserves the symbol.
            record = "recording"
        if item.get("Type") == "Program" or recording:
            # How far through the broadcast is, not how far through *you*
            # are — there is no resume point for something airing live. The
            # same bar jellyfin-web draws on an On Now card, and the one
            # thing that says whether you have missed most of it. A finished
            # recording is an ordinary item again and keeps its resume bar,
            # because neither branch applies to it.
            progress = live_tv.program_progress(item) or progress
        if parent_item and item.get("Type") == "Episode":
            # The series' poster, via SeriesId/ParentBackdropItemId — see
            # LibrarySource.image_spec. Poster art, so it fits the poster
            # tile the row is already drawing.
            image_type = "ParentPrimary"
        poster, tag, contain = self.poster_for(item, geom, image_type,
                                               inherit=inherit)
        # labels: (show_title, show_year) from the library's view settings.
        # None means "as always", which is both on.
        show_title, show_year = labels or (True, True)
        title, subtitle = components.tile_lines(item, parent_item,
                                                show_year=show_year)
        if not show_title:
            title = ""
            if not show_year:
                # Both off means no caption at all, and the grid reserves no
                # room for one -- so the second line has to go even where
                # show_year does not govern it (an episode's "Show · S1E2",
                # a listing's channel and time). Otherwise it draws into the
                # row below.
                subtitle = ""
        return Tile(
            key=item.get("Id", ""),
            title=title,
            subtitle=subtitle,
            poster=poster,
            poster_tag=tag,
            glyph=components.placeholder_glyph(item),
            watched=components.is_watched(item),
            badge=int(ud.get("UnplayedItemCount") or 0),
            progress=progress,
            downloaded=self.is_downloaded(item),
            kind=components.type_indicator_icon(item),
            recording=recording,
            record=record,
            contain=contain,
        )
    def tile_row(self, title, items, row_id, geom=None, image_type="Primary",
                  bleed=False, on_click=None, parent_item=False,
                  inherit=True, see_all=None, autofocus_first=False):
        """A titled horizontal carousel.

        ``bleed`` runs the strip edge-to-edge so overlay page arrows sit flush
        against the window's left and right sides; the title is indented to
        line up with the content instead. ``parent_item`` is passed through
        to the tiles (see ``_tile``)."""
        geom = geom or self.art.geom
        # Section-title size is theme-controlled (24 = the stock value), so a
        # theme with larger covers can size its headings to match.
        title_size = (theme.active() or {}).get("heading_size", 24)
        heading: object = Text(title, size=title_size, bold=True)
        if see_all is not None:
            # jellyfin-web's chevron: the heading becomes the link to the
            # full listing, because a row is a top-N of something and this
            # is the only route to the rest of it.
            #
            # A Box with on_click, not a bare Text, so the toolkit picks it
            # up: renderer.lua's nav_candidates collects any node carrying
            # click, which is what makes the heading a D-pad target with a
            # focus ring for free. We draw it in every layout -- web hides
            # it in its TV layout, but that is web having two layouts and
            # hiding the affordance in one, not a judgement that a remote
            # cannot use it.
            heading = Box(
                [Text(title, size=title_size, bold=True),
                 Icon("chevron_right", int(title_size * 0.9),
                      color=theme.TEXT_FG)],
                id="%s-more" % row_id, direction="row", align="center",
                gap=2, pad=(6, 2), radius=6,
                hover={"fill": theme.BUTTON_BG}, on_click=see_all)
        head = [heading]
        if bleed:
            # The strip runs edge to edge; indent the heading to line up with
            # the first tile instead.
            head.insert(0, Spacer(w=chrome.CONTENT_PAD))
        buttons = self.page_buttons(row_id, len(items), geom, bleed)
        if buttons is not None:
            # jellyfin-web's placement: the pair rides the section heading,
            # right-aligned. Nothing overlaps the artwork, which is the whole
            # point of this mode — no compositing question to answer, and the
            # buttons get hover and a disabled state for free.
            head += [Spacer(flex=1)] + buttons
            if bleed:
                # ...and pull them in off the window edge, mirroring the
                # indent the title got above.
                head.append(Spacer(w=chrome.CONTENT_PAD))
            # An explicit width, because a Column sizes each child to what it
            # *measures* unless told to stretch, and a Row of a title plus a
            # flex spacer measures to just the title — which parked the pair
            # immediately after the heading text instead of at the far edge.
            heading = Row(head, align="center", w=self.row_view_w(bleed))
        elif len(head) > 1:
            heading = Row(head)
        return Column(
            [
                heading,
                self.hscroll_row(
                    self.image_map(items, row_id, geom, image_type,
                                    on_click=on_click,
                                    parent_item=parent_item,
                                    inherit=inherit,
                                    autofocus_first=autofocus_first),
                    row_id, geom.strip_h + 2 * RING_PAD,
                    len(items), geom, bleed),
            ],
            gap=10,
        )
    def header_offset(self, header):
        """Exact content-y of the first tile row that follows ``header`` in a
        ``Column(pad=CONTENT_PAD, gap=GRID_GAP)``: the top pad, each header
        item's measured height, and one gap after each. This is the snap offset
        for a snapping grid — the virtualizer's approximate ``head_h`` would
        land a stop a few px short and leave the previous row's caption (its
        year label) peeking at the top edge. ``header`` may be empty (grid
        fills the scroll), and None entries are dropped to match Column."""
        from ..mpvtk.layout import measure

        hs = [h for h in header if h is not None]
        return (chrome.CONTENT_PAD
                + len(hs) * GRID_GAP
                + sum(measure(h)[1] for h in hs))
    def grid_of(self, items, prefix, size, geom=None,
                 image_type="Primary", scroll_id=None, head_h=0,
                 on_click=None, inherit=True, labels=None):
        """Tile rows for a vertical grid.

        With ``scroll_id`` the rows are **virtualized**: only those within a
        screen of the viewport are composited, the rest become fixed-height
        Spacers. Without it a long library blows past both the strip cache and
        mpv's 63-overlay budget, which showed up as tiles that came back blank
        after scrolling away and back."""
        geom = geom or self.art.geom
        cols = self.cols(size[0], geom)
        # (There was a `heading` parameter here that no caller ever passed.
        # The one page that wants a heading over its grid draws it itself,
        # outside the scroll, which is also what keeps head_h honest.)
        rows = []
        nrows = (len(items) + cols - 1) // cols
        first, last = 0, nrows - 1
        if scroll_id is not None:
            rh = geom.strip_h + GRID_GAP
            vh = max(240.0, float(size[1]))
            top = max(0.0, self.scroll.offset(scroll_id) - head_h)
            first = int(max(0.0, top - vh) // rh)
            last = int((top + 2 * vh) // rh)
        for r in range(nrows):
            if first <= r <= last:
                start = r * cols
                rows.append(self.image_map(items[start:start + cols],
                                            "%s-%d" % (prefix, start),
                                            geom, image_type,
                                            on_click=on_click,
                                            inherit=inherit,
                                            labels=labels,
                                            # Grid rows share one bounded blank
                                            # shape, so composite them off the
                                            # loop thread (see StripStore.strip).
                                            async_=scroll_id is not None))
            else:
                rows.append(Spacer(h=geom.strip_h))
        if not items:
            rows.append(Text(_("Nothing here yet."), size=18,
                             color=theme.SUBTLE_FG))
        return rows

    def image_map(self, items, prefix, geom=None, image_type="Primary",
                   on_click=None, async_=False, parent_item=False,
                   inherit=True, labels=None, autofocus_first=False):
        geom = geom or self.art.geom
        tiles = [self._tile(it, geom, image_type, parent_item, inherit,
                            labels)
                 for it in items]
        s = self.art.strips.strip(tiles, geom, async_=async_)
        regions = []
        act = on_click or self.on_open
        chip = None
        for i, (r, it) in enumerate(zip(s["regions"], items)):
            rid = "%s-%s" % (prefix, r["key"])
            playable = self.on_play is not None and self.can_play(it)
            regions.append(dict(
                r,
                id=rid,
                on_click=(lambda i=it: act(i)),
                on_context=(lambda x, y, i=it: self.on_context(i, x, y)),
                # Only tiles that would DO something with a hover ask to hear
                # about one: a row of people or genres is not worth a scene
                # rebuild per tile the pointer crosses.
                on_hover=((lambda _v=None, k=rid: self.set_hover(k))
                          if playable else None),
                on_hover_end=((lambda k=rid: self.end_hover(k))
                              if playable else None),
                # The page's default for a keyboard/remote arrival. Only
                # ever the first tile: it is "where this row starts", not a
                # judgement about the item.
                autofocus=(autofocus_first and i == 0),
            ))
            if playable and self._hover == rid:
                chip = self._play_chip(rid, it, r, geom)
        node = ImageMap(s["src"], s["iw"], s["ih"], regions=regions,
                        v=s.get("v", 0), w=s["lw"], h=s["lh"])
        if chip is None:
            return node
        # Inside the strip's own rect, so it travels with the row when a
        # carousel scrolls -- a Stack around the HScroll would leave the chip
        # parked in mid-air over whatever slid under it.
        return Stack([node, chip], w=s["lw"], h=s["lh"])

    def _play_chip(self, region_id, item, region, geom):
        """The round play button drawn over the hovered tile.

        Centred on the ARTWORK rather than the tile: the region includes the
        caption block underneath, and a chip centred on that sits visibly low
        on the picture.
        """
        cid = region_id + self.CHIP_SUFFIX
        # Grown around its own centre, so the pointer that triggered the
        # growth is still inside afterwards -- a chip that grew downwards
        # would slide out from under the pointer, un-hover itself, shrink,
        # and do it again.
        hot = self._hover_node == cid
        size = int(max(CHIP_MIN, min(CHIP_MAX,
                                     min(geom.tile_w, geom.tile_h)
                                     * CHIP_FRACTION))
                   * (CHIP_HOT_SCALE if hot else 1.0))
        # Lazily: the chip is re-derived on every build of the strip it sits
        # on, and there are only ever two of them per size. Handing the store
        # a finished 3x supersample to throw away is the whole cost of a
        # hover repaint.
        bm = self.art.strips.bitmap(
            ("play-chip", px(size), hot),
            lambda: _play_chip_bitmap(size, hot=hot),
            lsize=(size, size))
        dx = region["x"] + (geom.tile_w - size) / 2
        dy = region["y"] + (geom.tile_h - size) / 2
        return Image(bm["src"], bm["iw"], bm["ih"], v=bm.get("v", 0),
                     w=bm["lw"], h=bm["lh"],
                     id=cid,
                     anchor="nw", dx=dx, dy=dy,
                     on_click=lambda: self.on_play(item),
                     # The tile's own menu, because the chip covers the
                     # middle of the artwork and a hit test answers with one
                     # node: without this, right-clicking a poster does
                     # nothing whenever the pointer is where the chip put
                     # itself -- which is exactly where a pointer resting on
                     # a tile tends to be.
                     on_context=(lambda x, y, i=item:
                                 self.on_context(i, x, y)),
                     # It has to hear its own hover -- see CHIP_SUFFIX.
                     on_hover=lambda _v=None, k=cid: self.set_hover(k),
                     on_hover_end=lambda k=cid: self.end_hover(k))
    def row_view_w(self, bleed):
        """Laid-out width of a carousel's scroll viewport. Both the overflow
        test and the paging arithmetic measure against this."""
        avail = (self.art._size[0] if self.art._size else 1280)
        return avail if bleed else avail - 2 * chrome.CONTENT_PAD

    def _page_state(self, row_id, count, geom, bleed):
        """``(view, prev_target, next_target)`` for a carousel's page buttons,
        or ``None`` when it should not have any.

        A ``None`` target is an end stop, i.e. a disabled button. Computed once
        and shared by both arrow modes so the two cannot disagree about whether
        a row overflows.
        """
        view = self.row_view_w(bleed)
        content_w = count * geom.tile_w + max(0, count - 1) * geom.gap
        if content_w <= view or self.nav_mode():
            # keyboard/remote navigation auto-scrolls the row as focus moves,
            # so pointer paging buttons would be dead weight
            return None
        offset = self.scroll.offset(row_id)
        return (view,
                page_target(offset, -1, view, count, geom),
                page_target(offset, 1, view, count, geom))

    def page_buttons(self, row_id, count, geom, bleed):
        """The heading-row page buttons (``header`` arrow mode), or ``None``.

        jellyfin-web's design, and the default: the pair sits in the section
        heading rather than on the artwork, so it can be an ordinary pair of
        flat HUD-style buttons — hover works, and an exhausted direction dims
        instead of misleading. ``None`` for themes in ``overlay`` mode, whose
        buttons are drawn by :meth:`hscroll_row` instead.
        """
        if (theme.active() or {}).get("arrow_mode", "header") != "header":
            return None
        state = self._page_state(row_id, count, geom, bleed)
        if state is None:
            return None
        view, prev_to, next_to = state

        def button(icon, node_id, direction, target, label):
            # Disabled is a dimmed glyph and no hover wash, but the callback
            # stays bound: page_row re-reads the live offset and no-ops at an
            # end stop anyway, so an unbound button would only be *less*
            # correct — `target` is a build-time snapshot and can be up to
            # ScrollState.STEP behind by the time the click lands. Dropping
            # on_click would also drop the node entirely (Box emits one only
            # when it has a bg, a border or a click), taking the id with it.
            off = target is None
            return Button("", id=node_id, icon=icon, flat=True,
                          icon_size=22, tip=label,
                          fg=(theme.mix(theme.SUBTLE_FG, theme.WINDOW_BG,
                                        0.5) if off else theme.TEXT_FG),
                          hover=None if off else {"fill": theme.ACCENT,
                                                  "circle": True},
                          repeat=not off,
                          on_click=lambda: self.page_row(
                              row_id, direction, count, geom, bleed))

        return [button("chevron_left", row_id + "-pl", -1, prev_to,
                       _("Previous")),
                button("chevron_right", row_id + "-pr", 1, next_to,
                       _("Next"))]

    def hscroll_row(self, content, row_id, h, count, geom, bleed=False):
        """An HScroll, with ◀ ▶ page buttons floating over its edges in the
        ``overlay`` arrow mode.

        Those arrows genuinely overlay the poster strip: a Stack layers them on
        top, and each is a composited BITMAP, which is the only thing that can
        draw *over* a strip and alpha-blend with it (bitmaps composite above
        all script ASS — GUIDE §6). They hold-repeat while pressed, fade at an
        end stop, and are omitted when the row doesn't overflow.

        In the default ``header`` mode there is nothing over the strip at all —
        :meth:`page_buttons` puts the pair in the section heading instead.

        The strip is inset by RING_PAD so a tile's hover ring has room inside
        the viewport; the renderer clips it to the container, and without the
        inset its top edge was shaved off under the heading above."""
        state = self._page_state(row_id, count, geom, bleed)
        # Watch the container only when it has page buttons, and only for
        # end-stop crossings. Without a watch the renderer scrolls the row
        # entirely on its own and never tells Python, so the buttons' disabled
        # state was frozen at whatever the first build saw — back permanently
        # dim, forward permanently lit, however far the row had been paged.
        watch = None
        if state is not None:
            watch = (lambda off, mx, rid=row_id:
                     self.scroll.on_scroll(rid, off, mx, edges_only=True))
        scroll = HScroll(Box([content], pad=RING_PAD),
                         id=row_id, h=h, flex=1, on_scroll=watch)
        if state is None or (theme.active() or {}).get(
                "arrow_mode", "header") != "overlay":
            return Row([scroll], h=h)
        _view, prev_to, next_to = state

        def arrow(node_id, direction, anchor, target):
            # "w"/"e" centre on the whole strip, which includes the caption
            # block under the tile; shift up by half of it so the arrow sits
            # on the artwork.
            dy = -(geom.strip_h - geom.tile_h) / 2
            # A round arrow needs more clearance than a square one: its circle
            # reads as sitting ON the artwork, so leaving it at the ring
            # padding looked shoved against the window edge (a square button
            # squares off into the edge and does not).
            dx = ARROW_INSET if anchor == "w" else -ARROW_INSET
            off = target is None
            img = _arrow_bitmap(direction, disabled=off)
            bm = self.art.strips.bitmap(
                ("carousel-arrow", direction, px(ARROW_W), off), img,
                lsize=(ARROW_W, ARROW_W))
            # No hover glow here on purpose: a halo would have to be ASS,
            # and mpv composites overlay bitmaps ABOVE all script ASS
            # (GUIDE §6) — over a poster strip the glow simply disappears
            # under the artwork. It would have to be baked into the bitmap.
            # Bound in both states, as in page_buttons: page_row re-checks the
            # live offset, so a faded arrow clicked just as the row came off
            # its end stop still does the right thing.
            return Image(bm["src"], bm["iw"], bm["ih"],
                         v=bm.get("v", 0), w=bm["lw"], h=bm["lh"],
                         id=node_id, anchor=anchor, dx=dx, dy=dy,
                         repeat=not off,
                         on_click=lambda: self.page_row(row_id, direction,
                                                        count, geom, bleed))

        return Stack([
            scroll,
            arrow(row_id + "-pl", -1, "w", prev_to),
            arrow(row_id + "-pr", 1, "e", next_to),
        ], h=h)
    def page_row(self, row_id, direction, count, geom, bleed=False):
        """Page a carousel one screenful of whole tiles (jellyfin-web's
        ``scrollerItemSlideIntoView``; see :func:`page_target`).

        The offset is re-read from the renderer here rather than reused from
        build time: a scroll shorter than ``ScrollState.STEP`` never triggered
        a rebuild, so the built-in copy can be that far behind at the moment of
        a click — and paging from a stale offset lands on the wrong tile.
        """
        app = self.get_app()
        if app is None or not hasattr(app, "scroll_to"):
            return
        self.scroll.refresh(app)
        target = page_target(self.scroll.offset(row_id), direction,
                             self.row_view_w(bleed), count, geom)
        if target is None:
            return
        app.scroll_to(row_id, target, ms=PAGE_SLIDE_MS)
    def cols(self, w, geom):
        # _body_w, not w - 32: grids sit in the same padded scroll column,
        # so ignoring the scrollbar fits one tile too many at some widths
        # and the last one is clipped.
        return max(1, int(
            (self.body_w(w) + geom.gap) // (geom.tile_w + geom.gap)))
    def item_list(self, rows, prefix, on_click, scroll_id=None, head_h=0):
        """A library as a plain table -- jellyfin-web's List view type.

        Deliberately without an art column, unlike ``track_list``: the point
        of choosing this view is that the artwork was not helping, and it is
        also what makes it cheap — no strips, and no overlays to spend.

        **Virtualized all the same.** "No overlays" was read as "no need to
        virtualize", and it is only half the cost: a row is still a Row, two
        margin Spacers and three cell Boxes to build, lay out and serialize,
        on the loop thread, every repaint. Infinite scroll grows this list
        without bound and List view is the one people pick *because* the
        library is large — at 2000 rows that measured ~8k nodes, 85ms and
        1.4MB per push, repeated every 120px of scroll.
        """
        columns = [{"label": _("Name"), "flex": 4},
                   {"label": _("Year"), "w": 80},
                   {"label": _("Length"), "w": 110, "align": "right"}]
        table_rows = []
        for i, row in enumerate(rows):
            table_rows.append({
                "id": "%s-%d-%s" % (prefix, i,
                                    (row.get("item") or {}).get("Id", "")),
                "cells": row["cells"],
                "on_click": (lambda i=i: on_click(i)),
                "on_context": (lambda x, y, it=row.get("item"):
                               self.on_context(it, x, y)),
            })
        # Same window track_list computes, and for the same reason minus
        # the overlays. Only when the caller names its scroll container:
        # without one there is no live offset to window against.
        virtual = None
        if scroll_id is not None and self.art._size is not None:
            virtual = {
                "offset": max(0.0, self.scroll.offset(scroll_id) - head_h),
                "height": float(self.art._size[1]),
            }
        return Table(columns, table_rows, row_h=TRACK_ROW_H,
                     id=(scroll_id or prefix) + "-table", virtual=virtual)

    def track_list(self, tracks, prefix, on_play, playing_id=None,
                    selected=None, on_select=None, album=True,
                    art=False, scroll_id=None, head_h=0, menu=False):
        """Tabular track list (album, playlist, queue, search songs).

        Uses the toolkit's Table so header and cells come from one column
        spec — hand-laid Rows drifted out of alignment as soon as a cell's
        text width changed.

        With ``on_select`` the row click selects (mods-aware) and the first
        column becomes a play button, so a selectable list still has a
        one-click way to jump to a track. Without it, clicking the row plays.

        ``art=True`` adds a leading album-art thumbnail column — useful in
        mixed-album lists (playlists); redundant on an album page."""
        selected = selected or set()
        columns = []
        if art:
            columns.append({"label": "", "w": 32})
        columns += [{"label": "#", "w": 46, "align": "right"},
                    {"label": _("Title"), "flex": 3},
                    {"label": _("Artist"), "flex": 2}]
        if album:
            columns.append({"label": _("Album"), "flex": 2})
        columns.append({"label": _("Time"), "w": 70, "align": "right"})

        def first_cell(i, tr):
            if on_select is None:
                return str(tr.get("IndexNumber") or (i + 1))
            return Box([Icon("play_arrow", 16,
                             color=theme.ACCENT if tr.get("Id") == playing_id
                             else theme.SUBTLE_FG)],
                       id="%s-play-%d" % (prefix, i), w=40, h=26,
                       # justify centres along the row's main axis; align
                       # alone only centred it vertically, leaving the glyph
                       # packed against the left edge of the button.
                       direction="row", align="center", justify="center",
                       radius=4,
                       hover={"fill": theme.BUTTON_ACTIVE},
                       on_click=lambda i=i: on_play(i))

        # Virtualize against the live scroll offset. Not just a repaint
        # cost: with art=True each visible row is one mpv overlay, and a
        # few hundred tracks would blow the 63-overlay budget outright.
        virtual = None
        if scroll_id is not None and self.art._size is not None:
            virtual = {"offset": max(0.0, self.scroll.offset(scroll_id) - head_h),
                       "height": float(self.art._size[1])}
        # The window has to be known HERE, not just inside Table: art cells
        # composite a bitmap into the 48-entry strip LRU as they are built,
        # so building them for every row of a long playlist evicted (and
        # freed the backing buffer of) the very rows on screen — they drew
        # blank, deterministically, on every repaint.
        art_first, art_last = virtual_window(virtual, TRACK_ROW_H,
                                             len(tracks))

        rows = []
        for i, tr in enumerate(tracks):
            playing = playing_id is not None and tr.get("Id") == playing_id
            cells = [first_cell(i, tr), tr.get("Name", ""), components.track_artists(tr)]
            if art:
                cells.insert(0, self.art_cell(tr)
                             if art_first <= i < art_last
                             else self._art_placeholder())
            if album:
                cells.append(tr.get("Album", "") or "")
            cells.append(components.track_duration(tr))
            row = {
                "id": "%s-%d" % (prefix, i),
                "selected": i in selected or playing,
                "cells": cells,
                "on_click": ((lambda mods, i=i: on_select(i, mods))
                             if on_select is not None
                             else (lambda i=i: on_play(i))),
            }
            if on_select is not None:
                # A selectable list's row click SELECTS, so the only way to
                # play was the little arrow in the first column. Double-click
                # is what every media app does, and Table has carried on_dbl
                # unused since it was written.
                row["on_dbl"] = lambda i=i: on_play(i)
            if menu:
                # Right-click a track for the same menu a tile gets. Tiles
                # have had this all along; a Table row never asked for it, so
                # every music playlist lost Play/Queue/Favorite/Download —
                # and per-track "Remove from Playlist" entirely, leaving only
                # the bulk editor.
                row["on_context"] = (
                    lambda x, y, tr=tr: self.on_context(tr, x, y))
            rows.append(row)
        return Table(columns, rows, size=17, row_h=TRACK_ROW_H,
                     hover_bg=theme.BUTTON_BG, virtual=virtual)
