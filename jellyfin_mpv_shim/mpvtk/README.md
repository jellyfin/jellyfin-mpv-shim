# mpvtk — declarative UI framework rendered inside the mpv window

A full declarative UI toolkit — widget tree, layout engine, and an
in-mpv renderer — that draws application UI directly into the mpv
window using only mpv's OSD primitives (`osd-overlay` ASS + bitmap
`overlay-add`). `mpvtk_browser/` is the real workload built on it: a
complete Jellyfin library browser (home, tiles/strips, search, music,
downloads, settings, cast, dialogs) rendered entirely this way.

> **This file** is the overview: why the framework exists, the
> architecture, and the constraints of building UI on OSD primitives.
> **GUIDE.md** is the durable developer reference (protocol, full widget
> catalog, renderer internals, testing). Start here, then go there.

## Why this exists

The goal was to render the library-browser UI *on top of the video*,
sharing one window with the player. The conventional answer — embed
mpv in a desktop toolkit (Tkinter) and/or overlay a native child
window — has primitives for this on Windows and X11, but it breaks
down where it matters:

- **Process isolation.** mpv runs isolated from Tkinter for
  thread-cleanliness reasons; punching a shared, correctly-Z-ordered,
  DPI-correct native surface across that boundary is fragile.
- **Wayland.** There is no Tkinter-in-mpv-window story on Wayland, and
  we're unwilling to give up high-quality rendering on the future of
  Linux desktop just for a UI-polish feature.
- **DPI-aware scaling** and similar per-platform surface headaches.

So mpvtk **inverts the usual "embed mpv in a toolkit" architecture**:
mpv owns the window and swapchain, and the UI is OSD data composited in
mpv's *own* render pass. The video path is untouched — native VO,
direct scanout, and HDR passthrough all stay intact — and there is no
cross-process surface to keep in sync, because the UI *is* mpv output.
The cost is that everything must be expressible in mpv's OSD
primitives; the constraints that flow from that are catalogued below.

## Architecture

```
Python                                 mpv process (either backend)
──────                                 ────────────────────────────
widgets.py   declarative element tree
layout.py    → flat scene (abs coords)
app.py       → JSON over script-message ──►  renderer.lua
                                             draws ASS (osd-overlay)
             semantic events            ◄──  + BGRA files (overlay-add)
             (click/change/select)           owns hover, scroll offsets,
             → rebuild tree, push scene      text editing, popups locally
```

- **No per-frame IPC.** The Lua renderer handles hover, wheel/drag
  scrolling, cursor blink, and dropdown popups locally. Python is only
  involved when a semantic event fires, and responds by pushing a whole
  new scene (~tens of KB of JSON; full replace, no diffing).
- **Renderer-local state survives scene pushes**, keyed by node id:
  scroll offsets, textbox text/cursor/focus, dropdown selection. A node
  with `force=true` resets its state from the scene. Stateful widgets
  need explicit, unique ids (layout warns on duplicates).
- **Images** are pre-rasterized once to raw premultiplied-BGRA files
  (`rawimage.write_bgra`) at display size, in a plain temp dir — no
  tmpfs assumption; mpv re-reads them through the OS page cache when
  the renderer re-issues `overlay-add` during scrolling. Partial
  visibility at viewport edges is handled by cropping the source via
  offset/stride math, not by moving pixels.
- **Both backends**: the renderer runs inside mpv, so python-mpv-jsonipc
  and libmpv are identical (`--backend` in the demo; 13/13 selftest
  checks pass on both).

## Scaling to a real app: compositing, virtualization, popups

These are the techniques that take the model from "draws widgets" to
"runs a full library browser" — how the overlay budget, z-order, and
scroll cost are kept survivable at app scale.


- **Strip compositing (`ImageMap`)**: whole tile rows are baked into one
  BGRA file — posters, captions, progress bars, unwatched badges,
  watched checkmarks. This dissolves the z-order problem for
  data-driven decorations AND the overlay budget (a screenful is 2–8
  overlays regardless of tile count). Interaction stays declarative:
  regions become transparent hit-rects with outside hover rings.
  Strips are content-keyed (decoration changes recomposite under a new
  filename); `v` busts the renderer cache for in-place rewrites.
  Scrolling a strip is pure crop math — no recomposite.
- **Infinite scroll**: `Scroll(on_scroll=...)` gets debounced offset
  events from the renderer; the app materializes rows around the
  viewport with fixed-size `Spacer`s standing in for the rest. The demo
  virtualizes a 400-entry grid at 3 live overlays.
- **Context menus**: right-click on a node with `on_context` →
  `context` event → app re-renders with a floating `Menu` node at the
  click point. Same popup path as dropdowns (occludes images, hover,
  flip-at-edge); click-away/ESC dismiss instantly renderer-side and
  notify the app.
- **Measured metrics**: at startup Pillow measures real glyph advances
  for the platform UI font; both layout and renderer use the table and
  libass gets `\fn` for the same font, so sizing/ellipsis/cursor all
  agree. Heuristic table remains the fallback.
- **Memory overlays (libmpv backend)**: images live in ctypes buffers
  and reach mpv via overlay-add's same-process `&<address>` form — no
  files at all, so nothing on mpv's command path touches the fs during
  scrolling (file re-reads there were both a lag source and, on a slow
  fs, an input-stall risk). The renderer folds crop offsets into the
  address. Buffer lifetime: entries stay alive while any scene refers
  to them (LRU recency guarantees visible strips are hot) and frees go
  through a small graveyard to cover in-flight re-issues. The file
  path remains for jsonipc (RAM-backed dirs + FILE_ATTRIBUTE_TEMPORARY
  on Windows).
- **Flush ordering**: overlay adds/replacements are issued before
  removes, and new images take over departing slots via direct
  overlay-add — remove-before-add produced one-frame holes visible as
  tile flicker while scrolling.

Hard-won crash lesson: a crop that reads past the end of an image
source can kill the process, so image nodes never stretch past their
pixel size and the renderer clamps every crop to `iw`/`ih`. The exact
failure mode is **version-dependent**, and the clamp is required on all
of them:

- **File source, mpv ≤ 0.41**: the file is `mmap`'d from 0 to
  `offset + h*stride`, so reading past EOF touches mapped-but-absent
  pages → **silent SIGBUS**. This map also grows with `offset`, so a
  far-scrolled tall strip re-mmaps a region proportional to scroll
  depth on every re-issue (a real jsonipc-path cost).
- **File source, mpv ≥ 0.42**: no mmap — the source is `fseek`+`fread`
  (commit `3cd66d2fd7`). A past-EOF crop is now a **short read → soft
  command failure** (`overlay-add: could not open or read`), not a
  crash, and the offset-proportional cost is gone (a seek is constant).
  Note ≥ 0.42 also *mutates a passed `@fd`'s file position* — we use
  filenames, not `@fd`, so this doesn't touch us.
- **`&<address>` memory source (libmpv), all versions**: `overlay-add`
  `memcpy_pic`s straight from the pointer with **no bounds check**, so
  an OOB crop is a hard **SIGSEGV** regardless of mpv version. The
  clamp is load-bearing here on every build; only the *file*-path
  SIGBUS is what 0.42 defuses.

## Constraints & hard-won lessons

These are inherent properties of building UI on mpv's OSD primitives —
not bugs to fix, but the shape of the box we're working inside.


1. **overlay-add bitmaps composite ABOVE all script ASS** (verified on
   mpv 0.41; the thumbfast "hole punch" comment suggests the opposite —
   its hole serves translucency, not visibility). Consequences:
   - ASS cannot draw on top of an image. Captions/labels must be laid
     out beside images, not over them.
   - Chrome that must cover images (dropdown popups; later: dialogs)
     is an *occluder*: the renderer subtracts its rect from every
     image's visible region, emitting up to 4 sub-overlays per image.
   - Hover rings draw just outside image bounds.
2. **Text metrics are approximated** (per-char width table, duplicated
   in layout.py and renderer.lua). Fine for sizing boxes and ellipsis;
   cursor positioning drifts slightly on unusual glyphs. A follow-up
   could ship a real width table for the chosen font (uosc does this).
3. **Text input** enumerates printable ASCII as forced key bindings
   while a textbox is focused; Ctrl+V pastes via the `clipboard/text`
   property (mpv ≥ 0.40). IME (CJK) input is not available — the
   long-term path is mpv's `mp.input` / Wayland text-input integration.
4. **Overlay budget** is 63 ids. Off-viewport images cost nothing;
   ~50 simultaneously visible posters is comfortable. The renderer
   warns and drops beyond the budget.
5. **Screenshots need a video frame**: `screenshot-to-file` fails on a
   pure OSD window (selftest falls back to X11 capture).
6. Wheel events walk up the scroll chain by axis: vertical wheel over a
   tile row scrolls the page; horizontal (or shift+) wheel scrolls the
   row.
7. **Smooth scrolling is fundamentally not on the table; scrolling is
   hard-stepped by design.** `overlay-add`'s bitmap path was never built
   for continuously-moving UI. Each re-issue runs mpv's internal
   `recreate_overlays`, which re-atlases and re-copies *every* active
   overlay (not just the changed one), and mpv copies the bitmap ~3×
   more before it blends (ingest → atlas → `sub_bitmaps_copy` →
   per-render `sub_bitmaps_copy`). There is **no client-side lever** to
   collapse that; all we can do is shrink source area (we're at display
   res) and overlay count (strip compositing → 2–8/screen). We've done
   both, plus sticky-slot no-op suppression so an unchanged strip is
   never re-issued. Consequently every scroll tick moves by a whole step
   rather than a pixel — the copy cost is paid once per step instead of
   once per frame. At 4K the per-step copy is still visibly a beat
   behind; that's the ceiling of the OSD-bitmap approach, not a bug to
   chase.

## Demo / selftest

```sh
python3 -m jellyfin_mpv_shim.mpvtk                 # interactive (jsonipc)
python3 -m jellyfin_mpv_shim.mpvtk --backend libmpv
xvfb-run -a python3 -m jellyfin_mpv_shim.mpvtk --selftest /tmp/shots
```

The selftest drives the renderer's `mpvtk-debug` hooks (hover/click by
node id, wheel, typing, popup selection), screenshots each step, and
asserts on renderer state and Python-side model updates.

Unit tests for the layout engine: `python3 -m unittest tests.test_mpvtk_layout`.

## Not yet built (deliberately out of scope)

- Modal dialogs (the menu/popup floating-layer + occluder mechanism
  generalizes to them).
- Focus traversal (Tab), keyboard/remote navigation of tiles.
- Momentum/animated scrolling (ASS `\t` or renderer timers).
- Scene diffing (full-replace is fine at this scale).
- Image scaling in the renderer (pre-scale with Pillow instead).
- Text selection in textboxes (clipboard paste works; select/copy is
  ~150 lines of renderer work).
- IME text input; native file chooser (keep Tk for that one settings
  page or use a path textbox).
- Non-ASCII glyph metrics (measured table covers printable ASCII).
