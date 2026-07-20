# mpvtk migration plan — replace the Tk browser (and display mirror)

Living checklist for porting the Tkinter library browser (and, as a
bonus, the display mirror) onto **mpvtk**, the in-mpv UI toolkit. This
is the execution doc; read `GUIDE.md` (framework), `PARITY.md`
(component gap analysis) and `README.md` (spike log) first.

> **Doing the cutover?** Skip to
> [Cutover checklist](#cutover-checklist--deleting-the-tk-browser) at the
> end — current state, accepted losses, and the deletion steps. The rest
> of this file is the historical execution log.
>
> **Editing `mpvtk_browser/`?** `app.py` is one mixin per feature area —
> its module docstring has the map, the three invariants (thread contract,
> epoch discipline, the unlocked `build()`), and how to add a view. The
> queued cleanups are all done; what they turned up is recorded at the end.

## Field-test round 1 (2026-07-19) — reported issues

First real session with `browser_ui=mpvtk`. All fixed unless noted.

- [x] **OSC never appeared during playback.** Pushing an empty scene does
  not yield: `renderer.lua`'s forced `mpvtk_mouse`/`mpvtk_wheel` sections
  keep eating mbtn/wheel. Added the `mpvtk-active` message
  (`MpvtkApp.set_active`) which unbinds them and blanks the scene; driven
  from `enter_browse`/`_yield` and from the mirror's `hide`/`show`.
- [x] **`shim-menu-select` spam.** `mouse.lua` bound `MOUSE_MOVE` at load,
  so every mouse move fired a script-message whether or not the OSD menu
  was open. Now bound only between `shim-menu-enable True`/`False`.
- [x] **Letterboxed grey background.** The browse background is a 16×16
  solid PNG; mpv letterboxed it. `keepaspect=no` while browsing;
  `browse_yield()` restores it before video.
- [x] **Browser opened fullscreen.** It reused `settings.fullscreen`. New
  `browser_fullscreen` key (default off); playback fullscreen unchanged.
- [x] **`q` closed mpv.** With the in-window browser the window *is* the
  library, so `kb_stop` → `stop_to_browser()`. CLOSE_WIN/STOP still quit.
- [x] **Tofu for Japanese text.** Text baked into bitmaps goes through
  Pillow, which does no font fallback (ASS/libass text was always fine).
  `mpvtk/pilfont.py` picks a face per string's script; used by tile
  captions and the mirror.
- [x] **Blank tiles after scrolling back.** A large library emitted a strip
  per row for *every* row, blowing past both the strip LRU (48) and mpv's
  63-overlay budget. Grid rows are now virtualized against the scroll
  offset (`_grid_of(scroll_id=…)`).
- [x] **No infinite scroll in music.** Every tab is paged on near-end
  scroll now (`_on_music_scroll`); it was capped at the first 100 items.
- [x] **Now-playing bar updated every 5s.** It rode the timeline thread's
  tick; it has its own 1s ticker while the bar is up.
- [x] **Lost button icons / no search button / no user switcher.**
  `Button(icon=…)`; search button beside the box; user dropdown in the
  chrome plus full user management in Settings.
- [x] **Carousel arrows should be pinned to the screen edges.** Home rows
  bleed full-width and the arrows sit flush left/right, hidden when the
  row doesn't overflow. They are *beside* the strip, not floating over it
  — see "Framework deficits" below for why.
- [x] **Settings was a flat schema dump.** Now tabs (General / Servers &
  Users / Downloads / Logs), curated sections + Advanced collapse,
  friendly labels, enum dropdowns, PIN setup, server removal, a downloads
  manager that can delete, a log view, and "Open Config Folder".
- [x] **Playlist editor had no multi-select and no table.** Toggle-select
  with block moves and bulk remove; #/Title/Type/Runtime columns.

## Framework deficits found while fixing the above

Real mpvtk limitations, not app bugs. **All 7 fixed in the toolkit
(2026-07-19)** — unit-tested in `tests/test_mpvtk_framework.py`,
renderer behavior exercised by new selftest checks (both backends).
The app-side adoption (carousel arrows over strips, shift-select in
the playlist editor, `_wrap`→`Text(wrap=True)`, hand-rolled columns →
`Table`, tighter virtualization via `scroll_offsets()`) is follow-up
work — the browser still uses the old patterns.

1. [x] **Bitmap z-order.** mpv composites overlay slots in ascending id
   order; `flush_overlays` now keeps slot order consistent with paint
   order (slots stay sticky; when an overlapping pair contradicts paint
   order it renumbers everything to paint index once, add-before-remove
   preserved). Bitmap-over-bitmap layering now just works. ASS still
   can't draw over a bitmap directly — but see the `occlude` marker
   under (2).
2. [x] **`Stack` container.** Children share the parent rect (per-child
   `anchor` nw/n/ne/w/c/e/sw/s/se or fill, plus `dx`/`dy`), scroll with
   the page, and paint in child order. An ASS child marked
   `occlude=True` emits an `occ` node the renderer subtracts from image
   siblings *below it* — so arrows/badges/chips can sit "over" a strip
   by punching through it (opaque bg required).
3. [x] **Synchronous scroll offsets.** The renderer mirrors offsets into
   `user-data/mpvtk/scroll` on every change; `MpvtkApp.scroll_offsets()`
   reads it synchronously at build() time (mpv ≥ 0.36 → `{}` fallback).
   `debug_state` already echoed `scroll`.
4. [x] **Click modifiers.** shift/ctrl mbtn variants are bound; the click
   payload carries `shift`/`ctrl`, and a handler opts in by declaring a
   required first parameter (`lambda m, i=i: …`); zero-arg and
   default-arg handlers keep the bare call. Debug hook: `click` takes
   `shift`/`ctrl`.
5. [x] **Hold-repeat.** `Button(repeat=True)` (any clickable Box, and
   ImageMap regions via `"repeat": True`) fires on press and refires
   while held (0.4s delay, 0.12s interval, pauses while the pointer
   leaves); the release is swallowed. Debug hooks `down`/`up` cover it.
6. [x] **`Text(wrap=True, max_lines=…)`.** Layout-side greedy word wrap
   (kern-aware, hard-breaks long words, `\n` = paragraph); one scene
   node per line; last kept line ellipsized. Callers' `_wrap()` helpers
   can be deleted at adoption.
7. [x] **`Table` primitive.** One column spec (`w`/`flex`/`align`)
   generates header + rows, so geometry can't drift; `selected` rows
   get a background; row `on_click` composes with (4) for shift-range /
   ctrl-toggle selection. The demo's hand-rolled track table now uses
   it.

## Framework deficits — round 2 (2026-07-19 layout audit)

From a two-sided audit: what layout machinery the Tk UI actually leans
on (grid column weights, pack side conventions, Treeview) vs. how the
mpvtk views approximate it today. The two suspected areas — tabular UI
and management-panel layout — are exactly where the friction clusters.
Ordered by impact; `(Tk)` cites what the legacy UI used, `(app)` where
the mpvtk view compensates today.

**Status (same day):** items 1–3, 5–7, 10–12 are FIXED in the toolkit
(unit tests in `tests/test_mpvtk_framework.py`, selftest checks both
backends): Table virtualization + per-row `fg`/`bg` + `dbl` events;
`justify=` on Box; `Grid`/`Form` shared column tracks; `pad=(x, y)`;
`Progress`; wrap-in-Row heights; `tip=` tooltips. Partial credit on 9:
`MpvtkApp.node_rect(id)` gives post-layout geometry feedback (one
frame stale); build-time overflow queries and a priority-collapse
container remain open. Open: 4 (tree rows), 8 (min/max constraints).
Bonus shipped with this round: music playlists get a leading album-art
thumbnail column in the track table (`_track_list(art=True)`,
`_art_cell` via the thumbnail pool + `strips.bitmap`).

**Adoption (same day):** the Downloads and Servers & Users panels now
build on `Grid` — which grew dict rows for the purpose (`{"cells",
"id", "bg", "radius", "on_click", …}` + `row_pad`: a full-width card
rect behind track-aligned cells). The magic fixed widths
(name 220 / active 90 / username 180 / status 120 / meta 200-as-Text)
are gone; button columns auto-size to the widest set, so translations
can't shear rows; downloads keep tree indentation inside the title
cell so meta/Remove tracks align across all three depths. Item 4
(tree rows) is now HALF-covered: Grid dict rows give downloads its
card/indent structure; disclosure/collapse remains open.

**Tabular**

1. [x] **Table rows aren't virtualized.** Every row is built eagerly on
   each repaint (app: `_track_list`, playlist editor, queue), which
   contradicts the windowing story the tile grids use — a
   several-hundred-track playlist re-lays every row per repaint. (Tk:
   Treeview virtualizes internally.) Want: a windowed Table (or Table
   over `scroll_offsets()` with Spacer stand-ins handled internally).
2. [x] **No per-row styling override in Table.** Only a `selected`
   bool; the queue's now-playing highlight is faked by merging it into
   `selected`, and status coloring (downloads: green watched / amber
   in-flight / red error text) has nowhere to go. (Tk: Treeview row
   tags.) Want: per-row `fg`/`bg` (and per-cell color) in the row dict.
3. [x] **No double-click event.** The renderer only synthesizes
   double-click for textboxes; Tk's queue jumped on double-click and
   AddTo activated on it. Want: `dbl` on clickable nodes (payload like
   click), then wire queue jump-to-item.
4. [ ] **No tree/hierarchical rows.** The downloads manager is a
   3-level tree; the app fakes indentation with `Spacer(w=depth*26)`
   rows, no disclosure/collapse, no header. (Tk: graduated pack
   padding + signature-diffed sections.) Want: either an `indent`/
   disclosure affordance on Table rows or a small Tree list composite;
   per-side margins (below) would at least make indent declarative.

**Panel / form layout**

5. [x] **No main-axis justification.** The single most-repeated hack:
   `Spacer()` sandwiches for centering login/locked/busy cards, footer
   button right-alignment in every dialog, and the A–Z bar needs a
   Box-per-glyph workaround (documented in-code at app.py's letter
   bar). Want: `justify="start|center|end|between"` on Box.
6. [x] **No shared column tracks outside Table.** Label+input forms
   (Settings `w=340`, Login/PIN `w=140`) and management list rows
   (Servers `w=220/180/120`, Downloads `w=200`) fake column alignment
   with magic fixed widths — the exact drift Table was built to kill,
   but Table only fits true header+cells tables. (Tk: grid
   `columnconfigure(1, weight=1)` and char-width labels.) Want: a
   `Form`/`Grid` container where sibling rows share column tracks
   (label track sized to widest label, value track flex), cells
   hosting arbitrary Elements.
7. [x] **`pad` is uniform on both axes.** Table itself fakes
   horizontal-only padding with `Spacer(w=pad_x, h=1)` margin cells;
   tree indent (above) is spacers. Want: `pad=(px, py)` at minimum,
   ideally per-side.
8. [x] **Min/max size constraints.** `min_w`/`max_w`/`min_h`/`max_h`
   on every element — px, or a float fraction of the available space
   (Dialog children resolve fractions against the window). Rows now
   flex-shrink on overflow (proportional, floored at min; bitmaps/
   icons floor at natural, Text re-ellipsizes); columns keep
   overflowing on purpose. The chrome server/user switchers use
   min/max instead of fixed widths, so long names count toward the
   collapse decision and get room when there is some.
9. [x] **Overflow/fit feedback.** `layout.natural_size(tree)` is the
   synchronous build-time probe: measure a candidate layout against
   the window and choose. The chrome bar now collapses to icons via
   the probe (labelled bar + the title's minimum room vs. window
   width) instead of the hardcoded `COMPACT_W = 1280` — sessions with
   fewer switchers keep labels narrower. `node_rect()` remains the
   one-frame-late path for laid-out geometry (virtualizer offsets).
10. [x] **No determinate progress widget.** Downloads rows show
    "Downloading 43%" as text; the settings folder-move progress is a
    status string. `Busy` (indeterminate) and `Slider` exist; a
    filled-Box `Progress(frac)` composite is trivial and recurs.
11. [x] **Wrapped Text only pre-sizes in Columns.** A `wrap=True` Text
    in a Row parent needs an explicit `w=`; callers still compute
    `w - 32` by hand for paragraphs. Want: rows to assign wrap width
    from the laid-out slot like columns do.
12. [x] **No tooltips.** Tk chrome had hover tooltips; icon-only
    buttons (and any future responsive collapse, #9) need them. The
    renderer already owns hover state — a `tip="…"` field drawing a
    delayed floating label is renderer-local work, no protocol change.

Cosmetic footnotes (not worth primitives yet): `None` children could
be tolerated instead of `Spacer(h=0)` placeholders; no baseline
alignment for mixed text sizes on a row (cross-center approximates).

App-side debt recorded while auditing (not framework): the Downloads
and Servers & Users panels predate `Table`/`Stack` and still hand-roll
magic-width rows; `_paragraph` callers pass hand-computed widths;
queue double-click and chrome tooltips/responsive collapse remain
dropped vs Tk (also listed in the shell gaps above).

## Field-test round 2 (2026-07-19) — reported issues

- [x] **Playlists showed "N items" and nothing below.** Scroll container
  ids are per-view ("grid", "playlist"), not per-route, so a deep scroll in
  one library carried into the next view opened under the same id. The
  renderer clamps its own offset to the new content; our copy didn't, so
  virtualization windowed rows past the end. Offsets now come from
  `MpvtkApp.scroll_offsets()` synchronously at build time and are cleared
  on navigation.
- [x] A-Z letters were packed against their left border (Box only centres
  on its *cross* axis — the glyph needs `align="center"` + `flex=1`).
- [x] No fullscreen for music: there's no picture, and it hid the library
  the now-playing bar belongs to.
- [x] Music playlists render as a track list; playlist contents are
  filtered to `PLAYLIST_SUPPORTED_TYPES`.
- [x] Playlist/queue selection follows normal semantics: shift-click picks
  a range from the anchor in two clicks, ctrl-click toggles, plain click
  replaces.
- [x] The queue is the same Table + toolbar as the playlist editor.
- [x] Stopping music closed the library — the bar's stop button called
  `stop_and_close()`, which drops force_window. It stops *to* the browser
  now, and a stopped playstate re-asserts the browse window regardless.
- [x] Downloads are a series > season > episode tree with a delete at
  every level; Servers & Users is two full-width cards.
- [x] **"The floating selection display gets cut off by library
  headings."** Found: the tile hover ring is ASS (`draw_rect`, not a
  bitmap) and the renderer draws it 2px *outside* the hit rect — then
  clips it to the scroll viewport. The strip filled its container exactly,
  so the ring's outer edge fell outside the clip and was shaved off; the
  heading above was just what you saw in the gap. Fixed app-side by
  insetting the strip inside its HScroll by `RING_PAD`, which is also why
  the arrows are now inset by the same amount.

### Framework adoption (Fable's round)

All seven deficits are fixed in the toolkit and adopted here:

- Carousel arrows genuinely float over the strip (`Stack` + `occlude`),
  with hold-repeat. The flush-gutter workaround is gone.
- All tabular lists come from one `Table` column spec, so header and cells
  can't drift — the reported misalignment was hand-laid Rows with fixed
  widths meeting variable-width text.
- Overviews use `Text(wrap=True)`; the hand-rolled greedy wrap is gone.
- Virtualization windows against the renderer's live scroll offsets.

Still unadopted: nothing blocking.

### Polish round

- Page arrows are square, smaller and glyph-centred (`Box` only centres on
  its cross axis; the glyph needs flex spacers), so the occlusion punch
  reads as a notch instead of a slab.
- Headings get their own spacing above action rows.
- The top bar drops button labels below `COMPACT_W` (1280 = half of
  1440p), where the labelled buttons used to overflow into the page title.
- Accent fills always carry white text (`theme.ACCENT_FG`); black on blue
  read as disabled. `_action_btn` now distinguishes `primary=` (call to
  action — Play, Next Up, Play All) from `on=` (a toggle that shares the
  accent fill — Watched, Favorite).
- Detail/series banners are 2/3 the height of the equivalent 16:9 box
  (~2.4:1) with the title and metadata **baked into the bitmap** over a
  bottom gradient, like the Tk browser. ASS text can't be drawn over a
  bitmap, and an occlude punch would show the window background rather
  than the artwork, so baking is the only way to get text *on* the image.
  The no-artwork path still draws a normal ASS heading.

## Field-test round 3 (2026-07-19)

- [x] **Window closed and reopened when quitting playback / stopping music.**
  Stopping hits `set_browse_window(True)` twice — once from the
  stopped-playstate callback, once from the caller — and reloading the
  background over itself tears the video output down and back up. The call
  is idempotent now (`_showing_browse_bg`).
- [x] **`keepaspect-window=no` now survives mpv re-creation** (set in
  `_init_mpv`, not only in `set_browse_window`). A fresh mpv defaults it to
  yes, so after an idle-quit the window snapped back to each file's aspect
  on every play.
- [x] Paragraph spacing: `_paragraph` splits on newlines and spaces the
  paragraphs; the layout engine only wraps *within* one.
- [x] Dialog buttons trail. A content-sized dialog gives a flex Spacer no
  leftover to absorb, so the Spacer-sandwich packed them left; the shell
  stretches its children and the button rows use `justify="end"`.
- [x] The startup PIN gate is a full page listing the other local users. A
  locked user could otherwise lock the whole client out.
- [x] Downloads: sizes were read from a nonexistent `size` key (the catalog
  stores `size_bytes`/`downloaded_bytes`, hence 0 B everywhere); playlists
  are their own collapsed group and own their items, so a downloaded music
  playlist no longer lists hundreds of tracks; the view polls while
  transfers are outstanding.
- [x] Dropdown labels are ellipsized to the control width by the app —
  see the framework note below.

### Framework requests

- [x] **Dropdown labels don't ellipsize.** Fixed in the renderer: a
  kern-aware `ellipsize` (mirror of layout.py's) truncates the closed
  dropdown label to `w - 40 - icon` and popup/menu items to
  `w - 20 - icon` — the exact insets the widget knows. `_fit_items()`
  and its guessed insets are deleted; dropdowns get full labels again
  (so `select` values are no longer pre-truncated strings).
- [x] **Tree disclosure.** The downloads tree collapses: group and
  season rows carry a chevron (app-side `Icon` + `on_click` — no new
  framework needed beyond Grid dict rows), state keyed by entry id in
  `route["_dl_collapsed"]` so it survives refreshes; rows without a
  chevron reserve its gutter so indentation stays monotonic.
  Playlists still rely on the controller not emitting children.
- **Per-item progress** in the downloads list needs `Progress`, which is
  ready — blocked on app-side live progress push, already logged below.

## Playback lifecycle (2026-07-19)

The window rule, stated by the user and now enforced in one place
(`PlayerManager._set_force_window`):

> force_window is true for everything except being cast to while the
> library browser is minimized and the display mirror is disabled.

`mpvtk_active` is the flag that encodes it — true while the in-window UI
owns the window, *including* while yielded to playback, and false only once
minimized. So while it is set, force_window can never be cleared. Every
path that used to clear it — `stop_and_close`, the OSC's `shim-close`,
`force_window(False)` from the OSD menu, `set_browse_window(False)` — now
goes through the guard, and the minimize path clears `mpvtk_active` first,
which is what lets it through.

- [x] Window closed on stopping music/video, on `q`, on the OSC's back
  button, and at the end of a queue. All the same cause as above.
- [x] Window *resized* when playback started. Two mpv properties do that
  and both default to yes: `keepaspect-window` (snaps to the file's aspect)
  and `auto-window-resize` (resizes to the video's pixel size). Both are
  set to no in `_init_mpv`, so they survive idle-quit re-creation.
- [x] Fullscreen toggles made by the *user* (key, OSC button, remote) are
  persisted — to `browser_fullscreen` while browsing, `fullscreen` while
  playing. Toggles the app makes for its own reasons (the update notice
  leaving fullscreen, the browser opening windowed) are not.

## Field-test round 4 (2026-07-19)

- [x] **The player UI came back after playback ended**, showing the
  finished video paused. Root cause: `finished_callback`'s end-of-queue
  branch sent the timeline stop but never cleared `_video`, so
  `is_active()` stayed true. Once the browser re-loaded its background
  image (which clears `playback-abort`), the next timeline tick reported
  the *finished* item as playing and the browser yielded again. The branch
  now drops `_video`, unloads the file and pushes a stopped playstate. The
  brief "Drop files to play here" before it was the same window sitting
  idle with no file, and goes away with it.
- [x] Deleting a download refreshes the list. The delete and the re-read
  were separate pool tasks and raced, so the row came straight back; they
  run in order on one worker now.
- [x] Persistent **download status bar** with progress and a "View
  Downloads" button — downloads were invisible once the confirm dialog
  closed. Polled, since the sync manager has no push hook.
- [x] **Startup update check.** The notice previously only appeared once
  playback had started, because that was the only thing driving the check.
- [x] **Add Server** supports Quick Connect, offers previously-used server
  addresses, and can be cancelled. `show_login` only resets the nav stack
  on a first run — with servers connected it pushes, so Back/Cancel
  returns to the library instead of trapping the user on the form.
- [x] Page arrows centre on the poster, not the poster+caption strip.

## Polish round 3 (2026-07-19)

- [x] **Paragraph spacing.** `_paragraph` used a fractional gap, which put
  the paragraph break *close to* the wrapped line spacing around it and
  read as a mistake. It's a full line height now, so a break is cleanly
  double the line gap. Also handles `\r\n`.
- [x] **Mismatched button heights.** The plain `Button` widget defaults to
  a 20px label; `_action_btn` uses 16, so a trailing plain Button in an
  action row ("Go to Series", "Edit", "To Series") was ~5px taller than its
  neighbours. `_action_btn` takes `icon=None` now and every button in an
  action row comes from it.
- [x] **One blue.** Selection, hover rings, progress and the update banner
  all used their own blue — the toolkit's `7aa2f7`, `Table`'s `2f4468` and
  an ad-hoc `2a3a5a` alongside the app's `00a4dc`. Everything the app
  colours now comes from `theme.ACCENT` / `ACCENT_HOVER` / `ACCENT_SOFT`,
  with a test that walks rendered scenes and fails on any blue outside that
  family.

### Themeable accent ✅

The toolkit used to hardcode `7aa2f7` as its own accent in six places the
app couldn't reach. `mpvtk/theme.py` now holds the palette:

    from jellyfin_mpv_shim.mpvtk import theme
    theme.set_accent("00a4dc")          # hover/soft/on-accent derived

- `widgets.py` — `Checkbox` fill and tick, `Progress` default fill,
  `Table` selected-row background resolve at construction, so anything
  built after `set_accent()` follows it. An explicit per-call colour still
  wins.
- `layout.py` — the `ImageMap` hover-ring default.
- `renderer.lua` — the focused-textbox border, open-dropdown border and
  slider fill read `state.accent`, pushed by the `mpvtk-theme` message on
  ready and from `MpvtkApp.set_accent()`.

`on_accent` is chosen by relative luminance, not a channel average: a
saturated blue is much darker than its mean suggests and needs white on it.

The browser calls `mpvtk_browser.theme.apply_to_toolkit()` in
`MpvtkBrowser.__init__` — before any widget is built — and its per-call
overrides are gone. `TestOneBlue` walks rendered scenes and fails on any
blue outside the accent family; `TestThemeAccent` covers the toolkit side.

## Remaining framework work (2026-07-19, verified against code)

Excluding Phase 8 (spatial/remote navigation).

**Open in the toolkit**

- **#8 min/max size constraints.** Nothing exists (`grep min_w` finds only
  the local wrap parameter). Dialogs are fixed 440–560px, banners clamp by
  hand (`min(w - 32, 1100)`), and fixed/natural children overflow silently
  — there is no flex-shrink. Wanted: `min_w`/`max_w` (+ heights) honoured by
  measure/arrange.
- **#9 overflow / fit feedback, half done.** `MpvtkApp.node_rect()` gives
  post-layout geometry one frame late, which covers stable things. Still
  hand-derived in the app: the carousel recomputes `content_w` to decide
  whether arrows are needed, and virtualized grids feed estimated header
  heights (`head_h = 40 + 110…`). The top bar no longer guesses — it
  measures a probe bar with `natural_size()` and collapses when the
  labelled version doesn't fit, which is the shape the other two want too.
- **#4 tree rows, half done.** `Grid` dict rows give card + indent, and the
  downloads tree gets disclosure from a plain Icon + `route` state. There
  is no tree primitive, but nothing currently needs one.

**Not toolkit bugs, but worth knowing**

- **CJK text metrics are heuristic.** `metrics.extend_metrics` deliberately
  stops at U+2E80 and `layout.char_w` assumes 1em for CJK, because libass
  uses fallback fonts Pillow isn't measuring. ASS-drawn CJK text therefore
  ellipsizes and wraps approximately. Baked bitmap text (tile captions,
  banners, the mirror) measures exactly via Pillow, so the visible impact
  is limited to chrome and list rows.
- **Scratch-dir cleanup is best-effort.** `cache_dir()` registers an
  `atexit` rmtree, which does not run on SIGKILL or a hard crash, so a
  dev box can still accumulate BGRA scratch dirs. A startup sweep of stale
  `mpvtk-*` dirs would close it.
- **No IME on X11** (mpv limitation; Wayland and Windows are fine).

**App-side debt this audit found**

- [x] Track tables were not virtualized. `Table` grew `virtual=` for
  exactly this and the app never passed it, so a long playlist built every
  row — and with the album-art column that is one mpv overlay per row,
  which would blow the 63-overlay budget outright rather than just cost a
  repaint. Now windowed against `scroll_offsets()` in every track view.
- [ ] Live per-item download progress push (the `Progress` widget is ready;
  the downloads view polls instead).

## List widths (2026-07-19)

Reported as "table rows change width randomly while scrolling a long
playlist" and "download listings are as wide as their longest label".
Same cause, and app-side:

A `Table`'s **natural width is whatever its materialized rows need**. In a
`Column` with the default `align="start"` a child gets its natural width,
clamped to the container — so:

- a *virtualized* table's natural width changes as the window of rows
  moves, i.e. the rows visibly resize while scrolling;
- an unvirtualized list is sized by its longest label instead of the pane.

Fixed by stretching every container that hosts a `Table`/`Grid`. Servers &
Users already looked right because it was written with `align="stretch"`.

**Framework note (small, for the toolkit):** a virtualized `Table` having a
content-dependent natural size is a trap — the width depends on the scroll
position, which no caller expects. Either measure a virtualized Table's
natural width across *all* rows' string cells (cheap: `text_width` over
strings, skipping Element cells, which are fixed-width in practice), or
have it declare that it must stretch. Right now the only defence is every
caller remembering `align="stretch"`.

## Remote control (2026-07-19)

The Jellyfin remote (phone / web client) now drives the in-window browser
end to end:

- **Arrows / Select** were wired by the framework round —
  `playerManager.menu_action` maps MoveUp/… onto the renderer's nav keys
  whenever the mpvtk UI owns input, falling through to `kb_seek` during
  video playback.
- **Back** completes it: `menu_back` asks `playerManager.on_nav_back`
  (→ `MpvtkBrowser.on_back`) first, which unwinds one layer at a time —
  dialog, then tile menu, then the nav stack — and *declines* at the root
  so ESC keeps its old meaning (leave fullscreen).
- **GoHome / GoToSettings** reach real pages. `GoToSettings` aliased to
  `"home"` in `NAVIGATION_DICT`, which predates the browser having a
  settings page; it is its own action now. Both go through
  `playerManager.on_nav_command`, and every other path (CLI, Tk, and the
  browser mid-playback) keeps the historical meaning of opening the OSD
  menu — that is the only settings surface those have.
- **DisplayContent** ("show me this") opens the item's page, routed through
  the same `_open_item` dispatch a click uses, so a series lands on the
  series page and the remote's arrows then drive it. It wakes a minimized
  client and raises the window.

  Two deliberate refusals, both because **jellyfin-web emits DisplayContent
  as you browse on the phone**, not only when you pick something:

  - It never *interrupts playback*. Browsing on the phone while something
    plays here would otherwise stop the video every time the remote view
    changed. The page is simply waiting when playback ends.
  - It never *starts playback*. A cast track opens its album (or is ignored
    if it has none), because `_open_item` would play it.

`display_mirroring` stays what it was: a **kiosk mode** kept for
backwards compatibility, where casting shows a static backdrop preview and
the mirror owns the window instead of the browser. The two remain mutually
exclusive — `DisplayContent` goes to the mirror when it is enabled and to
the browser otherwise.

## Parity audit gaps (2026-07-19 code-level Tk→mpvtk diff)

Found after the mechanical pass: the initial port rendered every view but
dropped many action rows, pickers, filter bars and tile-shape rules. The
repository/data layer already backs almost all of these — they're UI
wire-ups that were skipped. Status updated as each is fixed.

**Tiles / Home**
- [x] Per-row / per-view **tile shape** (poster 2:3 / landscape Thumb 16:9
  / square 1:1) + `image_type` (Primary/Thumb). Dropped globally — every
  tile is a portrait poster. Home classifies by `collection_type`; Season
  episodes, Search, Playlists, music all need shaping. (`WIDE_GEOM`
  exists but is unused.)
- [x] **Downloaded** indicator on tiles (top-right badge; `is_downloaded`).
- [x] Tile **placeholder glyph** (music-note for audio / first-initial).
- [x] Watched checkmark **Series/Season fallback** (UnplayedItemCount==0);
  tile currently disagrees with its own menu.
- [x] **HScrollRow ◀ ▶ arrow buttons** (page + hold-repeat + auto show/hide).
- [x] Libraries row as **landscape** cards.
- [x] Home **stale-while-revalidate** (signature diff on re-entry;
  `on_sync_state` reload).

**Detail / Series / Season**
- [x] Detail **action row**: Mark-watched, Favorite, Download, Go-to-Series,
  Trailer (only tile-menu today, not on the detail page).
- [x] **Audio / Subtitle / Version pickers** + pass `media_source_id` /
  `audio_index` / `subtitle_index` into play (+ language_config defaults).
- [x] **Media-info line** (codec/res/HDR/container/size/bitrate/"Ends at").
- [ ] **Chapters** row (thumbnails + seek-to-chapter).
- [x] **Cast & Crew** people row + person-filmography route.
- [x] Episode **autoplay-next season queueing** (Tk queues rest of season).
- [x] Episode title formatting ("Series — S1E1 · Title").
- [x] Series/Season action buttons: Play Next Up, Shuffle, Mark watched,
  Download, back-to-series; metadata + More-Like-This.

**Grid**
- [ ] Filter bar: **sort** dropdown, **Unplayed/Favorites** filters,
  **Genre** + **Year** pickers, **A–Z letter-jump**, **Shuffle**,
  **Collections** toggle, status/count line + retry.
- [x] **Person** filmography route (`get_person_items`).

**Music**
- [x] Missing tabs: **Album Artists**, **Songs** (tabular track list).
- [x] **Instant Mix**, **Add to Queue**, artist/genre **action bars** (album/artist backdrops still TODO).

**Now-playing bar**
- [x] **Seek scrub**, **volume slider**, **repeat cycle**, **favorite**,
  add-queue-to-playlist (controller already exposes seek/volume).

**Queue**
- [x] **Reordering** (Top/Up/Down/Bottom) + Artist/Runtime columns.

**PlaylistEdit**
- [x] **Rename**, **public toggle** (delete deferred).

**Playlist / Search shaping**
- [x] Playlist: Shuffle, Download (shape switching still portrait).
- [x] Search: per-type grouped rows with correct shapes.

**Dialogs**
- [x] AddTo: **create-new** playlist (collections still TODO).
- [x] SyncPlay: participants + Refresh (joined-state TODO).
- [x] Download: already-downloaded + watched counts.

**Chrome**
- [ ] Persistent **download status bar** ("View Downloads").
- [x] Offline banner **"Configure Servers"** action.
- [ ] Login **Quick Connect** flow.

**Display mirror (Phase 6)** ✅ replaced Tk+Pillow with mpvtk (attaches
to the player's mpv; backdrop+gradient+text baked into one full-window
bitmap; mutually exclusive with the mpvtk browser). `display_mirror.py`.

Confirmed intentionally deferred (not regressions to chase now):
ClosePreference (N/A in-window), keybinding reconciliation, spatial/remote
nav (Phase 8). *(The user switcher, PIN setup and the Servers/Logs/
Downloads panels were on this list; they shipped in the round-1 fixes
above.)*

## Second audit (2026-07-19) — full code-level sweep

A subagent diffed every Tk view/dialog against its mpvtk counterpart. The
checklist above was stale in **both** directions (several `[x]` items were
only partly done). Everything below is verified against code. Items marked
✅ were fixed in the round-1 batch; the rest are open, ordered by severity.

### Open — blockers

- [ ] **Offline mode is entirely unwired.** `OfflineLibrarySource`
  (`repository.py`) and `MpvtkBrowser.set_offline` both exist and *nothing
  calls either*. `ui.py:_connect` only skips connecting when
  `work_offline` is set, leaving a permanent spinner with no catalog and
  no login route. The offline banner (`_banner`) is therefore dead code.
  The Tk browser had `_enter_offline`/`_exit_offline`/`offline_fallback`/
  `_show_disconnected`.
- [ ] **No offline playback fallback.** Tk's `on_play` fell back to the
  local copy when the client was disconnected (`syncManager.db.is_complete`);
  `ui.py:play_list` just logs "no connected client" and returns.
- [ ] **`clientManager.on_servers_changed` / `on_server_connected` are not
  registered.** A server that reconnects in the background stays invisible
  until restart. `gui_mgr.py:253-258` wires both.
- [ ] **Quick Connect is missing entirely** (button, code display, cancel,
  supersede logic). `views.py:1969-2028`.
- [ ] **Playlist Play All / Shuffle always pass `audio=True`**, so a video
  playlist plays behind the browser instead of yielding the window.
- [ ] **`edit_apis` capability gate missing.** Tk hid playlist/collection
  edits on jellyfin-apiclient-python < 1.15 and said why; `ui.py:_edit`
  swallows every failure into a log line with no user feedback. This one
  violates the project's optional-dependency degradation policy.

### Open — parity gaps

**Grid** — year filter dropdown; Collections (BoxSet) toggle
(`get_movie_collections` exists, uncalled); the Critic Rating and Parental
Rating sort modes; load-failure state + click-to-retry; the Random-sort
single-page cap and the empty-page guard (both prevent bad paging);
`library_page_size` and `library_image_width` are ignored (hardcoded 100 /
fixed geoms).

**Detail / Series / Season** — chapters row (`chapter_image_url` exists,
uncalled); trailer button (`get_trailers`, uncalled); **track-picker
defaults ignore `language_config` and the source's Default*StreamIndex**,
so the picker misreports what playback will do; media-info line is missing
audio codec, channel layout, file size, bitrate and "Ends at"; Series has
no Shuffle and no More-Like-This; Season has no Play Next Up; season
watched state reads the season DTO instead of `all(episodes)`; bulk
watched marks don't refresh child state; the Download button never becomes
"Remove download".

**Music** — artist/album pages have no backdrop header, overview or
More-Like-This; tile subtitles (album artists / "%d albums" / track index)
dropped; `AlbumArtist` item type not routed.

**Playlists** — contents aren't filtered to `PLAYLIST_SUPPORTED_TYPES`;
music playlists don't render as a track list; clicking an entry opens its
detail page instead of playing from that position; "Delete Downloads" and
per-entry "Remove from playlist" missing; no reload-on-failed-edit.

**Downloads / sync** — no persistent download status bar; no live
per-item progress and no `sync_state`-driven refresh; no offline
watched-mark queueing; `try_skip_within_queue` fast path missing.

**Dialogs / menus** — collections unsupported everywhere (AddTo is
playlist-only, no `collection_edit`); AddTo has no private-by-default
toggle (the API creates public playlists); SyncPlay has no joined-state
indicator; tile menu is missing "Add to queue" and "Add to collection",
has no item-type gating (it attaches to libraries and people), and the
view-contributed actions hook is gone.

**Shell** — `library_last_server` never read or written; no startup update
check; the `connecting` route is in `CHROME_FREE` but absent from the
dispatch table, so it falls through to a bare spinner (and has no "work
offline" escape); no browser crash recovery; responsive chrome collapse,
chrome tooltips, now-playing album art, and "add current queue to
playlist" all dropped.

**Queue** — no multi-select, no double-click-to-jump.

**Tray** — ✅ restored. The tray moved out of `gui_mgr` into
`jellyfin_mpv_shim/tray.py` so either UI can own one; `mpvtk_browser.ui`
starts it and maps the menu onto the in-window browser (Show Library
Browser → `activate()`, Configure Servers / Show Console → the matching
Settings tab). It stays a separate **process**, not a thread — pystray
needs its process's main thread and pystray + libmpv in one process
segfaults with GNOME AppIndicator. `UserInterface.activate` exists now, so
a second launch surfaces the window instead of doing nothing.
`start_minimized` and `close_to_tray` work again, once "minimize" was
pinned down as a *player* state rather than a window-manager action. With
one shared window the state is the product of two mpv properties:

| state                      | playback_abort | force_window |
|----------------------------|----------------|--------------|
| library browser            | yes            | yes          |
| media playing              | no             | yes          |
| "minimized" (tray only)    | yes            | no           |
| cast to, library not open  | no             | no           |

So minimizing is releasing `force_window` with nothing playing — which is
also why the app stays a usable cast target while minimized, and why a cast
that ends while minimized returns to row 3 instead of popping the library
open. `set_browse_window(True/False)` moves between rows 1 and 3;
`MpvtkBrowser.minimize()`/`enter_browse()` drive it.

Both settings are ignored when no tray came up — otherwise the app would be
running with no way to reach or quit it.

`close_prompt_shown` is **intentionally dead**. The Tk browser asked
"Minimize to Tray / Exit?" on first close; here the window is already gone
when CLOSE_WIN arrives, so a modal would mean re-creating the window to
ask. Minimizing is harmless as long as the setting is discoverable, which
it is (Settings → Interface → "Close to Tray"). Don't re-add the prompt.

**Minimized is cheap.** `mpv_idle_quit` now defaults on, and minimizing
clears `mpvtk_active`, which is what gates it — so a minimized app drops
mpv entirely after `mpv_idle_quit_secs` and gives back the window, the GPU
context and the process memory. Two consequences the UI has to handle, via
the new `playerManager.on_mpv_gone` / `on_mpv_recreated` hooks:

- The composited tile bitmaps must be freed on teardown. On libmpv they are
  in-process buffers that mpv reads *by address*, so keeping them both
  leaks and defeats the point of the quit. `on_mpv_gone` clears the
  `StripStore`.
- mpvtk binds its event callbacks and loads `renderer.lua` at attach time,
  so the app object is per-handle. `on_mpv_recreated` builds a fresh
  `MpvtkApp` on the new handle and restarts the loop thread; the browser
  keeps its routes, data and thumbnail cache and is just re-pointed. The
  loop ending because *we* detached must not be mistaken for a window
  close (`UserInterface._detaching`), or an idle-quit would exit the app.

### Fixed in round 1 from this audit

✅ Settings sections/labels/enum dropdowns, `sync_path` relocation,
`language_preference` materialization, `browser_ui` editable again; user
switching / add / rename / delete / PIN setup; server list + removal;
downloads manager with delete; log viewer; playlist-editor multi-select,
columns, delete-playlist and the **unsafe Public toggle** (it never read
the server's `OpenAccess`); music infinite scroll; carousel arrow
auto-hide.

### Display mirror regressions (introduced by 8b86473)

- [x] Every window resize refetched the backdrop over the network, and the
  *idle* screen re-rolled its random backdrop mid-drag. Decoded backdrop is
  now cached per data change.
- [x] Unbounded bitmap accumulation — the strip key was a monotonic
  counter, a guaranteed cache miss, retaining up to 48 full-window BGRA
  buffers (~400 MB at 1080p). Now content-keyed.
- [x] Not fullscreen (the core cast-screen UX) — it inherited the browser's
  non-fullscreen window. Now asks for fullscreen explicitly.
- [x] OSC was never suppressed while mirroring.
- [x] `stop()` before `run()` was dropped, hanging the app on a tray Quit
  during `gui_ready.wait()`.
- [ ] Closing the mpv window now terminates the whole app; the Tk mirror
  survived `q` and rebuilt mpv on the next cast. No re-open path exists.
- [ ] Mouse cursor is no longer hidden (`cursor="none"` in the Tk version).
- [ ] Pre-existing: the logo image is fetched but never drawn; the idle
  return path after a `DisplayContent` preview is still unwired (same as
  Tk, but the webview era did implement it).
- [ ] Stale docs: `CONTRIBUTING.md:77` still says "tkinter + Pillow";
  `README.md:165` documents Alt+F4 as mirror-specific; `win_utils.
  mirror_act` matches a Tk window title that no longer exists.

**Goal.** The library browser and the display mirror render *inside the
player's own mpv window* — one window that shows the browser when idle
and the video when playing — instead of separate Tk/Pillow windows in a
child process.

**Guiding constraints (do not relitigate — established in the spike):**
- One mpv instance, shared with playback. mpvtk must **attach** to
  `playerManager`'s existing mpv, never spawn its own (that spawn path
  in `app.py` is demo/selftest scaffolding only).
- Both mpv backends (libmpv in-process, python-mpv-jsonipc external)
  must keep working — `player.py` picks between them at runtime and so
  must the attach path.
- Bitmaps composite **above** all ASS: bake tile decorations into
  strips, dialogs occlude rather than dim, no translucent scrim over
  posters. (GUIDE §6.)
- Everything beyond the four required deps degrades gracefully
  (CONTRIBUTING policy). The browser depends on Pillow already (via the
  `mirror`/`gui` extras); keep the `try/except ImportError` + fallback
  pattern.
- **The `browser_ui` flag is temporary migration scaffolding, not a
  permanent dual-UI abstraction.** The end goal is *rip out and
  replace* — the Tk browser has **not shipped to real users**, so there
  is no compatibility debt to preserve. The flag exists only so mpvtk
  can be built and field-tested against the working Tk UI, then Tk gets
  deleted. **Do not build a pluggable-UI seam** (adapter layers,
  abstract base "UI backend" classes, shared indirection) to host both
  — that abstraction *is* the cruft we must avoid. Port by reading the
  Tk view and writing the mpvtk view directly against
  `playerManager`/`repository`; let the two implementations sit
  side-by-side behind a simple `if` in `mpv_shim.py`, and remove the
  loser.

**Status legend:** `[ ]` todo ・ `[~]` in progress ・ `[x]` done ・
`[-]` intentionally dropped.

## Implementation status (live)

The mechanical view/dialog inventory is **complete**. mpvtk is the
**default** UI (`browser_ui="mpvtk"`), attached to the player's mpv window.

- **Phase 0 (foundation)** ✅ + launch wiring; logo-free free-resizing
  browse window; browse↔playback handoff (audio keeps the now-playing
  bar, video yields to the OSC); idle-quit guard. Field-confirmed.
- **Phase 1 (core views)** ✅ Home, Grid, Detail, Series, Season, Search.
- **Phase 2 (music/playlists)** ✅ Music tabs, Album (track list), Artist,
  MusicGenre, Playlist, **PlaylistEdit** (reorder/remove), **Queue**.
- **Phase 3 (auth/settings)** ✅ Settings (schema form), Connecting state,
  **Login** (add-server), **Locked** (startup PIN).
- **Phase 4 (dialogs)** ✅ modal infra + message/confirm, SyncPlay,
  Add-to-Playlist, Download (with size estimate).
- **Phase 5 (chrome)** ✅ nav (back/home/search/server-switcher/SyncPlay/
  Settings), now-playing bar, update/offline banners, tile context menus
  (watched/favorite/play/add-to-playlist/download).

**Known remaining (fine-sanding / follow-ups, not blocking):**
- User switcher + PinSetup dialog + full Servers/Logs/Downloads *panels*
  (Settings is a single flat schema form rather than the Tk notebook).
- ClosePreference dialog — N/A for the in-window browser (closing the mpv
  window quits).
- Keybinding reconciliation (mpv default keys like `q` still fire while
  browsing); overview/seek-slider polish; **spatial/remote nav (Phase 8)**.

~340 automated checks: fast suite (`test_mpvtk_browser_shell` 76 +
adopt/strips/thumbnails/config) + real-mpv exit test on both backends.
Every view/dialog has scene-level unit coverage.

---

## Source-of-truth map (what we are porting)

Tk browser lives in `jellyfin_mpv_shim/library_browser/`:
`app.py` (1659, `BrowserApp` shell + routing + IPC pump),
`views.py` (4160, 17 views + 4 settings panels + 6 dialogs + tiles),
`widgets.py` (794, `MediaTile`/`TrackRow`/`ScrollableGrid`/`HScrollRow`/
`VScrollFrame`/`NavButton`), `repository.py` (1207, **UI-agnostic** API
facade), `thumbnails.py` (278, **UI-agnostic** loader pool),
`theme.py` (133, tokens), `icons.py`/`_icon_paths.py` (Material icons —
already shared with mpvtk via `svgpath`).

Launched today as a **separate `multiprocessing.Process`**
(`gui_mgr.BrowserProcess` → `library_browser.app.run_browser`), talking
to the main process over two `multiprocessing.Queue`s (`cmd_queue`
main→browser, `r_queue` browser→main). It never holds live
`clientManager`/`playerManager` refs. **Migrating collapses that process
boundary:** the mpvtk browser runs in the main process next to
`playerManager`, so today's queue-marshalled `on_*` handlers become
direct in-process calls.

---

## Phase 0 — Foundation (integration plumbing) ⚠ blocks everything

Nothing below Phase 0 can land without these. Build and prove them
against a stub UI before porting real views.

### 0.1 Attach mpvtk to the player's mpv ✅ backend done
- [x] Add an **"adopt existing handle"** backend to `mpvtk/app.py`:
  `MpvtkApp.attach(mpv_handle, ext)` (→ `AdoptBackend`) skips `MPV(...)`,
  registers a coexisting `client-message` callback + issues
  `load-script renderer.lua` on the passed handle; `stop()` never
  terminates the shared handle. Spawn backends kept for demo/selftest.
- [x] Expose the player's handle: `PlayerManager.get_mpv()` +
  module-level `is_using_ext_mpv` → mpvtk `in_process` (memory-store
  images on libmpv, files on jsonipc).
- [x] **Multiplex `client-message`.** Verified both backends store
  handlers in a set (`bind_event`) / support multiple `event_callback`s,
  so `AdoptBackend`'s listener coexists with the player's `shim-*`
  handler (`player.py:646`); `mpvtk-*` namespace doesn't collide.
  Unit-tested end-to-end in `tests/test_mpvtk_adopt.py` (9 checks, both
  backend flavors, via `FakeMPV`).
- [ ] Run `MpvtkApp.run(build)`'s loop on a **dedicated thread** in the
  main process (it currently blocks) — deferred to 0.5 wiring, where the
  browser is actually spawned next to `playerManager`.

### 0.2 Window / idle lifecycle ✅ (wired; keybinding reconciliation later)
- [x] **Persistent window while browsing** + **idle-quit guard**:
  `PlayerManager.mpvtk_active` flag (player.py) + guard in `idle_quit()`
  (same shape as the `get_webview()` guard) so browsing never tears the
  window down. Unit-tested in `test_mpv_lifecycle.IdleQuitGatingTest`.
- [x] **Browse ↔ playback handoff, modeled on the `c` menu**:
  `_PlayerController` (mpvtk_browser/ui.py) — `on_browse_enter` →
  `force_window(True)` + `enable_osc(False)`; `on_browse_leave` →
  `enable_osc(settings.enable_osc)`. The browser yields on a playable
  click (`_enter_playback`: `_browsing=False`, empty scene clears
  overlays off the video, OSC restored) and takes the window back when
  `on_playstate({"stopped": True})` fires (registered as
  `playerManager.on_playstate`). Browser-side logic unit-tested in
  `test_mpvtk_browser_shell.TestPlaybackLifecycle`.
- [~] Keybinding reconciliation (the player's `input_default_bindings`
  vs the renderer's bindings while browsing) is deferred to a polish
  pass — mouse navigation works now; some mpv default keys (e.g. `q`)
  are still live during browse.
- [~] Full browse↔play↔return **on the real player** (vs the exit
  test's spawned handle) is what the launch wiring below enables for
  live testing.

### 0.6 Launch wiring — mpvtk is the default UI ✅
- [x] `mpvtk_browser/ui.py` `UserInterface` (same contract as
  `cli_mgr`/`gui_mgr`): `login_servers()` attaches `MpvtkApp` to
  `playerManager.get_mpv()`, opens the window on a spinner, connects in
  the background, then `set_source()` populates. Runs the app loop on a
  daemon thread; window-close releases `main()`'s halt loop.
- [x] `conf.py:browser_ui` (default **`"mpvtk"`**) selects it in
  `mpv_shim.py`; falls back to the Tk browser then CLI if Pillow / the
  mpvtk UI can't load (graceful-degrade policy).
- [x] Storage per backend (memory on libmpv, files on jsonipc);
  `ThumbnailStore` wired for real posters.

### 0.3 Data layer into the main process ✅ ported (runtime wiring in 0.5)
- [x] **Relocated** `repository.py` (`LibrarySource`/`OfflineLibrarySource`/
  `ServerConn`) to the new `mpvtk_browser/` package — its canonical home
  (pure API, no Tk; `..constants`/`..i18n`/`..sync.db` still resolve).
  The doomed Tk package + tests repoint to it; nothing new depends on the
  old package. Constructing it in the main process (with the
  `_collect_servers()` credential list, coexisting with `clientManager`'s
  browse clients) happens in 0.5 wiring.
- [x] **Ported `thumbnails.py`** → `mpvtk_browser/thumbnails.py`: yields
  decoded **PIL images** from `pump()` (no `ImageTk`), plus a thread-safe
  `notify` hook so the loop wakes via `MpvtkApp.invalidate()` and drains
  on the next render. Tk's `ImageTk` version stays in `library_browser/`
  (dies at cutover — temporary, since the divergence is UI-specific).
  Unit-tested in `tests/test_mpvtk_thumbnails.py` (9 checks).

### 0.4 Production strip compositor ✅
- [x] Real `StripStore` in `mpvtk_browser/strips.py` (`Tile`/`TileGeom`):
  composites rows of real posters + baked captions/subtitle/badges/
  progress/watched, content-keyed (folds poster identity + every visible
  prop + geometry), LRU-bounded, memory-store on libmpv / files on
  jsonipc. The tile primitive for every grid/row view.
- [x] Placeholder tile when no poster yet; `poster_tag` in the key means
  the strip recomposites the moment the real poster arrives. Unit-tested
  in `tests/test_mpvtk_strips.py` (9 checks incl. both storage backends,
  LRU free, valid BGRA size). Theme tokens ported to
  `mpvtk_browser/theme.py`.

### 0.5 App shell & routing skeleton ✅ (skeleton; views are Phase 1)
- [x] `MpvtkBrowser` (`mpvtk_browser/app.py`): `nav_stack` of route
  dicts, `build(size)` dispatching on route `kind`, `navigate(reset=)` /
  `go_back()` / `after_playlist_deleted()`.
- [x] Threading: background `ThreadPoolExecutor` `run_async`, results
  applied on the loop thread under lock with an **epoch guard** (stale
  results from superseded navigations are dropped); repaint via
  `invalidate()`; `thumbs.pump()` drained at the top of `build()` so
  freshly-decoded posters land before strips compose.
- [x] Chrome-free routes (login/locked/connecting) suppress the nav bar.
- [x] Theme tokens ported (`mpvtk_browser/theme.py`); ttk styling dropped.
- [x] Home + Grid routes implemented (strip rows, library nav, paginated
  infinite scroll) to prove the shape. Unit-tested in
  `tests/test_mpvtk_browser_shell.py` (14 checks: routing, epoch
  staleness, scene assertions).

**Phase 0 exit test** ✅ `tests/integration/test_mpvtk_browser.py`:
`MpvtkBrowser` **attached to a real mpv** (via `MpvtkApp.attach`) renders
home + strip rows and a tile click navigates into a grid — passing on
**both backends** under xvfb (memory-store on libmpv, files on jsonipc).
Registered in `run_integration.py:PER_BACKEND_REAL`. Remaining exit-test
scope (idle-survival + real playback hand-off/return) lands with 0.2.

---

## Phase 1 — Core browsing views

Each item: port the view's `_build()` to a `build(route, size)` that
returns an mpvtk tree; wire data via the async pattern; reproduce the
interactions. Widgets in parens are the mpvtk primitives to use.

- [ ] **Home** (`HomeView`, views.py:192) — library shelves + Continue
  Watching / Next Up / latest carousels. (HScroll rows of ImageMap
  strips + library grid.) Preserve `home_cache` stale-while-revalidate
  and `_signature` diffing. Data: `get_libraries`, `get_home_rows`.
- [ ] **Grid** (`GridView`, views.py:369) — paginated library/folder
  grid + filter bar. (VScroll windowed grid of strips; filter bar =
  Dropdown sort + Dropdown genre + A–Z letter jump row + Collections
  Checkbox/toggle + Shuffle Button.) Infinite scroll via `on_scroll`
  windowing (demo `_grid_section` is the template). Tile context menu
  (Phase 5). Data: `get_library_items`, `get_filter_values`,
  `get_genres`, `get_shuffle_ids`, `get_movie_collections`.
- [ ] **Detail** (`DetailView`, views.py:1530 — largest) — backdrop,
  metadata, media info, chapters, cast, similar. (Backdrop Image;
  Play/Resume Button; version picker Dropdown; audio/subtitle Dropdowns;
  chapters HScroll of chapter-image strips; trailer/favorite/download
  Buttons; similar + cast HScroll rows.) Data: `get_item`,
  `get_similar`, `get_trailers`, chapter images. Note media-info text
  update path.
- [ ] **Series** (`SeriesView`, views.py:702) — poster, overview,
  seasons, similar/people. (Season tiles; Shuffle/Favorite/Download.)
  Data: `get_item`, `get_seasons`.
- [ ] **Season** (`SeasonView`, views.py:841) — episode list + season
  switcher. (Episode strip grid; season switcher Dropdown; Play Next
  Up / To Series / mark-season-watched Buttons.) Data:
  `get_episodes`, `get_seasons`.
- [ ] **Search** (`SearchView`, views.py:1437) — results grid + people
  row. (Result strip grid + people HScroll.) Data: `search`,
  `search_people`. Search box lives in chrome (Phase 5) but this view
  consumes the query.

---

## Phase 2 — Playlists, music, queue

- [ ] **Playlist** (`PlaylistView`, views.py:950) — playlist/download
  contents, play-from-index, shuffle, edit entry, delete-downloads,
  context actions. (Strip grid + Buttons.) Data: `get_playlist`,
  `get_playlist_items`.
- [ ] **PlaylistEdit** (`PlaylistEditView`, views.py:1148) — reorder /
  rename / visibility. Replaces a **Treeview** → mpvtk table composite
  (header Row + selectable Rows + Top/Up/Down/Bottom Buttons; demo
  track-table is the template). Rename via TextBox; public toggle via
  Checkbox. Reorder is button-driven (no DnD — matches Tk).
- [ ] **Music** (`MusicLibraryView`, views.py:3699) — Albums/Artists/
  Songs/Genres. Replaces a **Notebook** → tab-button Row + view switch
  in `build()`; each tab a windowed `_MusicGrid`. Data: `get_music_
  albums`, `get_album_artists`/`get_artists`, `get_songs`,
  `get_music_genres`.
- [ ] **Album** (`AlbumDetailView`, views.py:3841) — track list + music
  actions (Play/Shuffle/Queue/Instant-Mix). (Track table composite.)
  Data: `get_album_tracks`, `get_item`.
- [ ] **Artist** (`ArtistDetailView`, views.py:3889) — albums + top
  songs + similar/people. Data: `get_artist_albums`,
  `get_artist_songs`.
- [ ] **MusicGenre** (`MusicGenreView`, views.py:3943) — albums in a
  music genre. Data: `get_genre_albums`, `get_genre_songs`.
- [ ] **Queue** (`QueueView`, views.py:3979) — live playback queue
  editor. Treeview → table composite; reorder + remove + double-click
  to jump. Data is **pushed** from the player (`on_queue_data`) — now a
  direct in-process call instead of an IPC message.
- [ ] Port shared mixins: `_DetailRowsMixin` (cast row + similar row),
  `_MusicActionsMixin` (play/queue/instant-mix), `_MusicGrid` (lazy
  paginated grid), `_ServerForm` (reused by Login + ServersPanel).

---

## Phase 3 — Auth & settings

- [ ] **Connecting** (`ConnectingView`, views.py:2520) — spinner
  splash. (Busy node.) Chrome-free.
- [ ] **Login** (`LoginView`, views.py:2540) — add-server / login via
  `_ServerForm` (address/user/pass TextBoxes + Quick Connect flow).
  Chrome-free. Quick Connect code arrives via push.
- [ ] **Locked** (`LockedView`, views.py:2597) — PIN gate + user
  switch. (PIN TextBox `mask=True`.) Chrome-free.
- [ ] **Settings** (`SettingsView`, views.py:3470) — Notebook shell →
  tab-button Row hosting 4 panels:
  - [ ] **ServersPanel** (views.py:2656) — server + local-user mgmt,
    add/rename/set-PIN, embeds `_ServerForm`.
  - [ ] **LogsPanel** (views.py:2791) — read-only log view → VScroll of
    Text lines (demo Logs page is the template); `on_log_line` push.
  - [ ] **DownloadsPanel** (views.py:2841 — largest class) — offline
    download manager grouped by playlist/series/season, determinate
    progress bars (nested-Box), periodic refresh timer + `on_download_
    progress`/`on_sync_state` pushes.
  - [ ] **SettingsPanel** (views.py:3271) — schema-driven form
    (Checkbox/TextBox/Dropdown), advanced toggle, `_save`. The folder
    picker (`filedialog`) → **path TextBox** (`filedialog` accepted as
    a loss — PARITY). Move-progress via `on_folder_progress`.

---

## Phase 4 — Dialogs (mpvtk `Dialog`; no backdrop dim — z-order)

- [ ] **PinDialog** (views.py:2046) — PIN entry (unlock / switch user).
- [ ] **PinSetupDialog** (views.py:2113) — set/change/remove PIN +
  startup-lock opt-in (TextBoxes + Checkbox).
- [ ] **ClosePreferenceDialog** (views.py:2204) — minimize-vs-quit
  first-close prompt.
- [ ] **AddToDialog** (views.py:2264) — add to existing/new playlist or
  collection. Listbox → VScroll of Buttons + new-name TextBox +
  sync-mode.
- [ ] **SyncPlayDialog** (views.py:2424) — join/leave group; group list
  arrives via `on_groups` push.
- [ ] **DownloadDialog** (views.py:3553) — confirm download w/ size
  estimate + include-watched Checkbox; `on_estimate` push.
- [ ] **Messageboxes** (`app._message`/`_show_message`, app.py:992/
  1056) → simple `Dialog` composite.

---

## Phase 5 — Chrome / shell

- [ ] **Nav bar** (`_build_chrome`, app.py:159) — Back/Home/Settings/
  SyncPlay icon+label Buttons, search TextBox, server switcher +
  user switcher Dropdowns. Reproduce responsive icon-only collapse on
  narrow widths (`_relayout_topbar`) — or accept a fixed layout at 10ft
  sizes. Back-button enable state from `nav_stack` depth.
- [ ] **Now-playing bar** (`_build_playbar`, app.py:349) — persistent
  bottom bar while audio plays: transport Buttons (prev/playpause/next/
  stop), volume Slider (scrub), queue/add/favorite/repeat Buttons,
  title Text, seek Slider (press/drag/release), time Text. Driven by
  `on_playstate` (now a direct call) + ~1 Hz position push
  (`invalidate()` timer; ASS-only deltas are cheap with sticky slots).
  Controls call `playerManager` directly instead of `_send_r`.
- [ ] **Banners** — update banner (`_show_update_banner`), offline
  banner + retry, generic banner. Use `Float`/top-of-page Row. The
  existing `playerManager.notify_update` routing already targets the
  browser (see memory: update-notice routing) — repoint it.
- [ ] **Tile context menus** (`MediaTile._show_context_menu`,
  widgets.py:223) — mark watched/unwatched, favorite, plus per-view
  `tile_context_actions(item)` extras. mpvtk `Menu` at right-click
  point (demo context menu is the template). Recomposite affected
  strips on watched/favorite change.

---

## Phase 6 — Display mirror (bonus)

Reimplement `display_mirror.DisplayMirror` (Tk+Pillow fullscreen
window) on mpvtk sharing the *same* mpv window. Three states, small
public contract to preserve.

- [ ] Idle state: random backdrop (`_random_backdrop_url`) + "Ready to
  cast" title/overview. (Backdrop Image + Text.)
- [ ] Item-preview state: `display_content` fetches item →
  backdrop/logo/title/misc/rating/overview. (Backdrop Image + Text;
  optionally *draw the logo* — currently fetched-but-never-drawn, a
  latent gap you may fix or preserve.)
- [ ] Hidden during playback (mpv shows video). Reuse the browse↔play
  mode transitions from Phase 0.
- [ ] **Preserve the public surface** so it stays a drop-in: module
  singleton `mirror`, methods `run()` / `stop()` /
  `display_content(client, arguments)` / `get_webview()` returning an
  object with `hide()`/`show()`. But `run()`-as-main-loop **goes away**
  — mpvtk already lives in mpv, so drop the separate Tk root and the
  `gui_ready.wait()` ordering hack (`mpv_shim.py:139`).
- [ ] Wiring stays: `eventHandler` `DisplayContent` → mirror
  (event_handler.py:150); `player.py` hide/show on play/stop
  (909/1857); `display_mirroring` config key (conf.py:104), menu toggle
  (menu.py:587). Idle-return path (`("idle", …)`) is currently dead —
  decide whether to wire it live.

---

## Phase 7 — Cutover, wiring, cleanup

- [ ] **Temporary `browser_ui` flag** (`"tk" | "mpvtk"`, default `tk`
  until parity) gating which UI `mpv_shim.py` starts — a plain `if`, no
  abstraction layer. This flag is scaffolding to be **deleted** at
  cutover, not a permanent setting.
- [ ] Rework `gui_mgr.py`: for the mpvtk path, do **not** spawn
  `BrowserProcess`; run the mpvtk browser in-process. Convert the
  queue-marshalled `on_*` handlers (playstate/update/sync/download/
  queue pushes) to direct `MpvtkBrowser` calls. Keep the systray
  process (pystray still wants its own process).
- [ ] Reconcile OSC/keybindings: `enable_osc`, `menu_mouse`,
  `osc_style` govern the player's loaded lua/bindings; ensure browse
  mode disables them and playback restores them (Phase 0.2).
- [ ] i18n: browser strings already flow through `_()`; make sure the
  mpvtk build path passes translated strings through scene JSON.
- [ ] Tests: extend the mpvtk selftest with real-view smoke checks
  (route → build → scene assertions) on both backends under xvfb; add
  layout unit tests for the new table/grid composites.
- [ ] Docs: update `CLAUDE.md` (its ARCHITECTURE `mirror` bullet is
  already stale — Jinja2/pywebview long gone; and the browser process
  model changes), refresh `PARITY.md` statuses, note the shared-window
  model.
- [ ] **Rip out Tk** once parity is field-proven (this is the goal, not
  an option — Tk never shipped): delete the Tk browser
  (`library_browser/app.py`, `views.py`, `widgets.py`, `theme.py`, and
  `icons.py`/`_icon_paths.py` **only if** mpvtk's `svgpath` path fully
  supersedes them), the Tk display mirror, `gui_mgr.BrowserProcess` +
  its queue plumbing, and the `browser_ui` flag itself. Keep
  `repository.py`/`thumbnails.py` (now the mpvtk data layer). Audit for
  orphaned IPC/`on_*` handlers left behind.

---

## Phase 8 — Spatial keyboard/remote navigation (optional, the 10-ft payoff)

**SHIPPED (2026-07-19), renderer-local and simpler than the sketch
below expected:** no `focusable` protocol flag was needed — the
renderer infers focusables from the interactivity the scene already
carries (click/dbl/textbox/dropdown/slider), so every existing view
got keyboard navigation for free. Arrow keys move focus by a
directional metric (forward distance + 2.5× orthogonal penalty; first
press focuses the topmost-leftmost visible node), an accent ring draws
outside the focused node (hover-ring path), and focus scrolls its
container chain into view. ENTER activates (click / textbox focus /
dropdown popup with UP/DOWN+ENTER — context menus too / slider adjust
mode: LEFT/RIGHT step 5%, white ring). A focused textbox owns the
arrows/ENTER (delegated, so binding precedence can't break editing);
any mouse press drops key focus; `mpvtk-active no` unbinds everything
so playback keeps its seek keys. Modal dialogs restrict candidates to
their own nodes. Test hooks: `mpvtk-debug {cmd=nav, dir=|action=enter|
id=}` + `nav`/`nav_pidx` in `debug_state`; 5 selftest checks both
backends. Remaining polish: BACK/ESC as go-back is app wiring; tile
ImageMap regions already navigate (they're click rects).

**Polish round (same day, from field feedback):** focus ring uses the
theme accent (white stays for slider adjust) and replaces hover
styling on the focused node; when nothing on-screen lies in the
pressed direction the focused node's scroll chain pages ~60% viewport
and retries, so fully clipped carousel/grid tiles are reachable;
keyboard/remote modality is reported to the app (`nav` event →
`MpvtkApp.on_nav`) and the browser hides carousel arrows while it's
engaged; Jellyfin remote GeneralCommands (MoveUp/…/Select/Back) route
into the nav keys via `player.menu_action` + the new
`user-data/mpvtk/active` mirror, so a phone/web remote drives the
browser (and still seeks during video playback). Row flex-shrink now
floors clickable Boxes at natural size — the "E…"/"U…" squeezed-button
reports. A virtualized Table pins `min_w` to its widest content across
ALL rows so its natural width no longer depends on scroll position.

**Nav fixes (field round 2):** direction picking is two-tier — tier 1
requires orthogonal-interval overlap (same row for horizontal, same
column for vertical), so RIGHT at the end of a fully scrolled carousel
does nothing instead of hopping to a diagonal tile in another row;
vertical falls back to the unaligned cone (chrome/now-playing stay
reachable). A move into unmaterialized content remembers its direction
and completes when the next scene arrives (`nav_pending` in
reconcile), and focus loss (virtualization dematerialized the node)
re-anchors the next press to the nearest focusable at the remembered
rect instead of resetting to the top bar. Separately: the recurring
"E…" squeezed buttons were NOT the shrink pass — an exact-fit label
lost ~1e-14 to float association between measure and arrange and the
strict ellipsize comparison truncated it; both ellipsize
implementations now carry half-pixel slop.

Original build sketch (kept for reference):

Net-new capability, **nothing built yet** — the only "focus" in mpvtk
today is *textbox* focus for text editing (renderer.lua `state.focus`,
a single id). This is the biggest new chunk and the real reason to
render in mpv at all for a couch/remote experience. Not required for
Tk parity; can land in parallel with or after cutover, but only makes
sense once there's a full UI to navigate (after Phase 5). Build sketch,
grounded in what already exists:

- [ ] **Focusable flag on nodes.** Layout already emits flat nodes with
  absolute `x/y/w/h` + owning-scroll (`sc`) — add a `focusable` marker
  (buttons, tiles/ImageMap regions, dropdowns, textboxes, slider,
  table rows). The geometry needed for spatial math is already in the
  scene; no protocol redesign.
- [ ] **Renderer-side focus model.** Generalize the existing single
  `state.focus` into a focused-node id over all focusable nodes
  (survives scene pushes, keyed by id — same discipline as scroll/
  textbox state). A `force`-style reset when a route changes.
- [ ] **Arrow-key nav = nearest-in-direction.** On UP/DOWN/LEFT/RIGHT,
  pick the focusable node whose center best matches the direction
  (directional distance metric over the flat node list) — the renderer
  already owns hit-testing geometry, so this is the same math applied
  to keys instead of the mouse. ENTER activates (emit the node's
  `click`/`select`); text nodes enter edit mode.
- [ ] **Focus ring** reuses the hover-ring path (rings already draw
  outside image bounds for ImageMap regions — the tile focus indicator
  is free). Distinguish focus vs hover styling.
- [ ] **Scroll-into-view.** Moving focus into a node inside a scroll
  container adjusts that container's offset so the focused node is
  visible (the renderer owns offsets already).
- [ ] **Remote/keymap.** mpv already delivers arrow/ENTER/BACK keys;
  bind them renderer-side while the browser is active (respecting
  textbox edit mode capturing arrows for cursor movement). Reconcile
  with the OSC, which also wants arrow keys during playback (browser is
  suppressed then — Phase 0.2).
- [ ] Selftest: drive focus moves via a new `mpvtk-debug` hook and
  assert the focused id lands where geometry predicts; both backends.

Estimate: M–L. Keep it isolated behind its own feature toggle during
development so it can't destabilize the mouse-first parity path.

---

## Phase 9 — playback controls in mpvtk (retire trickplay-jf-osc.lua)

**Decision (2026-07-19, agreed with Izzie): rebuild, don't port.**
Porting spatial nav into the OSC lua means a second focus/hit/scoring
model in Lua 5.1 inside a large script we want to shrink; rebuilding
gets nav (+wraparound), tooltips, theme accent, hold-repeat, flat/
gradient styling, the selftest harness and both-backend coverage for
free. This section is the complete plan — written to be picked up in
a fresh context.

### Target behavior (YouTube-on-TV)

- Playback runs clean (no chrome). Any nav key (arrows/ENTER) or
  mouse motion summons the HUD; focus lands on play/pause.
- Bottom bar over a gradient scrim: seek slider on top; transport row
  under it (prev / play-pause / next / stop), clock "current / total",
  and right-aligned pickers (audio track, subtitle track, quality?)
  plus chapters/queue buttons as data allows.
- LEFT/RIGHT walk the focused row; on the seek slider, ENTER toggles
  adjust mode and LEFT/RIGHT scrub (slider adjust mode already does
  5% steps; consider chapter-snap). UP/DOWN move between slider row
  and button row (row-focused nav handles this already).
- Auto-hide after ~4s without input (timer in the HUD module; hiding
  = build returns an empty scene + unbind). Mouse move re-shows;
  while hidden, mpv's default keys work untouched.
- ESC/BACK hides the HUD first, then acts as stop-to-browser.

### The lifecycle inversion (the one hard design problem)

Today `mpvtk-active no` suspends the renderer during video so the OSC
gets input; the browser re-activates on stopped. A HUD inverts this:
the renderer must stay ATTACHED during playback but IDLE — empty
scene, mouse/wheel/nav sections UNBOUND — until summoned. Sketch:
- New renderer message `mpvtk-hud yes|no` (a third state besides
  active/inactive): scene stays blank and only a lightweight "summon"
  binding is live (a `keydown` catcher for arrows/ENTER + mouse-move
  observer). On summon: bind the full sections, notify Python
  (`{t=hud, active=true}`), app builds the HUD scene.
- On auto-hide timeout (renderer-side timer, like the tooltip's):
  unbind, blank, `{t=hud, active=false}`.
- The browser's `_yield`/`enter_browse` switch between browse-active
  and hud-idle instead of plain active/inactive. Keep
  `user-data/mpvtk/active` semantics: remote Move*/Select route to nav
  keys whenever EITHER browse or a summoned HUD owns input; while the
  HUD is hidden they should SUMMON it (menu_action already routes;
  the summon binding makes the first press show the HUD).
- Compatibility: `enable_osc`/`osc_style` gain an `"mpvtk"` value (or
  a new `hud_style` key); the lua OSC stays selectable until the HUD
  is field-proven, then trickplay-jf-osc.lua + osc_bridge's lua side
  are deleted.

### Data (already flowing — reuse osc_bridge's Python side)

`osc_bridge.py` already assembles: position/duration/pause state,
chapters (+seek), track lists (audio/subtitle incl. selection),
skip-intro/credits segments, watched/favorite, trickplay thumbnail
sources. The HUD module subscribes to the same feeds; playerManager
push (`on_playstate`-style) + a 1 Hz ticker like the now-playing bar.
Trickplay previews are Pillow-decoded bitmaps -> `strips.bitmap()`
one-offs, shown as an Image floated above the slider position while
scrubbing (bitmap-over-ASS is fine: the preview may cover the bar).

### Component map (all primitives exist as of this commit)

- gradient scrim -> `Gradient` (ASS-banded, controls draw on top)
- transport/chapter buttons -> `Button(flat=True)` (transparent at
  rest, translucent hover wash; hold-repeat for seek-step buttons)
- seek bar -> `Slider` (+ adjust mode; consider `Progress` overlay
  for buffered ranges later)
- track pickers -> `Dropdown(trigger_icon=...)` (chromeless icon
  trigger; popup sizes to items, clamps to screen edges, flips above
  when near the bottom — all in place)
- clock/title -> `Text`; layout via `Row(justify=...)` + `Stack`
- keyboard/remote -> spatial nav as-is (row-focused, wraparound,
  container tiers); remote GeneralCommands already route via
  `player.menu_action` + `user-data/mpvtk/active`

### Known gaps to close en route

- **Icons**: the generated set (`ui_icon_paths.py`, 40 names) lacks
  playback glyphs — need at least pause, fast_forward/rewind (or
  replay_10/forward_30), subtitles/closed_caption, audiotrack,
  bookmark/chapters, fullscreen, hd/quality. Add to the icon
  generation list (gen_ui_icons.py) — data-only change.
- Slider chapter ticks (marks on the track) — small renderer addition
  to the slider draw if wanted; not blocking.
- The demo's widgets page has a working "Playback-HUD style" sample
  (gradient + flat buttons + icon dropdown) with selftest coverage —
  the visual reference for the real thing.

### Suggested build order

1. 9.0 lifecycle: `mpvtk-hud` summon/hide in renderer + browser
   `_yield` integration; prove show/hide over real video (exit test).
2. 9.1 static bar: gradient + transport + clock, wired to
   playerManager; auto-hide.
3. 9.2 seek slider + scrub (+ trickplay preview image on scrub).
4. 9.3 pickers (audio/subtitle via osc_bridge data), chapters,
   skip-intro button parity.
5. 9.4 cutover flag + delete the lua OSC once field-proven.

### Progress log

**Prep ✅ (2026-07-19).** Playback glyphs added to the generated icon
set (40 → 51: replay_10/forward_30, closed_caption, audiotrack,
fullscreen/_exit, bookmark, hd, undo/redo, close — parity with what
trickplay-jf-osc.lua embeds). `draw_gradient` rewritten from 24
stacked alpha bands (visible banding — the exact failure the lua OSC's
comment warns about) to the OSC's blurred-box technique: one solid box,
gaussian `\blur` fading edge (blur=h/4, ramp midpoint at h/2.2 from
the dense end), oversized and clipped to the node rect. Verified
smooth on both fade directions over white.

**9.0 + 9.1 ✅ (2026-07-19).** The lifecycle inversion works over real
video on both backends (`tests/integration/test_mpvtk_hud.py`, in the
PER_BACKEND_REAL leg):

- Renderer: `mpvtk-hud yes|no` message; state `phud` {mode, shown}.
  Idle = blank scene + summon catchers (arrows/ENTER forced bindings +
  mouse-move delta in the mouse-pos observer) — every other key keeps
  its mpv default. Summon = `ui_resume()` (the extracted mpvtk-active
  'yes' body) + ESC binding (steps out popup → menu/dialog → hide) +
  `{t=hud, active=true}` to Python. Auto-hide: 4s renderer timer,
  reset by phud_touch() hooks in on_mouse_move/down, on_wheel,
  nav_move/activate; expiry re-arms instead of hiding while a popup/
  modal/drag/adjust is live or the video is PAUSED. `mpvtk-active`
  (either direction) leaves HUD mode entirely (phud_clear).
- Focus: `autofocus=True` widget flag → node `af`; a key/remote summon
  records want_focus and the first pushed HUD scene lands spatial-nav
  focus on the af node (play/pause). Mouse summons don't steal focus.
- Remote: renderer mirrors `user-data/mpvtk/hud`; player.menu_action
  routes Move*/Select as keypresses while EITHER the UI owns input or
  the HUD is idle (so the first remote press summons); Back keeps
  stop-to-browser while hidden, hides the HUD while shown.
- Browser: `_use_hud()` (controller.use_hud → osc_style "mpvtk") picks
  set_hud(True) in `_yield`; on_hud flips `_hud_shown`, primes a fresh
  playstate and starts the shared 1s ticker (now-playing ticker keeps
  running while `_hud_shown`). build() returns `hud.build_hud()` while
  yielded+shown. Video playstates are kept in `_hud_state` even while
  yielded (they used to be dropped).
- 9.1 bar (`mpvtk_browser/hud.py`): gradient scrim (2.2× bar height so
  the solid half covers the controls), title, seek Slider (force=True,
  1s-refreshed like the np bar), flat transport (prev/play-pause/next/
  stop) + "pos / total" clock. Wired through the same _PlayerController
  transport as the now-playing bar.
- Config: `osc_style: "mpvtk"` (falls back to jellyfin when browser_ui
  isn't mpvtk; keeps mpv's builtin OSC off in enable_osc). The lua OSC
  remains the default until field-proven (9.4 flips it).
- Test gotcha worth keeping: mpv applies script binding-section updates
  asynchronously, so a keypress issued immediately after a lifecycle
  transition can miss the fresh bindings — tests press-until-effect,
  which is also what a human does.

**9.2 ✅ (2026-07-19).** Scrub + trickplay preview:

- Slider grows commit/cancel semantics (toolkit-wide): 'change' fires
  throttled while the value is in flight, 'commit' once when the
  gesture ends (drag release / adjust toggled off), 'cancel' when it's
  abandoned (ESC in the HUD, or focus moving off the slider
  mid-adjust — renderer reverts to the scene value). `force=True` no
  longer stomps an in-flight gesture (sl_state skips the reset while
  the slider is being dragged/adjusted — this also fixes the np bar's
  thumb snapping back under the 1s ticker). np-seek and hud-seek are
  commit-only: scrubbing never spams seeks at a transcode; np-vol
  stays live on change.
- Trickplay: the TrickPlay worker now also stores its decoded bif
  metadata on `player.trickplay_meta` ({count, multiplier, width,
  height, file} — file is the same raw-BGRA frame dump the lua OSCs
  consume via shim-trickplay-bif); cleared on clear()/stop(). The HUD
  reads the frame for the scrub position straight out of the file
  (PIL raw BGRA decoder → strips.bitmap, one-slot cache keyed by
  frame index), floats it above the slider via node_rect('hud-seek')
  geometry feedback, and shows the pending target in the clock.
  Bitmap-over-ASS means the preview covers the bar — accepted in the
  plan. Chapter-image fallback (videos with chapter thumbs but no
  trickplay) is NOT wired — deferred with chapter ticks to 9.3.
- Layout gotcha for the record: a Slider's default w=180 defeats
  align="stretch" (stretch only sizes children with no fixed cross
  size) — wrap it in an unsized Row and flex inside.

**9.3 ✅ (2026-07-19).** Pickers, chapters, skip-intro:

- osc_bridge grows a public `build_state()` (same blob send_state
  pushes to the lua OSC); the controller exposes it as
  `hud_menu_state()` plus `hud_action(verb, arg)` — picker selections
  route through `osc_bridge.handle_action`, so a burn-in subtitle
  restarts the transcode exactly like the lua OSC's menus, and
  `chapters()` (mpv chapter-list).
- HUD right side: chapters / audio / subtitles / quality as
  icon-trigger Dropdowns (bookmark, audiotrack, closed_caption, hd),
  each only shown when there's a real choice; chapter select seeks to
  the chapter start; selection marked via force=True from the blob.
- Skip Intro/Credits: player.update() stores the promptable segment on
  `_hud_skip` when the HUD owns playback (same decision logic as the
  lua OSC's floating button, including skip_*_always auto-skip which
  stays player-side); push_playstate carries a localized `skip_label`;
  the HUD floats a white button above the bar's right edge
  (jellyfin-web placement). ~~PARITY GAP: the lua OSC's skip button
  appears even with the OSC hidden — the HUD's only while summoned.~~
  Closed in 9.3b (standalone idle overlay).
- Deferred still: chapter ticks on the slider track, chapter-snap
  while scrubbing, chapter-image preview fallback (videos with
  chapter thumbs but no trickplay).

**9.3b ✅ (2026-07-19, per Izzie).** Idle skip overlay + responsive
parity:

- **Standalone skip button while the HUD is idle** (closes the 9.3
  parity gap): player.update() pushes a playstate the moment a
  skippable segment starts/ends; the browser mirrors `skip_label`
  into the renderer (`mpvtk-hud-skip`), which — while idle — draws
  its own Skip Intro/Credits button (the blank-scene state means it
  can't come from a scene push) for ~6s. ENTER / remote Select skips
  (`{t=hudskip}` → hud_action skip-segment; the summon-ENTER binding
  is removed, not shadowed — deterministic), a click skips on the
  button and summons elsewhere, and while the segment lasts pointer
  movement re-shows the BUTTON rather than summoning the whole HUD
  (arrows still summon). Segment end or any lifecycle transition
  clears it.
- **Responsive shrink** (lua OSC parity): scale = clamp(w/900, .72, 1)
  on icons/pads/gaps; breakpoints drop ±10s/±30s step buttons and the
  clock below 500px, quality below 560px, chapter prev/next buttons +
  chapter dropdown below 700px. New transport buttons: replay_10 /
  forward_30 (hold-repeat, controller.seek_relative) and chapter
  prev/next (undo/redo icons, prev has the 2s re-seek grace like
  mpv's 'add chapter -1').
- **Chapter slits** on the seek bar: Slider gains `marks` (fractions);
  renderer draws 2×11px ticks, accent once passed, dim white ahead —
  render_jf_slider's treatment.
- Coverage: fast layout tests (tiers/marks/chapter-jump in
  test_mpvtk_browser_shell.TestPlaybackHudLayout), idle-overlay
  lifecycle on real mpv both backends (test_idle_skip_overlay), and a
  `{cmd=phud, action=mousemove}` debug hook because mouse-pos can't
  be injected under headless X.

**9.3c ✅ (2026-07-19, per Izzie).** Gear menu, favorite, lifecycle
hardening:

- **Settings gear menu** — the lua jf_settings_sheet rebuilt on the
  Menu widget (one level open at a time, `b._hud_menu` names it; Back
  row in submenus for keyboard/remote): Change Video Quality,
  Playback Speed (0.25–2x, mpv `speed` via controller), Aspect Ratio
  (Auto/16:9/4:3/2.35:1 via `video-aspect-override`), Change Video
  Playback Profile, Subtitle Size/Position/Color (the lua keeps these
  in its CC sheet; the HUD's sub picker is a flat Dropdown, so they
  live in the gear instead), SyncPlay (groups submenu; opening fires
  syncplay-refresh once — the cached groups land on a later 1s
  build), Playback Data (stats script-binding), Screenshot, Quit and
  Mark Unwatched. Root rows show "· current" asides. Renderer's menu
  geometry clamps + flips above near the bottom edge. Leaf actions
  route through the identical osc_bridge verbs as the lua sheet.
- **Favorite heart** in the transport (red when favorited, optimistic
  flip like the np bar, toggle-favorite verb; hidden <560px — the
  lua's show_fav tier).
- **Lifecycle hardening** (audit prompted by the old player-launch
  weirdness): two real bugs found and fixed. (1) mpv re-creation
  attaches a FRESH MpvtkApp but on_mpv_recreated only re-pointed
  `browser.app` — the new app's on_nav (pre-existing bug!) and
  on_hud/on_hud_skip callbacks were never wired, so nav mode and the
  whole HUD went dead after an idle-quit/crash re-open. Now
  `Browser.set_app()` (shared with __init__) rewires them and drops
  stale HUD state, and `reassert_window_state()` re-asserts
  browse-active / HUD-idle (video in flight) / fully-inactive on the
  fresh renderer. (2) Playback that starts while ALREADY yielded or
  minimized (cast, crash recovery) never entered HUD mode — `_yield`
  only runs on the browsing→video transition. on_playstate's video
  branch now idempotently re-engages `set_hud(True)` (the renderer
  dedupes) alongside the skip-label sync.
- Menu-nav quirk for the record: a freshly opened Menu has no
  highlight, so the first DOWN lands on row index 1 (clamp((nil or
  0)+1)); pre-existing, shared with tile context menus.

**9.3d/e ✅ (2026-07-19, per Izzie + subagent gap diff).** Top bar and
the remaining parity gaps a full lua-OSC-vs-HUD diff surfaced:

- **Top header** (9.3d): back arrow (yield to library — the
  stop_to_browser path shim-close maps to under the in-window UI),
  title on its own top-down scrim, SyncPlay groups button (accent
  tint while enabled) opening the SyncPlay sheet as a standalone
  drop-down (no Back row; the gear's copy keeps Back). Menus anchor
  to whichever button opened them; below top anchors, above bottom.
- **Volume** (9.3e): mute button (icon reflects level/mute; always
  shown, like the lua) + volume slider (live on change, ≥760px tier).
- **Fullscreen button**: toggles mpv fullscreen AND records intent
  via set_fullscreen (auto-fullscreen won't fight the choice) — the
  lua tog_fs behavior. Playstate now carries `fullscreen`.
- **Ends at HH:MM** (speed-adjusted wall clock, ≥1000px tier).
- **Clock click** toggles total ↔ negative-remaining (tc_right
  parity; per-session, browser-side flag).
- **Subtitle push-up**: controller.hud_sub_margin raises sub-margin-y
  to 130 while the HUD is up (skipped when sub-pos < 50), restored on
  hide and on the stopped path (the renderer clears without an
  on_hud(false) there).
- **Click-to-pause**: clicking bare video toggles pause both while
  the HUD is summoned (miss-everything path in on_mouse_down) and
  while idle (always-on mpvtk_phud_click binding, which also owns
  the standalone skip button's click).
- Icons: + volume_down, volume_off (53 total).

**9.3f ✅ (2026-07-19, per Izzie).** The two big deferrals landed:

- **Buffered/seekable-range shading**: push_playstate reads
  `demuxer-cache-state`'s seekable-ranges (seconds); hud.py maps
  them to fractions; `Slider(ranges=…)` → node `ranges`; draw_slider
  shades them white-at-40% between the track and the accent fill —
  render_jf_slider's treatment.
- **Passive-hover seek bubble**: sliders can opt into hover events
  (`Slider(on_hover/on_hover_end)` → node `hoverev`). The renderer
  reports the pointer-rest position throttled at 0.15s (same cadence
  as drag notifications — a preview, not a per-frame interaction;
  trickplay granularity is ~10s anyway) and one hover_end when the
  pointer moves off / leaves the window / the node leaves the scene.
  The browser floats the bubble there: dark rounded Box with the
  trickplay thumbnail over the chapter name + timestamp (text-only
  when the video has no tiles — same as the lua with no thumbfast).
  Scrub position takes precedence over hover for the bubble; the
  same bubble now also fronts the scrub preview, so scrubbing gained
  the chapter/timestamp labels too. Suppressed while dragging
  renderer-side (change events drive the bubble then).

**Deliberately NOT ported** (from the gap diff; revisit only if
field-proving misses them): millisecond clock mode, shift-click
frame-step / right-click coarse-seek variants on the step buttons
(mpv's default . , [ ] keys still work while idle), and
chapter/playlist OSD text lists on shift/right-click (the HUD has a
chapter dropdown; the queue lives in the browser).

**9.3g ✅ (2026-07-19, per Izzie — polish round).**

- Stop button dropped (the top bar's back arrow yields to the
  library).
- Accent styling: popup/menu hover rows use the pushed accent (was a
  hardcoded indigo); flat buttons and icon-trigger dropdowns hover as
  a round translucent accent circle + accent icon tint — the lua
  OSC's treatment (renderer: hover.circle on rects, hb/hc
  parent-hover tint on icons; custom-fg icons like the favorite
  heart keep their color, like the lua's icon_color override).
- Remote seek flow: a key/remote summon focuses the seek bar ACTIVE
  (autofocus slider → renderer enters adjust mode) — LEFT/RIGHT
  scrub immediately, UP/DOWN step off the bar, ENTER commits.
  ENTER-summon additionally toggles pause/play. Scrubbing pauses
  playback at gesture start and commit/cancel restores it (only if
  the scrub did the pausing). While the standalone skip overlay is
  up, ENTER skips and arrows still summon-with-active-bar.
- **Bug fixed**: ENTER-commit used to seek to the OLD position
  ("rejects my selection") — nav_activate cleared adjust mode before
  reading the value, so sl_state's force-reset snapped the slider
  back to the scene position mid-read. Commit now happens while
  adjust is still flagged busy.
- **Consequence handled**: with the bar always waking in adjust mode,
  nav_adjust had to leave the auto-hide busy-check (it would have
  pinned the HUD open forever); an actual scrub pauses playback,
  which already holds the HUD open. phud_hide also reverts a pending
  adjust gesture so the app resumes a scrub-pause cleanly.

**9.3h ✅ (2026-07-19, polish round 2 per Izzie).**

- Seek bar: accent outline on hover; **always-adjust** (`aadj` node
  flag / `Slider(always_adjust=True)`): live whenever focused —
  LEFT/RIGHT scrub, ENTER commits and stays live, UP/DOWN step off.
  New `nav_scrubbed` state distinguishes an in-flight gesture from a
  merely-focused bar (the force-reset busy-guard and ESC-cancel key
  off it; without it the idle thumb froze while focused). Focused
  aadj bars ring accent (white stays the explicit-adjust signal for
  ordinary sliders). Stop button removed (top-bar back covers it).
- **Keyboard policy** (the "don't grab my keys" defaults):
  `hud_grab_keys` (default False) + `hud_wake_key` (default ENTER).
  Idle grabs ONLY the wake key (ENTER also pause-toggles on wake);
  arrows keep mpv defaults unless grab is enabled. The policy rides
  the `mpvtk-hud yes {json}` message. Remote Move*/Select route via a
  new `mpvtk-hud-summon nav|select` script message (keypresses would
  hit mpv defaults), 'select' accepting a showing skip overlay.
- The `c` OSD menu is retired during playback under the HUD:
  `playerManager.on_hud_menu` (wired to Browser.open_hud_menu) opens
  the gear menu (toggle on repeat press); OSD menu remains for
  CLI/tk/non-video surfaces.

**9.4 ✅ (2026-07-19) — the lua OSC is GONE.** trickplay-jf-osc.lua,
gen_osc_icons.py, tools/osc-test/ and test_jf_osc_script.py are
deleted. `osc_style` defaults to "mpvtk"; "jellyfin" is a legacy
alias (resolution in player._init_mpv, stored as
`_osc_style_resolved` — c-menu routing, enable_osc and the skip
path key off it; browser_ui≠mpvtk falls back to "mpv",
thumbnail_osc_builtin=False still opts out to "default").
osc_bridge kept build_state()+handle_action() (the HUD's surface)
and lost send_state/active/update_skip_button + the
shim-jf-osc-*/shim-close client-message branches. HUD seeks stamp
`_last_ui_seek_time` directly (the seek-to-skip exemption the lua
requested by message). trickplay-osc.lua ("mpv" style) and
thumbfast.lua remain. The
flag shipped with 9.0: `osc_style: "mpvtk"` (README, settings-page
enum "In-window HUD (experimental)", conf.py docs; falls back to
jellyfin without the mpvtk browser; keeps mpv's builtin OSC off).
Default remains "jellyfin". What's left is deliberately NOT code:
Izzie field-tests the HUD against real servers/content, then in a
later change the default flips and trickplay-jf-osc.lua +
osc_bridge's lua-side plumbing (send_state gating, skip-button
script-messages) get deleted. Things to watch while field-proving:
scrim opacity over bright content, OSD-menu (`menu.py`) overlap while
the HUD is up, picker popup usability at 10ft, summon latency on slow
IPC, the idle skip overlay's timing/placement over real intros.

## Cross-cutting risks & open questions

- **Spatial/remote navigation** is *net-new scope* beyond Tk parity
  (see Phase 8). It's the main payoff of rendering in mpv, but the Tk
  UI is mouse-first, so it's additive, not a parity requirement.
- **Dialog backdrop** cannot dim posters (bitmaps > ASS). Accepted: no
  dim (GUIDE §6). Confirm this reads acceptably on the real detail
  view's backdrop.
- **Scroll stall** (PARITY open issue) — intermittent ~1s hit-test
  dropout at very fast wheel rates, mitigated by gesture stickiness,
  root cause unconfirmed. Watch for it with real dense grids; the F12
  HUD is the diagnostic.
- **Text input on X11/CJK** — no IME (Wayland/Windows OK). Affects
  search + login on X11 CJK only; accepted loss.
- **`filedialog`** (download-dir picker) → path TextBox. Accepted loss.
- **Window ownership contention**: the OSD menu (`menu.py`) and the
  OSC lua also draw on the player's window. Ensure the browser overlay
  and these don't fight — likely the browser suppresses the OSD
  menu/OSC while active and vice-versa.

---

# Cutover checklist — deleting the Tk browser

This is the section to work from when the Tk browser is actually
removed. Everything above is the historical execution log; this is the
current state and the mechanical steps.

Parity was closed by a set of code:code audits comparing
`library_browser/` + `gui_mgr.py` against `mpvtk_browser/`, followed by
field testing. Each item below was fixed with a regression test that was
verified to fail against the pre-fix code.

## What the audits found (all fixed)

The valuable finding is the *class* of bug: mpvtk looked like Tk but had
different semantics, and the difference was invisible until it reached
the server or the player.

- **Track defaults.** `media.map_streams` treats a browser selection as
  final (`explicit_tracks`) *because* "its pickers already defaulted to
  language_config (then the server default)". mpvtk never did that
  resolution, so its subtitle picker read "None" — and picking any
  track sent `sid=None` as a deliberate choice, so subtitles came up off
  and `remember_subtitle_track` pinned that for the whole queue.
- **`_is_watched` inverted.** `(count or 0) == 0` reads a *missing*
  `UnplayedItemCount` as fully watched, so an untouched Series showed a
  tick and the first click marked it unwatched.
- **New playlists were public.** `is_public` omitted; the server default
  is public.
- **Downloads: the flat group's Remove wiped the whole catalog.** No
  scope was passed, and `syncManager.delete()` treated "no scope" as
  "everything" — behind a prompt naming one group. `delete()` now
  refuses an unscoped call.
- **Offline marks were silently dropped**, with optimistic UI on top.
- **Route failures spun forever** — `run_async` had no error path, so an
  unreachable server was indistinguishable from a hang.
- **Playlist clicks queued the series**, not the playlist, and dropped
  the resume offset.
- **Queue skip did nothing**: `Media.get_from_key` matched item `Id`,
  the queue view addresses entries by `PlaylistItemId`.

Two rendering bugs worth remembering, both in `mpvtk/`:

- **Double wrapping.** The layout engine wraps text and positions each
  line, but the ASS events carried no `\q` tag, so libass applied its own
  smart wrapping on top. Our line breaks were never authoritative. Fixed
  with `\q2`; a static test guards new text emitters.
- **Virtualized tables pinned `min_w` to their widest content**, so one
  long song title pinned a table wider than the window, and `min_w` being
  a floor meant it could never shrink back.

## Accepted losses (decide, then record in the release notes)

These are Tk features that are **not** coming across. None blocked
field testing, but the deletion commit should say so out loud:

- **Text input IME on X11/CJK.** Wayland/Windows are fine.
- **Native file dialog** for the download directory → a path TextBox.
- **Dialog backdrop dimming.** Bitmaps composite above ASS, so a dialog
  cannot dim the posters behind it (GUIDE §6).
- **The first-close preference prompt.** Tk asked once, on the first
  window close, whether to minimise to tray or quit. Deliberately not
  ported: with one shared mpv window we would have to awkwardly re-open
  the window just to show the modal. Instead the choice is made obvious
  in Settings -> Interface ("Close to Tray (keep running)") and defaults
  to the harmless option — minimise, not quit.

Two settings were deleted rather than ported, because mpvtk displayed
them and never read them: `library_page_size`, `library_image_width`.
Unknown keys in an existing `config.json` are ignored on load, so no
user config breaks.

## Deletion steps

1. **Delete** `jellyfin_mpv_shim/library_browser/` (≈5 800 lines) and
   `jellyfin_mpv_shim/gui_mgr.py` (≈1 370 lines).
2. **Do not delete `mpvtk_browser/repository.py`** — it is the shared
   data layer and `library_browser/app.py` imports it *from* mpvtk, not
   the other way round. Nothing in the data layer is Tk-specific.
3. **Two modules live under `library_browser/` but are not the browser**
   and need a decision, not a reflex delete:
   - `library_browser/icons.py` — used by `gen_ui_icons.py` (the build
     script that rasterizes Material icons) and `tests/test_icons.py`.
     mpvtk draws icons as ASS vectors (`mpvtk/vector.py`) and does not
     need it. Move it next to `gen_ui_icons.py` or delete both together.
   - `library_browser/thumbnails.py` — its `MemoryCache` is what
     `tests/test_thumbnail_cache.py` exercises. `mpvtk_browser/
     thumbnails.py` has its own. Port the test or drop it deliberately.
4. **`mpv_shim.py:main`** — remove the Tk fallback branch so the mpvtk
   import failing is a hard error rather than a silent downgrade to a UI
   that no longer exists.
5. **`conf.py`** — `browser_ui` becomes dead. Either drop the key (and
   its `ENUMS`/`SECTIONS`/`LABEL_OVERRIDES` entries in
   `mpvtk_browser/config.py`) or keep it as a one-value enum. Check
   `player.py:358` and `player.py:776`, which branch on it.
6. **Tray/IPC.** `gui_mgr.UserInterface` is the *Tk path's* tray owner
   and IPC hub; `mpvtk_browser/ui.py:UserInterface` is the mpvtk one and
   owns `tray.py` directly. Confirm nothing else reaches into gui_mgr —
   `log_utils.py`, `update_check.py`, `users.py`, `player.py` and
   `tray.py` all reference it today.
7. **Tests.** These reference the Tk path and need porting or removing:
   `test_close_to_tray.py`, `test_thumbnail_cache.py`, `test_view_epoch.py`,
   `test_playlist_edit.py`, `test_icons.py`, `test_update_check.py`,
   `integration/test_lifecycle.py`, `integration/test_browser_ui.py`.
8. **Docs.** `README.md`'s `browser_ui` entry, `PARITY.md`, and the
   framing of this file.

## Known issue, unrelated to cutover

The **integration suite fails when run whole** (~12–17 real-mpv tests
report "renderer never became ready"), while the same modules pass in
isolation. Reproduced against a stashed tree with none of the parity
work applied, so it predates it — resource contention from many mpv
instances in one process, not a product bug. It makes
`python3 -m unittest discover tests/integration` untrustworthy as a
whole-run gate; run the modules in groups until it is fixed. Worth
fixing before the cutover commit, so the deletion can be validated by a
green full run.

# Deferred cleanups in `mpvtk_browser/` — all done

Carried over from `SPLIT_PLAN.md` when `app.py` was split into mixins, and
worked through in the commits that follow the split. Kept as a record of
what was found, because four of the ten were not cleanups at all:

- **Epoch dropped rollbacks.** `run_async` gated `on_error` on the epoch the
  same way it gates `on_done`, so navigating away while an optimistic edit
  was in flight discarded the rollback — the route dict kept a change the
  server had refused and showed it again on the way back. `on_error` is now
  ungated (it targets a captured dict, not the screen); `on_done` still is.
- **The music tab could stop paging.** Of the three copied infinite-scroll
  pagers, the music one had no `on_error`, so a failed page left `_loading`
  set and that tab never requested another page for the rest of the session.
  Two of the three also paged from an empty list, re-running the initial
  load. All three are now one `_page_more`.
- **Two pollers could start at once.** The starters were check-then-act and
  reachable from more than one thread. `_start_daemon` makes it atomic.
- **The download-folder move held a pool worker** for as long as the copy
  took, with route loads queued behind it. Long jobs get their own thread.

The rest were what they looked like: `_np_stop` renamed to `_shutdown_evt`
(four threads sleep on it), one dispatch table per view instead of an elif
chain and a distant dict, the Pillow helpers moved out of the optional
`display_mirror` into `imageutil`, `thumbs.set_notify()` instead of poking a
private, a test that cross-checks the constants duplicated in `renderer.lua`,
and the downloads display tree moved out of the player bridge — where it had
been untestable without a live `syncManager`, and now has 21 tests.

## Testing discipline this UI keeps punishing

Five times during the parity work, code existed, was committed, was
described as done — and never reached the screen, with the suite green
throughout: `_scenes_row` was never called from `_render_detail`;
`collection_remove` / `collection_new` had zero call sites; create-collection
was reachable only from a button gated on already *having* collections;
playlist "Remove Download" was wired to a predicate that could never return
True for a playlist; and `self.status` was written from 14 places but
rendered in 1, so edit-failure messages were invisible on every screen where
edits happen.

The shape is always the same: **assert on the helper, stub the layer
beneath, never assert on what a user would see.** So:

> A test for a helper is only worth having alongside a build-to-scene test
> proving the helper is wired.

Related: a fix's error path can be **inert** because a lower layer swallows.
`_edit()` logged-and-returned, which silently defeated every caller's
`on_error` — including a delete-rollback whose test passed because it
stubbed the controller and bypassed `_edit` entirely. `_edit`,
`queue_reorder` and `playlist_move_many` now raise deliberately; the
comments say why.

**Revert-check every fix.** After making a change and adding tests, revert
the change and confirm the new tests fail. That caught several tests which
passed against broken code, and found more real problems than the suite did.
