# The artwork pipeline

How a picture gets from the server to the screen: fetching and decoding
(`thumbnails.py`), transparency handling (`imageutil.py`), compositing a row into
one bitmap (`strips.py`), and choosing tile shapes and decorations
(`tile_renderer.py`).

Each of those files states its rules at the line. This is the reasoning, the
measurements, and the alternatives that were tried.

## 1. Strips

A **strip** is one BGRA bitmap holding a whole row of tiles — posters plus baked-in
captions, year/subtitle, watched checkmarks, unwatched-count badges and resume
progress bars — declared to the renderer as a single `ImageMap` with one
transparent hit-region per tile.

This is what makes tiles scale (`jellyfin_mpv_shim/mpvtk/GUIDE.md` §5/§6): a screenful is a handful
of overlays instead of one per poster, decorations dodge the "bitmaps composite
above ASS" z-order constraint, and scrolling is pure crop math on cached bitmaps.

Strips are **content-keyed**: the key folds in every visible property (poster
identity, title, watched/badge/progress, geometry), so changing any of them
composites a new bitmap under a new src. The cache is LRU-bounded so a long browse
session does not grow without limit; anything on screen was requested by the
current build and is therefore most-recent.

**A new src alone does not guarantee the renderer refreshes.** On the libmpv path
src is a malloc address, and addresses are recycled once a freed buffer leaves
`MemoryStore`'s graveyard — so a new entry can be handed a departed entry's exact
src. Every entry therefore also carries a monotonic `v` (see `_store`), and that
is what actually keeps the renderer's overlay cache from showing stale content.

**Backends.** On libmpv (in-process) strips go to a `MemoryStore` (ctypes buffers,
`&<addr>` src, no filesystem); on jsonipc they are BGRA files. The view supplies
decoded PIL posters from `thumbnails`; a tile with no poster yet renders a
placeholder and recomposites when the poster arrives, because its `poster_tag`
changes the key.

### Invalidating the cache

`StripStore.MAX_BYTES` is 96–128 MB, and **32 MB on a machine short of RAM**.

Anything that changes how a strip *looks* but not what it contains — a theme
change, a logo-legibility toggle — calls `retag()` rather than `clear()`. Tag
invalidation lets the rows recomposite as they are next drawn instead of throwing
away work that is still on screen. See `set_theme_tag`.

## 2. Transparent artwork

Channel logos and Logo artwork arrive on a transparent background, and handling
them wrong is silent.

**Decode transparent artwork to RGBA.** `thumbnails._load_image` does; everything
else still decodes to RGB. `convert("RGB")` **does not composite** — it drops the
alpha and keeps the black Pillow left underneath, which is how a black-on-transparent
logo rendered as a solid black block.

**The alpha then has to survive compositing.** `paste()` takes **one** mask, so
`strips._paint_poster`'s rounded path multiplies the art's alpha into the corner
clip rather than passing the clip alone.

### Plating

Those logos are drawn for the white page every other client puts them on. So
**every transparent logo in a given row gets the same light plate** — the tile by
recolouring its card, `TileRenderer._plated` by flattening onto a rounded one (an
art cell is its own overlay, with only `WINDOW_BG` behind it).

One plate for all of them is the point. A per-logo "does this need rescuing from
the dark surface" judgement is right about each tile and wrong about the row: it
comes out half light chips and half bare artwork, and it sits on a threshold a
downscale can walk across — the full-size logo plated, the guide's smaller copy of
the same file not.

### Which logos get plated: two settings, not one (#637)

Transparent artwork arrives in two **opposite conventions**:

- a broadcaster's **channel logo** is *dark* ink drawn for a white page, and is
  invisible here without the plate;
- a film's or series' **Logo artwork** is *white* by convention, reads on a dark
  surface already, and is only made to need a drop shadow by being plated.

So `logo_legibility_live_tv` defaults **on** and `logo_legibility_library` defaults
**off**, and both are reachable — neither is a bug and libraries differ. Off, the
artwork gets the theme's `CARD_BG` and no shadow, as jellyfin-web does.

The line is `TvChannel` and `Program` — most guide data carries no art of its own,
so the channel logo is the whole fallback — but **not** a finished recording, which
the server hands back as an ordinary item wearing its own art.
`live_tv.is_channel_artwork` answers it, because only the caller knows what it is
drawing.

`strips.logo_plate` is the single decision point so the tile compositor and the art
cells cannot diverge. The answer rides on `Tile.live` (the compositor has the
picture, not the item) and is **part of the strip cache key**, or one convention's
card colour is served to the other.

Both settings only move the colour and the shadow. **Whether** there is a plate at
all is still `plate_for`'s answer, because the callers also read it as "this is a
mark, not a photograph" — which is what keeps a wordmark letterboxed rather than
cover-cropped either way.

### The drop shadow is asked about the ink at the boundary

The only per-logo decision left. What a white plate cannot carry is white lying
*directly* on the transparency — and that is not "the logo is bright": a white
wordmark almost always ships with a keyline or a coloured mark around it, and those
read on white perfectly well. A keylined wordmark and a bare one have near-identical
luma histograms.

So `measure_transparency` also keeps `edge`, the histogram of the ring that falls
away when the opaque mask is eroded by a pixel. (Ink thinner than the erosion is
*all* ring, which is the right answer for a hairline stroke.) `plate_for` asks
`_lost_fraction(edge, light) > max_edge`; over `max_edge` the logo still gets the
same plate and `with_shadow` gives its outer ink an edge to sit against.

A keyline finer than the downscale genuinely blurs away in the small copies, so the
same logo can be shadowed in the guide's art cell and flat on the tile. Both are
faithful to the pixels being drawn, and both wear the same plate — which is why this
is tolerable where the old plate/no-plate flip was not.

`_lost_fraction` is:

- a **ramp, not a count** of the ink within some distance. Logo ink comes in tight
  clusters — PBS keeps 68% of its within two luma steps — and a hard edge through
  one flips the whole cluster.
- a **WCAG contrast ratio, not a luma distance**. That axis is not perceptually
  uniform at the dark end: black ink is 23 steps from `WINDOW_BG` and utterly
  invisible on it, but 1.2:1.

Luma is the only axis and it under-rates saturated ink. That mattered when the
question was asked about a near-black surface; the answers it gives about a light
plate and a boundary ring are the ones `tests/test_transparent_logos.py` pins.

The measurement is taken **once**, on the thumbnail worker, and parked in the
image's `info`. The compositors never scan pixels.

## 3. Tile shape

### Contain is the default; one pairing covers

The one is a **poster going into a poster tile**. There, cropping is free — a few
percent off the edge of a 2:3 key art loses nothing and the grid comes out
perfectly uniform, which is most of what a wall of posters is for — and the two are
close enough that a contain would only add thin bars.

Everywhere else the tile and the artwork can disagree by a lot, and a crop is
destructive in proportion to the disagreement:

- a **Home Videos** library is arbitrary footage — 4:3, phone video shot in
  portrait, and 16:9 all in one grid, so whatever shape the row takes, most of what
  goes in it is the wrong shape for it;
- a **film in a playlist of episodes** — `auto_geom` shapes a row from the median
  aspect ratio, right for the row and necessarily wrong for its minority, so the
  row comes out landscape and the one 2:3 poster in it loses its top and bottom;
- a **Logo** is a wordmark on transparency with no frame it was cut for, and a
  **Banner** standing in for one would be cropped to exactly the title it was
  borrowed for.

The per-row shape decision cannot fix any of that, because the mismatch is per
**item**. Contain is the half that can.

This is nearly free where it changes nothing: when the artwork and the tile already
agree, contain and cover produce the same picture. It only diverges where cover was
destroying something.

### Row shape follows the artwork, per row

`TileRenderer.auto_geom` is jellyfin-web's `cardBuilder.setCardData`: the **median**
`PrimaryImageAspectRatio` across a row picks one shape for the whole row (≥1.33
landscape + Thumb, >0.8 square, else poster). That is why a row of films comes out
as posters and a row of guide stills does not.

Per row rather than per tile because a strip is composited at one tile size.

Only the Live TV rows use `auto_geom`; the other home rows keep their
collection-type classification. See `docs/live-tv.md`.

`auto_geom`'s `≥3` bucket deliberately folds into landscape rather than banner — no
web row or grid defaults to banner, and everything landscape in web is
`overflowBackdrop`, which is our landscape tile under another name. This covers only
the *inferred* case; an explicit `imageType=Banner` from the view settings still
gets a real ~5.4:1 tile.

## 4. Cover Size scales the artwork only

`with_cover_scale` scales `tile_w`/`tile_h` and nothing else. **The type is not
touched, and neither is the caption band.**

Cover Size used to scale `title_size`/`sub_size`/`badge_size`/`caption_h` along with
the art, on the reasoning that a label under a bigger poster should be bigger. Three
things were wrong with that:

- it is a second, unlabelled text-size control — a user who wanted bigger covers got
  bigger captions they did not ask for, and one who wanted *smaller* covers (Extra
  Compact is 0.75) got captions below the floor `ui_text_min` exists to enforce;
- the badge decorations do **not** scale with it. Every offset in
  `_paint_decorations` is a fixed logical constant (the 22px disc, `BADGE_PITCH`,
  the 17px corner inset), so `badge_size` growing alone put a 24px numeral in a 22px
  disc at Extra Large;
- text scaling already has two controls that do it properly and keep working here —
  `ui_scale` through `physical()`, which scales the whole interface, and
  `ui_text_scale`/`ui_text_min` through `with_text_scale()`, which is applied
  *after* this and is what a theme's pinned caption size is measured against.

The gap is kept too, so rows keep their rhythm as the art grows.

The live-apply path (clearing every route's parked `_grid_shape`) is in
`docs/browser-shell.md` section 9.

## 5. Badges

### Shadows under a badge are not the logo shadow

`_shadowed` composites a mark over a dark halo of its own silhouette.

`span` is the size the blur is derived from: the **mark's own**, not the padded
layer's — the padded layer is computed from the blur, so measuring it there would
grow one every time the other grew to hold it. For a glyph that is its box; for text
it is the **cap height**, because what a shadow is proportional to is the weight of
the ink. Taking the *width* made a three-digit count three times blurrier than a
one-digit one and gave it a 36px layer to live in, which centred 17px below the top
of a card does not fit and was clipped.

**This is not `imageutil.with_shadow`.** That one is tuned for a logo: a large mark
with margins, wanting a hint of separation from a plate it is nearly the colour of,
so its blur is a sixtieth of the image and its alpha is a straight multiply. This is
a 20px glyph over *artwork*, which can be any colour including white, with nothing
else holding it up — the halo is not a hint, it is the entire reason the mark is
visible. Hence a blur about a tenth of the mark, an offset big enough to read as a
direction rather than a ring, and a gain that drives the middle of it opaque.

### The unplayed-episode chip

Sized to the number it carries, not to a guess about how big numbers get. It was a
fixed 26 logical px, which is three physical px **narrower** than "123" draws at the
default badge size — so a three-digit count (routine on an unwatched anime series,
and reachable by anyone who adds a show and never starts it) hung out of both ends
of its own chip. Two digits fitted, with 3px of padding against the single digit's 8.

jellyfin-web's `.countIndicator` is the same shape and grows the same way:
`padding: 0 .5em` over a min-width.

**Pinned by its right edge** rather than its centre, because that edge is the one
lined up with the badge stack beside it — growing from the middle would walk a wide
chip off the corner of the card.

**A rounded rectangle where every other badge is a disc**, which is web's split too:
this one runs to three digits, while the version count beside it is a count of files
on disk and stays at one.

It returns the horizontal pitch the next badge must clear, which is why it is a
function rather than four lines inline: it sits *in* the corner, so unlike every disc
in the stack the badge to its left has to clear a width that depends on the text.
`BADGE_PITCH` is a floor here, not the answer.

## 6. Headers bake their heading into the bitmap

With a title, the heading is **baked into the banner bitmap** over a bottom gradient.
Text drawn as ASS would sit under the image (bitmaps composite above all script ASS),
and the occlude punch would show the window background rather than the artwork.

**The waiting state bakes the same heading over a flat panel**, and that is not
cosmetic. Baking the heading into the artwork means the heading is *inside* the
banner's fixed box when the art is there, and has to be drawn somewhere else when it
is not — so a header that drew it below the banner while waiting moved everything
under it, play buttons included, the moment the image arrived, by the height of up to
three text blocks.

Composing the placeholder through the same function fixes the geometry at the first
paint, keeps the text in the same place within the banner, and leaves the title
readable if the fetch never succeeds — `_request_image` gives up after
`IMG_MAX_ATTEMPTS`, and a permanent failure would otherwise be an anonymous grey
panel forever.

A plain placeholder Box is returned only when the item has **no artwork of any kind**
— no backdrop and no poster to inset — because then there is no baked heading to
match and the caller draws its own. `header_bakes_heading` is how a caller tells the
two apart; *not* the returned node's type, which cannot distinguish "none" from "not
yet".

## 7. Grid centring

A whole number of tiles rarely divides the available width exactly, and the remainder
used to land entirely on the right: at some window sizes that is most of a tile's
width of empty background down one side, which reads as the page being misaligned
rather than as a grid fitting what it can fit. Split it evenly.

Measured against `body_w` — inside the scrollbar gutter, when there is one — because
the bar is furniture the reader can see. Centring against the whole window would push
the block half a scrollbar left of centre, which is the same fault smaller.

Returned as a padding rather than applied to the rows, so the header above the grid
moves with it and the title stays aligned with the first tile in the first column.

There used to be a residual: `body_w` subtracts the scrollbar gutter, and the gutter
was reserved only when a page actually scrolled, so a grid short enough not to scroll
sat 10px left of centre — unknowable at that point, because whether the bar appears
was a function of the laid-out height. `layout._arrange_scroll` reserves it
unconditionally now (and renderer.lua paints the track whenever it is reserved), so
`body_w` is right either way.

## 8. Measuring a server's artwork handling

`tools/bench_image_loading.py [--config DIR] [--library NAME]` profiles whatever the
reporter is already signed in to. It reads saved credentials out of a config
directory (the app's own by default) and is read-only.

It reports a poster cold vs cached, the unresized original for comparison, whether a
size one pixel wider costs full price (**Jellyfin caches per exact pixel size, so it
does** — which is why every window size re-does every poster), and a whole first paint
at the browser's real burst size and worker count.

That last number is the one to read. A first paint that keeps the server busy past the
apiclient's 30 s timeout does not make the library slow — it makes a browse query fail
the screen with "Failed to load. Check the connection." (`_route_async`).

## 9. Quantising artwork requests

Jellyfin caches per exact pixel size, so a dimension that is a *continuous* function of
the window width asks for a new picture on every pixel of a drag.

**Banners** (`BANNER_STEP = 128`). The banner width used to be continuous, so dragging
a window edge across 400px asked the server for up to 400 different backdrops, decoded
400 bitmaps and kept them all resident until the LRU pushed them out — issue **#592**,
and the reason a resize both hammered the access log and ballooned memory.

jellyfin-web does the same thing for the same stated reason: it rounds the screen width
down to a multiple of 100 "to improve cache hits" (`cardBuilder.js:126-129`). 128 rather
than 100 because these are pixels in a cache key, not a CSS breakpoint — it makes at
most nine distinct banner widths between a small window and the 1100 cap.

**Rounded up, never down**, so the bitmap is never asked to upscale: a banner drawn
slightly wider than requested is cropped by the compositor, which is invisible; one
drawn narrower is soft.

**The header's inset poster** is the axis that was left uncovered. The slot is a
*fraction* of the banner, so it moves with every pixel of the banner's width, and a
drag-resize minted a poster request roughly every four pixels. Padded headers hid most
of it behind the 1100 cap — above that window width the slot stops moving — but a
full-bleed header has no cap, so there it ran for the whole drag.

Also note `box` arrives **physical** from `backdrop_node` (which rastered it) and
`poster_box` works in the same units, so rastering again asks for a poster at
scale-squared on any HiDPI display, cached under a key the drawn size never matches.

The fetch key carries the width only — see `docs/jellyfin-api-notes.md` on `maxWidth`.

## 10. Fetching and decoding: the thumbnail store

`jellyfin_mpv_shim/mpvtk_browser/thumbnails.py` is the front of the pipeline:
`request()` hands a key to a worker pool, the worker reads the disk cache or the
network, decodes and resizes with Pillow, and `pump()` delivers a `PIL.Image` to
the loop thread. Nothing here is thread-affine — what comes out is a PIL image
the strip compositor pastes into a row bitmap (§1), not a toolkit object.

### 10.1 Where the cache goes, and how big

Two caches, one per medium, and **the budget follows the medium**.

On disk what is kept is the server's own **compressed** bytes, not decoded
pixels: a poster is **20–80 KiB** there against **~300 KiB decoded**. Artwork is
also long-lived — the key folds in the server's own image tag, so an entry is
never *wrong*, only unwanted — and it belongs somewhere that keeps it between
launches. So `DEFAULT_DISK_MB` is **1024**: a gigabyte is a whole large library's
artwork at every size it has been drawn at, on a medium where that is a rounding
error.

`disk_cache()` prefers a real cache directory (`XDG_CACHE_HOME`,
`~/Library/Caches`, `LOCALAPPDATA`), so a poster fetched last week is still the
right poster and no launch re-fetches a library it already has.
`SCRATCH_DISK_MB` (**64**) is the fallback, taken only when that directory cannot
be created at all — a read-only home, a sandbox. That is a sixteenth of the room
because the fallback is the session's tmpfs: it is RAM, on machines that may have
only 8 GiB of it, and everything in it dies with the session anyway. It **hardly
ever fires**, and never on its own: a machine that will not take a cache
directory will not take a config one either. It exists so that a cache cannot
stop the app starting.

The in-memory half is `MemoryCache`, a byte-bounded LRU of decoded images sized
by bytes rather than entry count, so a mix of small posters and large backdrops
cannot balloon. It is deliberately modest (`conf.library_image_cache_mb`, 96 MB)
because it sits behind the strip cache: a decoded poster is only wanted while a
row is being composited. `trim_memory` shrinks it to `ROUTE_KEEP_BYTES` (16 MiB)
when the browser leaves a screen — not to zero, because the chrome that outlives
a navigation draws from it too, most visibly the now-playing bar's album art.

### 10.2 The budget is not a promise

`LOW_DISK_SHARE` (**0.05**) caps the cache at five per cent of what is available
to it, whatever the configured budget says, and the smaller of the two wins.

This is **one continuous rule, not a "low disk space" mode**. On a roomy disk 5%
is far more than the budget and the budget binds; the share only starts to matter
**below roughly 20 GiB free**, and from there it shrinks the cache continuously
instead of waiting for a threshold to trip. A cache that gives space back as a
disk fills is the useful behaviour, because somebody else filling it is the
common case and holding still does not help them. The share is measured against
free space **plus what the cache already holds**, or it would ratchet its own
allowance down every time it grew.

`MIN_DISK_BYTES` (24 MiB) is the floor — a cache too small for one screenful
re-fetches every tile on every scroll, which costs the server and the user more
than the space saves — but **it gives way too**: on a filesystem where even that
is a quarter of everything left, the floor is the rudeness.

`MAX_AGE_SECS` (**30 days**) is the other bound, and it answers a different
question. The size bound decides what a busy cache may keep; the age bound
decides what an idle one is still *for*. The key folds in the image tag **and the
requested pixel size**, so changing the Cover Size (§4), the theme's tile shape,
or the window it is measured against **orphans** every entry made for the old one
rather than replacing it. Nothing can invalidate those explicitly — an orphan is
only recognisable by nobody having read it — so age is the reaper for all of it,
and a month is long enough that a library you go back to seasonally is still
warm. A disk hit therefore `utime`s the file *after* reading it, in its own
`try`, which is what makes the bound mean "unused for a month" rather than
"fetched a month ago".

### 10.3 Pruning is paced by traffic

A prune stats the whole cache directory, so it is counted rather than run per
file: `_note_written` accumulates bytes and prunes at `PRUNE_EVERY` (**16 MiB**),
which is a few times per browsing session instead of a few times per screen. The
accounting sits outside the write's `try`, because one failed `replace` — Windows,
over a file another reader has open — used to skip it and stall the trigger for
the rest of the session.

**One pruner at a time** (`_prune_disk` takes the lock non-blockingly and returns
if it is held). Two workers, or two app instances sharing the persistent cache,
each scan, each compute the same total and each delete towards it — but a file
the other already removed raises `ENOENT`, and a loser that did not decrement its
own running total kept deleting. Between them they evicted roughly twice the
excess, which is why the `FileNotFoundError` arm still subtracts the size.

The startup prune is submitted to the pool, **deliberately not run on the calling
thread**. It used to measure a directory that had just been created; it now
measures a persistent one that may hold thousands of files, and it runs on the
path that opens the browser window — one `listdir` plus a stat per entry is
seconds on a cold cache or on NTFS, all of it before anything is on screen. The
future is kept only so a caller that wants to reason about the directory can let
it finish.

### 10.4 Decode: `_fit_into` cover-crops and never upscales

Everything a poster tile draws is cover-cropped at paint time
(`strips._paint_poster` → `ImageOps.fit`), so a decode that used
`Image.thumbnail` — the *contain* half of the pair — threw away exactly the
pixels the crop was about to want, and the fit then had to magnify what was left.
Nothing about that is visible to a size or a shape assertion: the answer is the
same picture, softer.

Measured over the shapes a cover tile actually sees, against a **200×300** tile,
this is what the contain used to cost in magnification: **1.20× for 4:5 art,
1.50× for a square headshot, 2.65× for a 16:9 still** — all of it on pixels the
server had already sent. The square and the 16:9 are not exotic: a
`BaseItemPerson` carries no `PrimaryImageAspectRatio` at all, so every Cast & Crew
tile lands here. `tests/test_grid_artwork.py:271` pins both halves — that the
paint never magnifies the decode, and what the contain was charging.

`_fit_into` **never upscales**, which is the difference between it and asking
`scale_to_cover` for the box outright. A source too small to fill the box is
cropped to the shape and left at its own resolution; the paint-time fit then
resizes it, from the same pixels and by the same filter, so the picture is
identical and the decoded cache does not hold a magnified copy of a small image.

Transparency opts out of cover entirely: a mark on a transparent background is
not a photograph, and the compositor refuses to crop one whatever the tile says
(§2). Cropping it in the decode would be a decision taken before the thing that
decides has looked.

### 10.5 Crop in the source, resample once

`imageutil.scale_to_cover` scales and crops in **one** call. The obvious
spelling — scale the whole picture up to cover, then crop the box out of it —
pays for every pixel it is about to throw away, and a full-bleed banner throws
away most of them: a 1920×1080 backdrop covering a **6390×412** header is
resampled to 6390×3596 so that 412 rows of it can be kept. Pillow's `box` does
the crop as part of the resample, so the same call reads 1920×124 out of the
source and writes the 6390×412 that is wanted. **Measured at that size: 194 ms
and +204 MB of peak RSS became 24 ms and +32 MB.** This runs on the loop thread,
once per pixel of a drag-resize, because the bitmap has to be exactly as wide as
the header it is drawn in and so cannot be quantised the way the *request* for it
is (§9).

The box is a **float box, deliberately**. The integer version had to round the
scaled size up and clamp the crop, because a truncated product landed a pixel
short and Pillow pads an out-of-bounds crop with transparent black rather than
refusing — a hairline down the edge of the banner for **about one width in
eighty**. In source space the box is exact by construction (`w / scale <= iw` on
the binding axis and below it on the other), so there is nothing to round off the
edge of the picture. `tests/test_imageutil.py`'s
`test_no_width_leaves_a_transparent_hairline` pins it.

`gravity_y` is stated as a point in the *source* rather than as an alignment of
the crop box, so it keeps its meaning when the box is too tall for the bias to be
honoured: the crop is clamped to the picture and goes as far that way as it can.

### 10.6 The wire

**`pool_block=True`.** urllib3 defaults to `pool_maxsize=10` with
`pool_block=False`, which does not queue an over-limit request — it opens a fresh
connection and then **discards** it instead of returning it to the pool. With
these workers plus the trickplay tile fetch hitting the same host, that meant a
churn of one-shot TLS handshakes exactly while mpv was opening the stream, which
is a very good fit for the intermittent "tls: Error decoding the received TLS
packet" seen in the field. The pool is sized to the worker count and blocks on
exhaustion instead. `THUMB_POOL_HOSTS` is 4 and small on purpose: this store
talks to the logged-in Jellyfin servers and nothing else.

**`Authorization` per origin, not a query-string token.** Artwork is by far the
highest-volume first-party traffic this app makes, and putting the token in the
query string means an admin cannot put Jellyfin behind a proxy that rejects
unauthenticated traffic — every tile would 401. The map is keyed by
scheme+host+port because a session can hold several servers at once, so a token
only ever travels to the server it came from, and never over plain http to one
reached by https. `set_auth` replaces it wholesale rather than merging: a server
the user has just signed out of must stop receiving its old token.

**A real User-Agent, unconditionally.** It identifies the client, not the
session, and there is nothing in it to leak. Without it a server's access log is
mostly anonymous `python-requests/x.y` lines that nothing ties back to the shim —
which is exactly the log somebody reads when they are working out which client is
hammering them.

Failures are classified rather than merely logged: a **4xx** means the image is
not there and never will be at this URL, so the key goes in `_gone` and the
caller can stop asking (`is_gone`); a timeout, a connection reset, a 5xx or a
truncated body is transient and stays retryable. The callback runs either way,
with `image=None`, so a caller can release its dedup marker — a fetch that failed
silently used to leave the tile blank permanently.

### 10.7 Two measurements that belong elsewhere

**Why a luma mean decides nothing (§2).** A logo is routinely *bimodal*: the NBC
peacock is a bright mark next to a black wordmark, and **its mean luma of 71
describes no pixel in the image**. That is why `measure_transparency` keeps the
whole 256-bin histogram, and the `edge` ring beside it, and why `plate_for` asks
about distributions rather than about an average.

**A slot that asks for far more picture than it can draw (§9).** The header's
inset poster is bounded by `POSTER_W_FRAC` of the banner, which grows without
limit while the slot's height is fixed — widening a banner buys backdrop, not
page. So on a very wide window the slot came out **1597px wide for a picture that
can never be drawn wider than ~570**, and since `_banner_poster` sizes its
*request* from the slot, a detail page pulled a **1000×1500 poster whole (185 KB)
to draw it at 214px (19 KB)**. Nothing inset there is wider than a 16:9 still, so
`banner.poster_box` caps the width against the slot's own height
(`MAX_INSET_ASPECT`) as well as against the banner's width.
