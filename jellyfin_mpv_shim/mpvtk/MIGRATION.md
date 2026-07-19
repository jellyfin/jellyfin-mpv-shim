# mpvtk migration plan — replace the Tk browser (and display mirror)

Living checklist for porting the Tkinter library browser (and, as a
bonus, the display mirror) onto **mpvtk**, the in-mpv UI toolkit. This
is the execution doc; read `GUIDE.md` (framework), `PARITY.md`
(component gap analysis) and `README.md` (spike log) first.

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

Real mpvtk limitations, not app bugs. Worth fixing in the toolkit.

1. **No way to draw ASS above a bitmap, and no z-order between bitmaps.**
   `overlay-add` composites above all script ASS (GUIDE §6), and the
   renderer's overlay *slots* are sticky/arbitrary, so bitmap-over-bitmap
   ordering isn't controllable either. Consequence: nothing can float over
   a poster strip — not the carousel arrows, not a hover scrim, not a
   badge added after the bake. Everything overlaying tiles must be baked
   into the strip. Possible fix: give the renderer a paint-order slot
   allocator and re-issue `overlay-add` in scene order when the order
   changes, which would at least buy bitmap-over-bitmap layering.
2. **No absolute positioning inside the flow.** `Float` is
   screen-absolute and never scrolls, so it can't pin anything to a row
   that scrolls with the page. A `Stack` container (children sharing a
   rect, per-child anchor) would cover this — but note (1) limits what it
   could usefully draw over.
3. **Scroll offsets live only in the renderer.** Virtualization has to
   round-trip through the debounced `on_scroll` event, so the Python side
   is always slightly behind and has to over-materialize (±1 screen) to
   hide the lag. A `scroll` field echoed back in `debug_state`, or a
   synchronous query, would let the app window tightly.
4. **No modifier state on click events.** The renderer reports a bare
   click, so shift/ctrl-click range and additive selection are impossible;
   the playlist editor uses toggle-select instead. Adding `mods` to the
   click payload is cheap and would make every table behave normally.
5. **No hold-repeat on buttons.** The Tk `NavButton` auto-repeated while
   held; mpvtk buttons fire once per click, so paging a long carousel is
   one click per page.
6. **`Text` can't wrap.** Every caller hand-wraps with `_wrap()` +
   `text_width()`. A `wrap=True` (and `max_lines`) on `Text` would remove
   that duplication from the browser, the mirror, and the dialogs.
7. **No table/column primitive.** Columns are hand-laid `Row`s with fixed
   `w=`, so headers and cells drift apart when one is edited.

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
Still inert: `start_minimized` / `close_to_tray` / `close_prompt_shown`
(there is no separate window to minimize or close-to-tray).

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
