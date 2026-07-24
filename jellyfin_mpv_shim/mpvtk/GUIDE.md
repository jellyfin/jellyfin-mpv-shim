# mpvtk — developer guide

A declarative UI toolkit that renders inside the mpv window. Python
owns application state and layout; a Lua engine inside mpv owns all
per-frame interaction. This document is the durable context for anyone
(including future us) building on it. Companion doc: `README.md`
(rationale, architecture overview, and the constraints of building on
mpv's OSD primitives).

## 1. Architecture

```
Python (app process / thread)          inside mpv
─────────────────────────────          ─────────────────────────────
your app state                         renderer.lua
  └─ build(size) -> widget tree          ├─ draws ASS (osd-overlay)
       └─ layout() -> flat scene         ├─ places bitmaps (overlay-add)
            └─ JSON via script-message ► ├─ owns: hover, scrolling,
                                         │  text editing, dropdowns,
   semantic events ◄─────────────────────┤  menus, dialogs, sliders,
   (click/change/select/scroll/...)      │  spinner animation
       └─ mutate state, invalidate()     └─ renderer-local state
            └─ full new scene pushed        survives scene pushes
```

Principles:

- **No per-frame IPC.** Anything that must feel instant (hover,
  wheel/drag scrolling, cursor blink, popup hovers, spinner frames)
  happens renderer-side. Python is involved only for *semantic* events.
- **Scenes are full replacements.** No diffing. A scene is ~100–300
  nodes / tens of KB of JSON; a full build+layout+push measures ~1ms
  (25ms when strips recomposite). Renderer-local state carries across.
- **Renderer-local state wins** for stateful widgets (scroll offsets,
  textbox text/cursor/selection, dropdown selection, slider value),
  keyed by node id. A node with `force=true` resets from the scene.
  Consequence: stateful widgets need explicit, unique ids (layout
  warns on duplicates — a duplicate silently breaks hit-testing).
- **Both backends identical.** The Lua runs inside mpv either way;
  python-mpv-jsonipc and libmpv differ only in spawn/attach plumbing
  (`app.py` backends) and the image transport (§5).

## 2. Widget catalog (`widgets.py`)

Layout: `Box` (direction, `pad` — uniform or `(pad_x, pad_y)`, gap,
cross-axis `align`, main-axis `justify` start/center/end/between,
bg/radius/border, on_click/on_dbl, hover, `tip=` tooltip),
`Row`/`Column` sugar, `Spacer` (flexes unless given w/h — a
sized Spacer is the stand-in for virtualized content), `Stack`
(children share one rect, per-child `anchor`/`dx`/`dy`; scrolls with
the page unlike `Float` — the way to pin arrows/badges to a row; see
§6 for what may draw over what), `Grid` (cells on shared column
tracks — `{"w"}`/`{"flex"}`/`{}` auto — so sibling rows can't drift;
rows may be dicts carrying card chrome — `bg`/`radius`/`id`/
`on_click`/`on_dbl`/`hover` draw a full-width row rect behind the
cells, `row_pad` insets them — for management-list rows; `Form` sugar
for label+input rows), `Table` (header + rows generated
from one column spec — `{"label", "w"|"flex", "align"}`; rows take
`selected`/`fg`/`bg`/`on_click`/`on_dbl`, cells may be Elements
(album-art thumbnails, buttons); `virtual={"offset", "height"}`
materializes only the visible rows, fed from `scroll_offsets()`).

Content: `Text` (size/color/bold/align; ellipsized to fit, or
`wrap=True` + `max_lines` to word-wrap to the laid-out width),
`Image` (pre-rasterized BGRA; never scaled or stretched — see §5),
`ImageMap` (one composited bitmap + interactive sub-regions; THE tile
primitive, see §5), `Button` (Box+Text sugar; `repeat=True` refires
on_click while held — paging arrows; `flat=True` is the
transparent-over-video style: nothing at rest, translucent hover
wash), `Checkbox` (Row sugar), `Progress` (determinate bar; `Busy`
stays the indeterminate spinner), `Gradient` (vertical fade — one
solid ASS box with a gaussian-blurred fading edge, the banding-free
technique from the lua OSC; ASS, so ordinary content still draws on
top — the playback HUD's bottom scrim). `Dropdown(trigger_icon=…)` swaps the
boxed control for a bare icon button; its popup sizes to the items
and clamps to the screen edges.

Every element takes `tip="…"` — a renderer-drawn tooltip after a
0.5s hover delay (occludes images like a popup). `MpvtkApp.node_rect
(id)` returns a node's laid-out geometry from the last pushed scene —
layout feedback for the next build (header offsets above virtualized
lists, overflow decisions).

Inputs: `TextBox` (editing, paste, selection, `mask=True` for
passwords; `on_change`/`on_submit`), `Dropdown` (readonly picker,
`on_select`), `Slider` (`on_change`, throttled while dragging;
seek-style sliders add `on_commit` — once, when the drag/adjust
gesture ends — and `on_cancel` — gesture abandoned via ESC or focus
moving away, value reverted; `force=True` tracks the scene value but
never stomps an in-flight gesture), `Busy` (indeterminate spinner).

Containers: `HScroll`/`VScroll` (optional scrollbar; `on_scroll` for
windowed/infinite content — fires leading-edge-throttled every 150ms
during scrolling).

`Icon` (Material vector icon — the same generated set and SVG→ASS
pipeline as the Tk UI and the OSC via the shared `svgpath` module;
24×24 unit canvas with corner anchors, scaled crisp via `\fscx`;
compose with Text in a Row for labelled buttons). `Dropdown` and
`Menu` take per-item `icons=` name lists.

Floating: `Menu` (context menu at a point; `on_select`/`on_dismiss`),
`Dialog` (centered modal, grabs all input, ESC/click-away →
`on_dismiss`), `Float` (positioned toast/banner, no grab). All floating
content draws above everything and occludes image overlays.

Every element takes `id=`, `w=`, `h=`, `flex=`, and size constraints
`min_w`/`max_w`/`min_h`/`max_h` — int px, or a float in (0, 1] as a
fraction of the available space (a Dialog child resolves fractions
against the window: "natural, but at most 60% of the screen"). Rows
flex-shrink on overflow: fixed/natural children squeeze proportionally
down to their min (bitmaps/icons AND clickable Boxes — buttons — floor
at natural; a squeezed "E…" button is garbage, so plain Text absorbs
the shrink and re-ellipsizes); columns still overflow on purpose
(vertical overflow is pre-scroll content, not an error). `layout.natural_size(tree)` is
the build-time fit probe: measure a candidate (e.g. the labelled
chrome bar) against the window and pick a layout — no hardcoded
breakpoints.

**Spatial navigation (10ft)** is renderer-local and always on while
the UI is active: arrow keys walk the focusable nodes (anything
clickable, plus textboxes/dropdowns/sliders — inferred from the scene,
no protocol additions), scored by direction with an accent focus ring
drawn outside the node; focus scrolls its containers into view. ENTER
activates: clicks buttons/rows, focuses a textbox (whose own keys then
own the arrows), opens a dropdown (UP/DOWN walk the popup, ENTER
picks — same for context menus), toggles slider adjust mode
(LEFT/RIGHT step 5%, white ring while active; the accent ring
otherwise, and it replaces hover styling on the focused node). Any
mouse press drops key focus. Direction picking is container-aware and
tiered: aligned candidates inside the focused node's own scroll
containers win first; then the container pages ~60% of a viewport
along the axis and retries (completing on the next scene push if the
content wasn't materialized yet); only when the containers are
exhausted may focus escape to fixed chrome (top bar, now-playing
bar). Vertical moves are row-focused: the nearest row beyond the
node's edge wins, then the horizontally nearest element within it —
no x-overlap required, so UP from a right-hand button lands in the
row directly above it. Horizontal moves stay overlap-confined to
their row. Vertical navigation wraps: UP with nothing above jumps to
the bottom-most row (the now-playing bar is two presses from anywhere
in a long list), DOWN past the end wraps to the top. Scroll-into-view uses asymmetric margins (56px leading, 12px
trailing) so a row's heading scrolls in with its carousel. Modality is
reported to the app as the `nav` event (`MpvtkApp.on_nav`): the
browser hides carousel arrows while keyboard/remote navigation is
engaged. The bindings live with the mouse sections: suspended by
`mpvtk-active no` so playback keeps its seek keys, and the active
state is mirrored to `user-data/mpvtk/active` so the player can route
Jellyfin remote commands (MoveUp/Select/…) into these keys only while
the UI owns them.

## 3. Scene protocol (Python → Lua)

`script-message mpvtk-scene <json>`:
`{"v":1, "w":W, "h":H, "nodes":[...]}` — flat, paint-ordered.
Common fields: `t`, `id`, `x/y/w/h` (absolute OSD px), `sc` (owning
scroll container id), `top` (floating layer), `mod` (modal layer).

| t | extra fields |
|---|---|
| rect | fill, a, radius, bc/bw, click, ctx, rpt (hold-repeat), hover{fill,bc,c}, ring |
| text | text, size, c, bold, align, click, hover (one node per wrapped line: `id`, `id.l1`, …) |
| img | src (path or `&addr`), iw, ih, v (cache-bust) |
| scroll | axis, cw/ch (content), bar, watch |
| textbox | text, ph, size, mask, force |
| dropdown | items, sel, size, force |
| slider | min, max, value, force |
| busy | — |
| menu | items, size, ih (floating; x/y absolute) |
| layer | kind: modal\|float (meta: bounds for grab/occlusion) |
| occ | Stack `occlude=True` marker: rect subtracted from images earlier in paint order |

Children of a scroll are positioned in content space as if offset 0;
the renderer subtracts live offsets and clips. `ring` marks transparent
hit-rects over bitmaps whose hover ring draws *outside* their bounds.

Other messages: `mpvtk-metrics` (measured glyph widths + font family,
pushed once at ready), `mpvtk-debug` (test hooks, §7).

## 4. Events (Lua → Python)

`script-message mpvtk-event <json>`; `app.py` dispatches to the
handlers registered during layout:

| t | payload | fires |
|---|---|---|
| ready / resize | w, h | osd size known/changed |
| click | id, shift?, ctrl? | press+release on same target (`rpt` nodes: on press, refiring while held) |
| dbl | id | double-click on a node with on_dbl (after its two clicks) |
| nav | active | keyboard/remote navigation engaged / mouse took over (`MpvtkApp.on_nav`) |
| context | id, x, y | right-click on a node with on_context |
| change | id, value | textbox keystrokes; slider (throttled) |
| submit | id, value | textbox ENTER |
| select | id, index, value | dropdown or menu item chosen |
| dismiss | id | menu/dialog click-away or ESC |
| scroll | id, offset, max | watched scrolls, ≤ every 150ms |
| clipboard | op, need | a textbox copy/paste found no clipboard at all (`MpvtkApp.on_clipboard_error`); once per renderer |
| debug_state | … | reply to the `state` debug hook |

Click handlers opt into the modifier payload by declaring one
**required** positional parameter (`def f(mods)` / `lambda m: …`);
zero-arg handlers and default-arg lambdas (`lambda i=item: …`) keep
the bare call. `mods` is `{"shift": bool, "ctrl": bool}`.

`MpvtkApp.invalidate()` is thread-safe and wakes the loop — background
workers (thumbnails, downloads, playback timers) repaint through it.

Scroll offsets are also mirrored into the `user-data/mpvtk/scroll`
property on every change; `MpvtkApp.scroll_offsets()` reads it
synchronously, so a build() can window virtualized content against the
renderer's LIVE offset instead of trailing the throttled scroll event
(mpv ≥ 0.36; returns `{}` on older builds).

## 5. Images: strips, files, memory

- Rasterize at display size with Pillow; `rawimage.bgra_bytes` /
  `write_bgra` produce premultiplied BGRA.
- **Never let a crop exceed the source pixels.** Layout refuses to
  stretch images; the renderer clamps crops to iw/ih. Keep it that way.
  The failure mode is version-dependent, and the clamp is required on
  all of them: on the **`&<address>` memory path (libmpv, every mpv
  version)** overlay-add `memcpy_pic`s from the pointer with no bounds
  check → a hard **SIGSEGV**; on the **file path with mpv ≤ 0.41** the
  file is `mmap`'d 0→`offset+h*stride`, so a past-EOF read is a silent
  **SIGBUS** (and the map grows with the crop offset, a real cost for
  far-scrolled strips); on the **file path with mpv ≥ 0.42** the source
  is `fseek`+`fread` (no mmap), so a past-EOF read degrades to a soft
  `overlay-add: could not open or read` failure and the offset cost
  disappears. The memory path is the unforgiving one — the clamp is
  load-bearing there on every build.
- **Strips**: composite whole tile rows into ONE image (captions,
  badges, progress baked in) and declare tile hit-regions via ImageMap.
  This is what makes tiles scale: a screenful is 2–8 overlays (budget
  is 63), decorations dodge the z-order constraint (§6), and scrolling
  is pure crop math on cached files. Content-key the strips (see
  `demo.StripStore`): decoration changes produce a new key/filename, so
  stale renderer caches are impossible. LRU-bound the store.
- **libmpv backend** (`app.in_process`): pass images as same-process
  memory — `rawimage.MemoryStore` holds ctypes buffers, src is
  `"&<address>"`, the renderer folds crop offsets into the address.
  No files, no fs on mpv's command path. Buffers must outlive
  referencing scenes: LRU recency covers visible strips; frees go
  through a small graveyard for in-flight re-issues.
- **jsonipc backend**: files in `rawimage.cache_dir()` (RAM-backed dirs
  preferred on POSIX; `FILE_ATTRIBUTE_TEMPORARY` on Windows keeps the
  lazy writer from flushing scratch files).

## 6. Constraints that shape designs

1. **overlay-add bitmaps composite ABOVE all script ASS** (verified;
   the thumbfast hole-punch comment suggests the opposite). ASS can
   never draw on top of an image. Therefore: bake decorations into
   strips; hover rings draw outside image bounds; floating layers
   (popups/menus/dialogs/toasts) *occlude* images — their rect is
   subtracted from image overlays (≤4 sub-rects per image). A
   translucent scrim cannot dim posters — dialogs don't dim.
   Two escape hatches exist for in-flow content (`Stack`):
   **bitmap-over-bitmap works** — mpv composites overlay slots in
   ascending id order and the renderer keeps slot order consistent
   with paint order (sticky slots; a one-time renumber when an
   overlapping pair contradicts it), so a later Image child draws
   above an earlier one; and an ASS child marked `occlude=True` is
   subtracted from image siblings *below it* and draws in the hole
   (give it an opaque bg — the hole reveals the window background).
2. Overlay flush is hole-free by construction: adds/replacements are
   issued before removes and new images take over departing slots
   (slots are sticky per node id — don't regress this; index-shifted
   slots and remove-before-add both showed as scroll flicker).
3. Text metrics: measured per-char advances (ASCII) shared by layout
   and renderer + `\fn` for the same font. **libass scales `\fs` to the
   font's ascender+descender height, not the em** (VSFilter compat) —
   metrics.py folds the correction factor (em/(asc+desc), ≈0.859 for
   DejaVu Sans) into the table; `calibrate.py` verifies pixel-wise
   (ratios ~1.00). Without the factor, widths run ~16% wide and
   click/selection lands on the wrong letter. **Pair kerning** is also
   measured (`getlength(ab) - a - b`, ~220 non-zero ASCII pairs for
   DejaVu, e.g. "Ta" = -0.14em) and applied in every width/boundary
   path — advances alone drift badly on strings like "TaTaTa".
   Caret/selection boundaries include the kern INTO the next glyph
   (that's where libass puts its origin). Non-ASCII falls back to a
   heuristic table (`layout.py` + `renderer.lua`, keep in sync).
4. Text input arrives through a single `any_unicode` complex binding
   (`e.key_text`) — the FULL unicode range, not just ASCII. Editing is
   UTF-8 aware: cursors are byte offsets kept on codepoint boundaries
   (u8_prev/u8_next); BS/DEL/arrows step whole codepoints. IME status
   by platform: **Wayland** — mpv ≥0.40 supports text-input-v3
   (`--wayland-ime=yes` default); committed strings become key presses
   that land in any_unicode; preedit is NOT forwarded (no inline
   composition display; the popup sits at the window's top-left).
   **Windows** — mpv handles WM_IME natively (enabled by default);
   committed text arrives as unicode key events. **X11** — no
   IME/XIM: keyboard-layout characters (accented Latin via xkb) work,
   composition-based input (CJK) does not. Clipboard needs mpv ≥0.40.
   Textboxes support the full editing key set — click-drag selection,
   double-click word select, triple-click select-all (synthesized:
   plain click ≤0.4s after a double), shift+arrows, ctrl+arrows (word
   jump), ctrl+shift+arrows (word select), ctrl+BS/DEL (word delete),
   ctrl+A/C/X/V, ctrl+HOME/END, replace-on-type — plus a built-in
   right-click Cut/Copy/Paste/Select All menu (masked boxes offer
   Paste/Select All only — no clipboard leaks). The caret is an INLINE
   zero-width ASS drawing spliced into the text at the cursor — libass
   positions it at the exact pen boundary, so width math is only
   needed for click mapping. Three hard-won rules: inline drawing y
   origin is the line's ASCENT TOP (not baseline); the drawing must
   stay spliced during blink-off (alpha toggle) or the line bbox
   change bobs the text ~1px; and the run split drops the kern of the
   surrounding pair — restored via negative \\fsp on the prefix's last
   char using the measured kern amount. Metrics: ASCII + Latin-1 are
   bulk-measured at startup (~45ms fast machine, disk-cached to ~6KB
   JSON so warm starts read in ~0.5ms; stack is Pillow → raqm/HarfBuzz
   → FreeType, layout only, no rasterization); everything else is
   measured ON DEMAND as it appears in scene text or typed input
   (extend_metrics — the unicode pair space can't be pre-enumerated),
   scoped to scripts the base font covers (< U+2E80). CJK keeps the
   ~1em heuristic deliberately: libass renders it with a fallback font
   that Pillow isn't measuring, and fallback CJK glyphs are ~1em.
5. Wheel targeting walks the scroll chain by axis and holds a 2s
   gesture lock on its target (raw hit-tests can drop out — cause
   still unconfirmed; F12 HUD shows `tgt:<id>*` when the lock saves a
   gesture).
6. mpv options that matter: `keepaspect-window=no` (free resizing),
   `osc=no`, `cursor-autohide=no`; `background-color` is the app
   background (don't paint full-screen ASS rects — they'd sit under
   images anyway).

## 7. Testing

- `python3 -m jellyfin_mpv_shim.mpvtk [--backend libmpv]` — demo with
  Browse / Widgets / Logs pages exercising every widget.
- `--selftest DIR` (headless: `xvfb-run -a …`) — ~60 checks driving the
  renderer's `mpvtk-debug` hooks: `hover`/`click`/`rclick` by node id
  (`click` takes `shift`/`ctrl`), `down`/`up` (separate press/release —
  hold-repeat), `wheel` (id/dir/steps/axis), `text`, `key` (incl.
  CTRLA/SLEFT…), `popup`/`menu` (item index), `nav` (spatial
  navigation: `dir=`, `action=enter`, or `id=` to focus directly),
  `state` (renderer state dump incl. `scroll` offsets, the `ov`
  overlay-slot map, and `nav` focus).
  Screenshots per step (mpv can't screenshot without video — falls
  back to X11 `import`).
- `tests/test_mpvtk_layout.py` — layout engine unit tests (stdlib).
- `python3 -m jellyfin_mpv_shim.mpvtk.calibrate` (under xvfb) —
  renders text with markers at predicted widths, screenshots, and
  prints actual/predicted ratios. Run it when changing fonts or
  metrics; healthy output is ~1.00 per row.
- **F12** toggles the input-diagnostics HUD (wheel count/scale/target,
  mouse state). INFO logs time strip composition and render pushes.

## 8. The real browser (`mpvtk_browser/`)

The toolkit here is app-agnostic; `jellyfin_mpv_shim.mpvtk_browser` is
the application built on it — the Jellyfin library browser, rendered in
the player's own mpv window. How it lands against the model above:

- **Data layer is UI-agnostic and owned by the app.**
  `mpvtk_browser/repository.py` (live `JellyfinClient`) and
  `thumbnails.py` are the single source of truth; the strip compositor
  points at real posters decoded on a worker pool and recomposites on
  arrival via content keys + `invalidate()`.
- **Views are `build()` branches on a route stack** (`views.py` — home,
  grid, detail, series/season, search; state lives in the route dict,
  every mutation ends in `invalidate()`).
- **Same process as the player.** It lives near `playerManager`, not in
  the gui_mgr `multiprocessing` side — so there is no separate child and
  the libmpv memory-image path (§5) is available. `--idle
  --force-window` gives browse-before-play in the same window.
- **Spatial (10ft) navigation** (§2) is the net-new capability this
  architecture unlocks and the main reason to render in mpv for the
  remote/keyboard case.

The browser replaced an earlier Tkinter browser package (removed on
reaching parity). Note: `mpvtk_browser/__init__.py` still cites a
`MIGRATION.md` that does not exist — a leftover pointer, not a doc.
