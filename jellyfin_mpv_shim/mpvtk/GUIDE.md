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
bg/radius/border, on_click/on_dbl, hover, `tip=` tooltip,
`window_drag=True` — this box IS the title bar: pressing it drags
the window and double-clicking it toggles maximized, both handled
renderer-side because a drag must start on the press; it also
forces a hit rect, since the box may have no fill of its own),
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

**`disabled=True`** is a control that is on screen but cannot be used
right now — a setting the current mode ignores. The node draws muted
(`on_surface_faint` over the control's own surface, geometry unchanged
so the form does not reflow) and is inert: no hover, no click, no
spatial-nav focus. It still **absorbs** the pointer, so the press stops
there instead of reaching whatever it sits over — `node_at` keeps
returning it and each consumer drops it, which is not the same as making
it invisible. `Dropdown`/`TextBox`/`Slider` are drawn by the renderer and
mute themselves; `Button`/`Checkbox` are composites whose colours are
baked into child nodes the renderer cannot recognise, so they mute in
`widgets.py` and drop their handler. Always say *why* next to it: a
disabled control with no explanation reads as a broken one.

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

**MENU opens the focused node's context menu** — the keyboard's
right-click, and the only way a tile's actions (Play, Queue, Watched,
Favorite, Download) are reachable from ten feet away. A remote's
hamburger arrives as this key, like every other remote nav command. It
is anchored *below* the node, never over it: the menu is about that
node. A node with no `ctx` is a no-op, as right-clicking one is, and so
is an unfocused scene — with nothing selected there is nothing for the
menu to be about, and choosing a node on the user's behalf would be a
different gesture from the one they made.

**The mouse's back button is ESC.** `mbtn_back` sits in the mouse
group and its handler is a synthetic `keypress ESC`, not a ladder of
its own: ESC already steps out exactly one layer (slider scrub →
dropdown → menu → modal → the playback HUD, with "one page off the
nav stack" as the base case in Python, on the player's ESC binding),
and those bindings come and go with what is on screen, so a second
implementation would go stale on the first layer anyone adds. Being
in the *group* is what scopes it: the group is suspended during
playback, which leaves mpv's own weak `MBTN_BACK` (playlist-prev →
previous queue item) in force there.

Its pair has no key to ride on — nothing in mpv or the app means
"forward" — so `mbtn_forward` is the `forward` **event** instead, and
the app decides what history it has. Windowless like `nav`/`hud`
rather than addressed to a node: history belongs to the app, not to
whatever the pointer happens to be over. An app that registers no
`on_forward` ignores it.

## 3. Scene protocol (Python → Lua)

`script-message mpvtk-scene <json>`:
`{"v":1, "w":W, "h":H, "nodes":[...]}` — flat, paint-ordered.
Common fields: `t`, `id`, `x/y/w/h` (absolute OSD px), `sc` (owning
scroll container id), `top` (floating layer), `mod` (modal layer).

| t | extra fields |
|---|---|
| rect | fill, a, radius, bc/bw, click, ctx, rpt (hold-repeat), hover{fill,bc,c}, ring, nnav (clickable but outside the focus order — an ImageMap `zone`), wdrag (title bar: press drags the window, dbl toggles maximized — renderer-side, no event) |
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
pushed once at ready), `mpvtk-focus` (below), `mpvtk-keys` (below),
`mpvtk-debug` (test hooks, §10).

`mpvtk-keys {"keys": ["LEFT", …]}` — the app CLAIMS those mpv keys for
as long as it keeps the claim; each arrives back as a `key` event
instead of doing what it normally does. For a page whose gesture is
neither "move focus" nor "scroll" and so cannot be a widget: the epub
reader, whose content is one bitmap and whose LEFT/RIGHT mean *turn
the page*. Precedence is the renderer's and matches `key_scroll`'s — a
focused textbox, an open dropdown or menu, and any modal all take the
key first, so a page may claim LEFT without breaking the search box
above it. A claim is dropped by sending an empty list, and the
renderer drops every claim itself on `mpvtk-active no` (there those
keys are the player's seek keys, and the player outranks the UI).
`MpvtkApp.claim_keys()` is the Python side; the browser drives it from
a page's `claimed_keys` attribute, so leaving the page gives them
back.

`mpvtk-focus {"id": …}` puts spatial-nav focus on a node — a textbox
also takes the keyboard, because asking for the search box means asking
to type in it. With **no** id it means "whatever the next scene marks
`af`" (`autofocus=True` on any element), which is how a page opened by
remote lands on its Play button. Either form is **parked** until the
node appears, since a page is a spinner before it is a page; any user
input (arrows, Tab, a click) drops the request, and an `af` request is
dropped outright once the pointer is driving. `af` is also what a
key-summoned playback HUD focuses (§9).

## 4. Events (Lua → Python)

`script-message mpvtk-event <json>`; `app.py` dispatches to the
handlers registered during layout:

| t | payload | fires |
|---|---|---|
| ready / resize | w, h | osd size known/changed |
| click | id, shift?, ctrl? | press+release on same target (`rpt` nodes: on press, refiring while held) |
| dbl | id | double-click on a node with on_dbl (after its two clicks) |
| nav | active | keyboard/remote navigation engaged / mouse took over (`MpvtkApp.on_nav`) |
| forward | — | the mouse's forward button, while the UI owns the pointer (`MpvtkApp.on_forward`) |
| key | key | a key claimed via `mpvtk-keys`, when nothing on screen outranks the claim (`MpvtkApp.on_key`) |
| gpseek | dir | a game controller's seek gesture, "up"/"down"/"left"/"right" (`MpvtkApp.on_gamepad_seek`) |
| gpnav | a | a game controller button whose meaning differs between the library and a playing video, as a remote-control action name (`MpvtkApp.on_gamepad_nav`) |
| vpan / vzoom | (no id) | a wheel notch that ran off the end of a panned picture, and ctrl+wheel over one (`MpvtkApp.on_picture_gesture`, called as `(t, evt)`) |
| context | id, x, y | right-click on a node with on_context |
| change | id, value | textbox keystrokes; slider (throttled) |
| submit | id, value | textbox ENTER |
| select | id, index, value | dropdown or menu item chosen |
| dismiss | id | menu/dialog click-away or ESC |
| scroll | id, offset, max | watched scrolls, ≤ every 150ms |
| clipboard | op, need | a textbox copy/paste found no clipboard at all (`MpvtkApp.on_clipboard_error`); once per renderer |
| debug_state | … | reply to the `state` debug hook |

The last four are **windowless** — no node id, delivered to the app
rather than to the handler registry, because none of them is about
something in the scene. History belongs to the app (`forward`), and a
panned picture is mpv's video, not a node (`vpan`/`vzoom`; see §9). The
pad's **UI buttons are NOT routed through the gamepad hooks** — those are
synthetic keypresses the renderer issues locally, so a d-pad held down
does not queue a round trip per repeat. `gpseek` and `gpnav` are the two
a keypress cannot express: the arrows' seek is the user's own `input.conf`
distance and has to be SyncPlay-aware, and a nav button's meaning depends
on what is on screen. Both run on the loop thread.

Click handlers opt into the modifier payload by declaring one
**required** positional parameter (`def f(mods)` / `lambda m: …`);
zero-arg handlers and default-arg lambdas (`lambda i=item: …`) keep
the bare call. `mods` is `{"shift": bool, "ctrl": bool}`.

`MpvtkApp.invalidate()` is thread-safe and wakes the loop — background
workers (thumbnails, downloads, playback timers) repaint through it.

Scroll offsets are also mirrored into the `user-data/mpvtk/scroll`
property on every change; `MpvtkApp.scroll_offsets()` reads it
synchronously, so a build() can window virtualized content against the
renderer's LIVE offset instead of trailing the throttled scroll event.
`{}` and `None` mean different things and callers rely on it: `{}` is an
*answer* (the renderer is there, nothing is scrolled), `None` means it could
not be asked at all (mpv < 0.36 has no `user-data`), so a caller keeping its
own copy of scroll positions must fall back to that. Conflating the two makes
the fallback outvote the renderer — see `MpvtkApp.scroll_offsets`.

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
   `occlude` suits chrome that is *meant* to cover what is under it —
   popups, dropdowns, dialogs. It is the **wrong tool for a control
   that should look like it floats ON artwork**: the punched rect is
   hard-edged and opaque by necessity, so the control reads as a notch
   cut out of the picture, and it can be neither translucent nor
   non-rectangular. Make that control an `Image` instead — it carries
   `on_click`/`repeat` like a Box — and let it alpha-blend; see
   `jellyfin_mpv_shim/mpvtk_browser/tile_renderer.py`'s
   `_arrow_bitmap`.
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

## 7. Tokens and the type scale (`jellyfin_mpv_shim/mpvtk/theme.py`)

Colours and sizes are **tokens**, not literals. An embedding app calls
`theme.set_tokens()` once at startup — or `set_accent()`, still the right
entry point for an app whose only opinion is its brand colour — and every
widget and renderer-drawn control follows. Replacement is wholesale, which
is what makes a *runtime* theme swap safe: a token the new theme does not
mention resets to stock instead of keeping the old theme's value. Tokens
are served through the module's `__getattr__` rather than being module
globals, so a theme can never redefine the functions in there and an
unknown name raises instead of silently resolving.

Two rules shape the token set:

- **Semantic, not literal.** `ON_SURFACE_MUTED`, not `grey_aaaaaa`. There
  are deliberately fewer tokens than there were literals; four
  near-identical greys collapsing into one is the point, because a theme
  author cannot reason about seventeen shades and will not try.
- **Two surfaces.** Everything named `*_SURFACE*` is chrome drawn on the
  app's own background and follows the theme. The `SCRIM_*` / `CHIP_*`
  tokens are for things drawn over **video** — the playback HUD, the Skip
  Intro chip, the cast backdrop — and stay dark whatever the theme does,
  because a white HUD over a dark film is wrong no matter how light the
  rest of the app is. jellyfin-web keeps its player OSD dark for the same
  reason.

`ACCENT_ON_VIDEO` (the seek bar's fill, its chapter marks) defaults to
`ACCENT`, because one accent everywhere is what makes a theme read as a
theme — but it is **separable**, because the two jobs differ: over app
chrome the accent only has to beat a known background, over video it has
to stay visible against an unknown and moving one. This is a deliberate
divergence from jellyfin-web, which hardcodes its player slider to
Jellyfin blue and lets no theme near it.

### The type scale

**Pass a tier name, not a number.** `Text("…", size="caption")` — every
widget's `size=` goes through `theme.text_size`, which takes a tier name
or a number. The name is the preferred spelling at a call site: an author
can tell whether a line is a caption and cannot tell whether it is 13 or
14. A number still works, for the genuine one-off no tier describes. An
unknown tier is an error, not a silent default — a typo'd tier would
otherwise render as body text and look almost right.

| tier | × base | what it is |
|---|---|---|
| MICRO | 0.70 | guide badges ("HD"), the densest chrome |
| CAPTION | 0.80 | help text under a settings row |
| SMALL | 0.88 | dense body: guide, music, comic, reader |
| NORMAL | 1.00 | body text, and every control label |
| LARGE | 1.12 | meta and secondary lines on a detail page |
| TITLE | 1.30 | dialog titles |
| HEADING | 1.42 | carousel section headings |
| PAGE | 1.53 | page titles |
| HERO | 1.70 | onboarding: "Connect to Jellyfin" |

The base is **17 logical px**, and the ratios are surveyed rather than
invented: every explicit `size=` in the app was counted (237 call sites)
and grouped by what the text actually is, and with a base of 17 these
ratios reproduce 236 of those 237 to within a pixel. `HEADING` landing on
24 — what `heading_size` has always been — is the check that the ratios
are real rather than fitted. The tiers are named for the JOB, like the
colour tokens, and for the same reason: 161 of the 237 sizes sat in the
3px band 14–18 with no rule saying which.

`theme.set_type_scale(base, minimum, factor)` sets all three; `None`
restores stock, wholesale like `set_tokens` and for the same reason.
`factor` is the user's text multiplier and applies to **every** size the
toolkit resolves, tiers and explicit numbers alike — it is not folded into
the base, because that would scale the tiers and leave every call site
still passing a literal exactly where it was, recreating the mismatch the
scale exists to fix in the other direction.

**The multiplier is applied exactly once.** `theme.size()` / `text_size()`
return a `Px` — an `int` subclass meaning "already resolved" — and
`text_size(Px)` returns it untouched. Composite widgets resolve their own
size and hand the number to a child (`Button` builds a `Text` and an
`Icon`, `Checkbox` builds a `Text`, `Form` builds a `Grid` which builds a
`Text` per cell), and without this each of those resolved again: at 150% a
button label came out 39px against body copy at 20. Arithmetic on a `Px`
returns a plain `int`, which is the right default — `int(size * 0.95)` is
a NEW size derived from this one, and deriving is not resolving.

**The accessibility control is a floor, not a scale.** A scale multiplies
everything, so the smallest text stays the smallest text and a guide badge
at 0.70× base is still the hardest thing on screen to read. A floor
compresses the bottom of the scale instead of moving all of it, which is
what somebody who cannot read 12px actually wants. It is capped at the
**largest tier**, not at the base: capping at the base swallowed every
useful value (with a base of 17, a floor of 18 came out as 17, so the
setting appeared to do nothing at all to buttons or body text — exactly
what it is for). So a floor set above the largest tier caps *there*, and
flattens the whole scale into one size.

Two rules for anyone writing a widget or a view:

- **Sizes and colours are resolved in the constructor BODY, never as
  default arguments.** A default is evaluated once at import, and the type
  scale and palette are set by the app at startup and again on every theme
  swap. Every `x = theme.Y if x is None else …` line in
  `jellyfin_mpv_shim/mpvtk/widgets.py` is that rule, not an oversight.
- **An explicit `size=` on a widget is GEOMETRY, not type**, and does not
  take the text multiplier: `Icon(size=…)`, `Dropdown(trigger_icon=…)`'s
  button box. Scaling the whole interface — controls, artwork, spacing —
  is what `ui_scale` does, and having the text multiplier resize controls
  too would make it a second, partial copy of that. An icon with *no*
  size still resolves to a tier, because one with no opinion is standing
  in for a line of text and should match it.

## 8. Logical vs physical pixels (`jellyfin_mpv_shim/mpvtk/scaling.py`)

**Every number in Python view code is logical.** Views author at 1×,
`layout()` runs in logical space, and `scale_scene()` converts the
finished scene to physical on the way out. `app` hands `build()` a
*logical* size, which keeps derived math (the HUD's responsive sizing,
anything computed off the surface width) logical automatically instead of
double-scaling. The only code that thinks in physical pixels is bitmap
rasterization — because the renderer never resamples (§5) — plus the Lua
side itself. The factor is resolved once at startup (on `ready`) and is
not reactive: changing it needs a restart, because rescaling live would
mean dropping every cached bitmap.

`px()` is **the** rounding rule, shared by layout and every rasterizer;
never inline it. If the two ever rounded differently — 150×1.5 to 225 on
one side and 224 on the other — the mismatch lands in overlay-add's stride
and shears the image. Its companions: `dip()` back the other way (mouse
positions, surface size), `raster(w, h)` for a producer about to rasterize
a logical box, `logical_size()` for the surface (deliberately float —
truncating there loses up to a pixel of usable width per axis).

**Font sizes scale exactly; line boxes round.** `size` is scaled and *not*
rounded: nothing is rasterized at it, no stride depends on it, and libass
takes a fractional `\fs` happily. What does depend on it is that the text
comes out the width layout wrapped and ellipsized it to. At 0.75×, an 18px
run rounded to 14 is 18.67 logical — every line renders 3.7% wider than
the width it was fitted to, so a full-width paragraph overruns its column
and disappears under the scrollbar. The error is worst at fractional
scales (0 at 1× and 2×), which is why it only ever showed up on a scaled
display. The one thing that must round exactly like every other rasterizer
is the LINE BOX, and that still does.

Images are the leak in the abstraction — a decoded bitmap is physical — so
they get an explicit boundary. `Image`/`ImageMap` take `iw`/`ih` (physical
bitmap size) and `w`/`h` (the logical footprint), and check the two agree
through `raster()`. Declaring one axis and not the other is an error.

- **Only declare `w`/`h` on a canvas you sized yourself** — a composited
  strip, the cast backdrop, a banner. Decoded artwork must NOT: the server
  preserves aspect, so a square request comes back 56×52 and the footprint
  is whatever the bitmap turned out to be. What has to be scaled for those
  is the *request*, which `Image` cannot see. Getting it backwards asks
  the server for artwork at scale-squared on any HiDPI display, and caches
  it under a key the drawn size never matches.

Two traps in `scale_scene` itself:

- **`"rh"` vs `"ih"`.** A menu's row height is a logical row height and
  must scale; an `img` node's `ih` is the physical bitmap height and must
  not. One key meaning both is how the menu ended up drawing 1× rows under
  2× text. The scale tables are keyed on name alone, which is only safe
  while a key means the same thing on every node type — check before you
  add a field. (Full version inline in `jellyfin_mpv_shim/mpvtk/layout.py`.)
- **Hover dicts are frequently shared module constants** (layout's region
  default, theme-ish literals in the settings pages). `scale_scene` copies
  before scaling the pixel keys inside one; scaling in place would
  compound on it every single frame.

## 9. Playback HUD mode (`MpvtkApp.set_hud`)

`set_hud(on, opts)` enters and leaves the playback-HUD lifecycle, which is
**attached-but-idle with a blank scene**. That is the distinction that
matters: `set_active(False)` gets the renderer entirely out of the way for
another OSC — it unbinds its forced mouse/wheel sections, and pushing an
empty scene is *not* enough, because the bindings are what swallow the
clicks. HUD mode keeps the renderer attached during playback with only a
lightweight summon surface bound (the wake key + mouse motion). Summoning
rebinds the full input sections and fires `on_hud(True)`; the inactivity
timer drops back to idle with `on_hud(False)`. `set_active` in either
direction also leaves HUD mode. A summoned HUD lands its focus on whatever
the scene marks `af` (§3); `MpvtkApp.summon_hud()` wakes an idle one as if
a nav key were pressed, with no pause toggle.

`opts` is everything the renderer owns about the HUD, and is re-sent on
every engage — which is what lets a settings change stick without a
restart:

| key | meaning |
|---|---|
| `grab` | keyboard policy: summon on all arrows/ENTER while idle |
| `key` | otherwise only this mpv key is taken over (ENTER also pause-toggles on wake) |
| `hide` | auto-hide delay in seconds; absent means the toolkit's own default (`PHUD_HIDE.def`), and `0` is not absent — it forces "hover" |
| `mode` | auto-hide policy: `"hover"` / `"always"` / `"paused"` |
| `shadow` | draw the glyphs with their own dark halo, for the app that paints no scrim behind them |

`MpvtkApp.set_hud_skip(label)` tells the renderer whether a skippable
segment is live (falsy = none). While the HUD is idle, entering one shows
a standalone renderer-drawn skip button for a few seconds (pointer
movement re-shows it) and ENTER / remote Select / a click fires
`on_hud_skip`; while summoned, the scene's own button is authoritative and
this only tracks the label.

**Panned pictures.** `MpvtkApp.set_picture_pan(config)` hands the renderer
the gesture model for a picture mpv is displaying — `{"unitx", "unity",
"minx", "maxx", "miny", "maxy", "step"}`, or `None` to stop. While it is
set, a drag over the empty part of the window and the wheel move mpv's
`video-pan-x/y` **in the renderer**, with no round trip: a page turn is
one message, but a scroll is sixty a second and the app has nothing to add
to one. The units are the **displayed picture's** pixel size, not the
window's, because that is what mpv's pan is measured in (the measurement
is in `jellyfin_mpv_shim/mpvtk_browser/gateway/picture.py`). The clamp
comes from the app, because it depends on the page's size and the reader's
own chrome, neither of which the renderer knows; re-send it whenever
either moves. What comes back is the `vpan` / `vzoom` events (§4).

Both the key claim (§3) and the pan model are compare-and-skip caches, and
the renderer drops both of its own accord when it goes inactive without
saying so — `set_active` forgets them for that reason.

## 10. Testing

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

## 11. The real browser (`mpvtk_browser/`)

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
reaching parity). The migration's own write-ups (`MIGRATION.md`,
`PARITY.md`) were deleted in c37bfc3e and this section is what survives of
them; the comments that cited those files were cleaned up on 2026-08-07, so
a fresh citation of either is a dead pointer rather than a doc you have not
found.
