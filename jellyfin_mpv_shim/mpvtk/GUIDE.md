# mpvtk — developer guide

A declarative UI toolkit that renders inside the mpv window. Python
owns application state and layout; a Lua engine inside mpv owns all
per-frame interaction. This document is the durable context for anyone
(including future us) building on it. Companion docs: `README.md`
(spike history + findings), `PARITY.md` (Tk-browser gap analysis).

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

Layout: `Box` (direction/pad/gap/align, bg/radius/border, on_click,
hover), `Row`/`Column` sugar, `Spacer` (flexes unless given w/h — a
sized Spacer is the stand-in for virtualized content).

Content: `Text` (size/color/bold/align; ellipsized to fit),
`Image` (pre-rasterized BGRA; never scaled or stretched — see §5),
`ImageMap` (one composited bitmap + interactive sub-regions; THE tile
primitive, see §5), `Button` (Box+Text sugar), `Checkbox` (Row sugar).

Inputs: `TextBox` (editing, paste, selection, `mask=True` for
passwords; `on_change`/`on_submit`), `Dropdown` (readonly picker,
`on_select`), `Slider` (`on_change`, throttled while dragging),
`Busy` (indeterminate spinner).

Containers: `HScroll`/`VScroll` (optional scrollbar; `on_scroll` for
windowed/infinite content — fires leading-edge-throttled every 150ms
during scrolling).

Floating: `Menu` (context menu at a point; `on_select`/`on_dismiss`),
`Dialog` (centered modal, grabs all input, ESC/click-away →
`on_dismiss`), `Float` (positioned toast/banner, no grab). All floating
content draws above everything and occludes image overlays.

Every element takes `id=`, `w=`, `h=`, `flex=`.

## 3. Scene protocol (Python → Lua)

`script-message mpvtk-scene <json>`:
`{"v":1, "w":W, "h":H, "nodes":[...]}` — flat, paint-ordered.
Common fields: `t`, `id`, `x/y/w/h` (absolute OSD px), `sc` (owning
scroll container id), `top` (floating layer), `mod` (modal layer).

| t | extra fields |
|---|---|
| rect | fill, a, radius, bc/bw, click, ctx, hover{fill,bc,c}, ring |
| text | text, size, c, bold, align, click, hover |
| img | src (path or `&addr`), iw, ih, v (cache-bust) |
| scroll | axis, cw/ch (content), bar, watch |
| textbox | text, ph, size, mask, force |
| dropdown | items, sel, size, force |
| slider | min, max, value, force |
| busy | — |
| menu | items, size, ih (floating; x/y absolute) |
| layer | kind: modal\|float (meta: bounds for grab/occlusion) |

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
| click | id | press+release on same target |
| context | id, x, y | right-click on a node with on_context |
| change | id, value | textbox keystrokes; slider (throttled) |
| submit | id, value | textbox ENTER |
| select | id, index, value | dropdown or menu item chosen |
| dismiss | id | menu/dialog click-away or ESC |
| scroll | id, offset, max | watched scrolls, ≤ every 150ms |
| debug_state | … | reply to the `state` debug hook |

`MpvtkApp.invalidate()` is thread-safe and wakes the loop — background
workers (thumbnails, downloads, playback timers) repaint through it.

## 5. Images: strips, files, memory

- Rasterize at display size with Pillow; `rawimage.bgra_bytes` /
  `write_bgra` produce premultiplied BGRA.
- **Never let a crop exceed the source pixels: mpv mmaps the file and
  reading past EOF is a silent SIGBUS crash of mpv.** Layout refuses to
  stretch images; the renderer clamps crops to iw/ih. Keep it that way.
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
   Paste/Select All only — no clipboard leaks). The caret is a thin
   bar centered on the char boundary. Metrics cover ASCII + Latin-1;
   unmeasured glyphs fall back to a fullwidth heuristic for CJK.
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
- `--selftest DIR` (headless: `xvfb-run -a …`) — 35 checks driving the
  renderer's `mpvtk-debug` hooks: `hover`/`click`/`rclick` by node id,
  `wheel` (id/dir/steps/axis), `text`, `key` (incl. CTRLA/SLEFT…),
  `popup`/`menu` (item index), `state` (renderer state dump).
  Screenshots per step (mpv can't screenshot without video — falls
  back to X11 `import`).
- `tests/test_mpvtk_layout.py` — layout engine unit tests (stdlib).
- `python3 -m jellyfin_mpv_shim.mpvtk.calibrate` (under xvfb) —
  renders text with markers at predicted widths, screenshots, and
  prints actual/predicted ratios. Run it when changing fonts or
  metrics; healthy output is ~1.00 per row.
- **F12** toggles the input-diagnostics HUD (wheel count/scale/target,
  mouse state). INFO logs time strip composition and render pushes.

## 8. Integrating the real browser (the road from here)

- `library_browser/repository.py` and `thumbnails.py` are UI-agnostic:
  point the strip compositor at real posters (decode on the existing
  worker pool; recomposite-on-arrival via content keys + invalidate).
- Views become `build()` branches on a route stack (see PARITY.md for
  the full component mapping and build order).
- The mpv-rendered browser lives near `playerManager` (same process as
  the player), not in the gui_mgr multiprocessing side; `--idle
  --force-window` gives browse-before-play in the same window.
- Remote/keyboard spatial navigation is the one net-new capability
  left, and the main reason to render in mpv for the 10-foot case.
