# local-ui regression checklist

Tested on: 2026-07-07
Additional items tested on: 2026-07-13

Hand-testing pass for the `local-ui` branch (audit fixes + offline sync +
library browser + mpv-lifecycle). Ordered by risk × how often the path runs.

Two automated layers already cover a lot — run them first, they're fast and
catch most regressions before hand-testing:
- **Fast unit suite** (pure logic): `python3 -m unittest discover tests`
- **Integration + concurrency matrix** (fake mpv, real mpv under xvfb, both
  backends, deterministic race tests): `python3 tests/integration/run_integration.py`

The flows below still need a **real server and player** — the automated suites
don't exercise a live Jellyfin, real casting, or a real window on your hardware.

**Test key flows under BOTH mpv backends** — libmpv (default) and external mpv
(`mpv_ext: true`). External is the historically under-tested path and several
bugs were backend-specific; the automated matrix covers both, but confirm the
real flows (esp. auto-advance, close/recast, idle-quit) on each.

# Legend

[ ] Not tested yet
[-] Didn't bother testing
[X] Test pass
[*] Test had issues (subnote explains)

# REGULAR MPV

## Highest risk × frequency

### 1. Auto-advance between episodes (online)
Most-touched path: `finished_callback`, `_video` snapshot, playback epoch, EOF detection.
- [X] Multi-episode queue plays straight through; each advances and reports progress.
- [X] **Last episode in a queue** played to the very end gets marked watched (it ends via `playback-abort`, not `eof-reached` — the case the EOF fix targets). Test with `force_set_played` **on** and **off**.
- [X] Manual next/prev/skip reports the **actual** position (not full duration) — intended change.
- [X] The "mark watched" keybind still fully-marks an episode as before.

### 2. Cast / remote-control onto an already-playing shim
Targets the cast-while-playing race (epoch + `wait_property` stale-value fix).
- [X] Cast a new item while something is playing → plays the **right** item at the **right** resume position (not the old file seeked to the new offset).
- [X] Cast item is not auto-skipped by a stale finished-callback.

### 3. Close the mpv window (OSC 'x') mid-playback
Teardown moved from mpv's event thread to a queued action-thread task.
- [X] Closing mid-playback reports a stop (session clears from the Jellyfin dashboard).
- [X] Closing while paused behaves the same.
- [X] Closing with the server briefly unreachable doesn't hang or leave a zombie.

### 4. Server reconnect after a network drop
Client-lifecycle locking + the dead-code health-check reconnect fix (was fully broken before).
- [X] Drop the network / stop Jellyfin, wait past a health-check interval, restore → remote control & casting come back **without an app restart**.
- [X] App shutdown **while a server is unreachable** exits promptly (no ~100s hang).
- [X] Two servers configured, one down → the healthy one stays responsive while the other retries.

## Major new / changed surface

### 5. SyncPlay group leave / rejoin
Scheduled-command timing fixes; hard to reason about without exercising.
- [X] **Join a group that is already playing** → no crash (this hit an
  `AttributeError` on the missing `_rearm_sync` — the "Playing Now" path), and
  unpause / skip-to-sync re-arm work.
- [X] Leave a group mid-playback → no phantom pause/seek fires afterward.
- [X] Group leader pause-then-quick-unpause → player isn't yanked to a stale position.
- [-] Leave group 1, join group 2 → no group-1 timing bleeds into group 2.

### 5b. MPV process lifecycle (close / re-open / idle-quit) — new this branch
The re-open path was rebuilt; the big fix was draining the outgoing mpv's
stale queued tasks so a re-opened player still auto-advances. Test on **both
backends**.
- [X] **Close the mpv window (OSC 'x') mid-playback, then cast/Play again** →
  it re-opens, plays, AND the next episode **auto-advances** on EOF. (The
  stale-queue bug specifically broke auto-advance after any re-open — this is
  the headline regression to confirm, and likely resolves #458.)
- [X] Close mpv while paused, then re-cast → same clean re-open.
- [X] Close mpv with the server briefly unreachable, then re-cast → no hang,
  correct re-open, session reported.
- [X] **idle-quit** (opt-in): set `mpv_idle_quit: true` and a short
  `mpv_idle_quit_secs`; let it idle out → mpv quits (window/process gone,
  resources freed). Then cast → re-opens, plays, auto-advances. Verify on
  libmpv AND external mpv.
- [X] idle-quit does **not** fire while: something is playing, the menu is
  open, a SyncPlay group is active, the display-mirror window is up, or mpv is
  a **user-launched** external one (`mpv_ext_start: false`).
- [X] Repeated close→reopen cycles → no leaked trickplay threads / no growth,
  process still exits cleanly on quit.

### 6. Offline download lifecycle (biggest new-code area)
- [X] Queue a season → files land, items show complete.
- [X] **Delete an item mid-download** → it stops and cleans up (no orphan file left as "complete").
- [X] **Interrupt** a download (kill app / drop network mid-download) then relaunch → **resumes** from `.part`, doesn't restart or error out.
- [-] Disk full during a download (if simulable) → worker survives, other downloads not wedged.
- [X] Delete the download folder under a "complete" item, relaunch → startup reconcile requeues it (no dead path handed to mpv).
- [X] Queue against a down server → doesn't busy-spin CPU; other server's playstate still syncs.

### 6b. Change the download folder (Settings → Downloads → Browse…) — new this branch
- [X] Change the folder with **no downloads yet** → takes effect, new downloads land in the new folder.
- [X] Change with **existing downloads to another drive** → progress bar advances, Save disabled during the move, UI/tray stay responsive (no "not responding"), files + `catalog.db` end up at the new path, old folder gone, downloads still play.
- [X] Change to a folder that **already has a `catalog.db`** → refused with a message, nothing moved.
- [X] Try to change **while a download is actively transferring** → refused; existing queue untouched.
- [X] Clear the folder (blank) → resets to the default `<config>/offline`, moving any downloads back.
- [X] Restart after a move → downloads still present at the new folder (path persisted).
- [X] After a successful move → a **"Restart required"** prompt appears (the browser keeps the old catalog wiring for live progress until restart). Downloading before restarting shows no progress bar — known, hence the prompt.

### 7. Offline playback
- [X] Fully offline / `work_offline`: play a downloaded item to the end.
- [X] Auto-advance to a **non-downloaded** next episode → "Next episode is not downloaded", stops gracefully (no crash).
- [X] Kill the app mid-episode offline, relaunch → resume position was saved (periodic 30s record).
- [X] Watch offline, come back online → watched state / position sync back to the server.
- [X] "Delete watched" after watching offline → deletes the items actually watched offline.

### 8. Single instance
- [X] Launch twice → second launch raises the existing window, no duplicate.
- [X] Running in the systray, launch again → surfaces rather than duplicating.
- [X] Two instances with different `--config` dirs both run.
- [X] Kill the app uncleanly, relaunch → not blocked by a stale lock.

## Lighter touches

### 9. Library browser under load
- [X] Fast-scroll a large library, change sort mid-scroll, navigate away while a page/thumbnails load → no duplicated/misordered tiles, no stuck "Failed to load".
- [X] Long browse session → memory doesn't balloon (thumbnail-cache byte bound).
- [X] Open DownloadsPanel during an active season download → updates smoothly, progress % ticks, no flicker.
- [X] Server switcher with two same-named servers → both selectable.

### 10. In-player track menus
- [X] With a language filter set, open audio/subtitle menu → highlighted row matches the actually-selected track.

# EXTERNAL MPV

## Highest risk × frequency

### 1. Auto-advance between episodes (online)
Most-touched path: `finished_callback`, `_video` snapshot, playback epoch, EOF detection.
- [X] Multi-episode queue plays straight through; each advances and reports progress.
- [X] **Last episode in a queue** played to the very end gets marked watched (it ends via `playback-abort`, not `eof-reached` — the case the EOF fix targets). Test with `force_set_played` **on** and **off**.
- [X] Manual next/prev/skip reports the **actual** position (not full duration) — intended change.
- [X] The "mark watched" keybind still fully-marks an episode as before.

### 2. Cast / remote-control onto an already-playing shim
Targets the cast-while-playing race (epoch + `wait_property` stale-value fix).
- [X] Cast a new item while something is playing → plays the **right** item at the **right** resume position (not the old file seeked to the new offset).
- [X] Cast item is not auto-skipped by a stale finished-callback.

### 3. Close the mpv window (OSC 'x') mid-playback
Teardown moved from mpv's event thread to a queued action-thread task.
- [X] Closing mid-playback reports a stop (session clears from the Jellyfin dashboard).
- [X] Closing while paused behaves the same.
- [X] Closing with the server briefly unreachable doesn't hang or leave a zombie.

### 4. Server reconnect after a network drop
Client-lifecycle locking + the dead-code health-check reconnect fix (was fully broken before).
- [X] Drop the network / stop Jellyfin, wait past a health-check interval, restore → remote control & casting come back **without an app restart**.
- [X] App shutdown **while a server is unreachable** exits promptly (no ~100s hang).
- [X] Two servers configured, one down → the healthy one stays responsive while the other retries.

## Major new / changed surface

### 5b. MPV process lifecycle (close / re-open / idle-quit) — new this branch
The re-open path was rebuilt; the big fix was draining the outgoing mpv's
stale queued tasks so a re-opened player still auto-advances. Test on **both
backends**.
- [X] **Close the mpv window (OSC 'x') mid-playback, then cast/Play again** →
  it re-opens, plays, AND the next episode **auto-advances** on EOF. (The
  stale-queue bug specifically broke auto-advance after any re-open — this is
  the headline regression to confirm, and likely resolves #458.)
- [X] Close mpv while paused, then re-cast → same clean re-open.
- [X] Close mpv with the server briefly unreachable, then re-cast → no hang,
  correct re-open, session reported.
- [X] **idle-quit** (opt-in): set `mpv_idle_quit: true` and a short
  `mpv_idle_quit_secs`; let it idle out → mpv quits (window/process gone,
  resources freed). Then cast → re-opens, plays, auto-advances. Verify on
  libmpv AND external mpv.
- [X] idle-quit does **not** fire while: something is playing, the menu is
  open, a SyncPlay group is active, the display-mirror window is up, or mpv is
  a **user-launched** external one (`mpv_ext_start: false`).
- [X] Repeated close→reopen cycles → no leaked trickplay threads / no growth,
  process still exits cleanly on quit.

### 6. UI-review fixes (2026-07) — hand-test items
Multi-angle review of the browser/gui layer; the pure-logic pieces are covered
by `tests/test_ui_review_fixes.py`, these need a live session.
- [ ] **Switch spam**: start a switch to user A (slow server helps), then pick
  locked user B from the switcher and enter the PIN → the dialog shows
  "Another user switch is already in progress." and closes cleanly; the window
  never wedges behind the modal.
- [ ] **Failed switch recovery**: delete a user from another window right
  before switching to them → error message, and the UI lands back on
  home/login instead of an eternal "Connecting…" spinner.
- [ ] **Add Server during a switch**: kick off Add Server against a slow
  server, switch users while it authenticates → the new server appears under
  the ORIGINAL user (check users.json), not the one you switched to.
- [ ] **Quick Connect twice**: start QC on one server, then start QC on
  another → the first flow is cancelled (its late authorization does not yank
  the UI to Home); Cancel always kills the visible flow.
- [ ] **Server drop while browsing**: with two servers, kill one while
  scrolled into its library grid → artwork/lazy-load keep working (tiles show
  placeholders; no wedged scroll), no traceback storm in the log.
- [ ] **First-page load failure**: open a library while the network blips →
  status line reads "Failed to load — click here to retry." and clicking it
  reloads.
- [ ] **Offline watched state**: offline, a fully-watched downloaded series
  shows the ✓ badge and "Mark unwatched"; marking a series watched offline
  marks its downloaded episodes and syncs to the server on reconnect.
- [ ] **Backdrop cache**: open an item's detail offline, reconnect, reopen →
  the online backdrop replaces the offline one (no stale header art).
- [ ] **Browser crash race**: kill -9 the browser process, immediately click
  the tray's Show → exactly one working window; no orphaned unreachable one.

Note: This batch deferred until better offline detection while browsing logic is implemented.

### 7. jellyfin-web parity batch (2026-07) — hand-test items
Filters/favorites/latest rows/shuffle (batch A), detail-page upgrades (batch
B), grouped search + A–Z (batch C), and browser-side SyncPlay join. Pure
logic is covered by `tests/test_browser_features.py`.
- [X] **Filters**: in a library grid, Unplayed / Favorites / Genre combine
  correctly with every sort and with infinite scroll; totals match; offline
  the same filters work against downloads.
- [X] **A–Z strip**: jumping to a letter filters (`#` = non-alphabetic);
  clicking the active letter clears it.
- [X] **Favorites**: right-click add/remove on tiles + the detail/series
  button stick server-side (check in jellyfin-web); Favorites filter then
  shows them.
- [X] **Home**: per-library "Latest in X" rows appear (replacing the two
  global Recently Added rows) and match jellyfin-web's home. Row orientation is
  by **library CollectionType**, not item type: Movies / TV Shows / boxsets →
  **posters** (a TV row that mixes grouped Series with stray recently-added
  Episodes stays poster — the bug was one Episode flipping the whole row
  landscape); home-video / misc (Type=Video/MusicVideo) libraries → **landscape**
  cards.
- [X] **Shuffle**: library-grid Shuffle plays a random queue spanning the
  whole library (not just loaded pages); series Shuffle shuffles episodes;
  offline shuffle plays only downloads.
- [X] **Detail page**: cast row renders with photos and clicking a person
  opens their filmography; multi-version items show the Version picker and
  the track pickers re-source on change; media-info line + "Ends at" look
  right; Scenes row plays from the chapter offset (thumbnails online,
  text-only offline).
- [X] **Series page cast + similar**: the show overview page (SeriesView) now
  shows the **Cast & Crew** and **More Like This** rows too (previously
  movies-only); person tiles open the filmography, similar tiles open the show.
- [X] **Search**: results grouped Movies / Shows / Episodes / Videos.
- [X] **SyncPlay**: with nothing playing, top-bar SyncPlay → groups list →
  Join starts playback of the group's queue in mpv and stays in sync; Leave
  works; joining a group on server B while in a group on server A leaves A
  first; the button politely refuses offline.

### 8. Playlist & collection editing (branch local-ui-playlist-edit) — hand-test items
Needs jellyfin-apiclient-python >= 1.15 (branch add-browse-edit-apis);
with an older apiclient every edit affordance must be hidden.
- [X] **Bulk remove**: playlist → ✏ Edit → shift-click a whole show's worth
  of episodes → Remove selected → ONE call, all gone server-side (verify in
  jf-web). The 48-clicks problem this exists to fix.
- [X] **Block moves**: select contiguous and non-contiguous sets; Top / Up /
  Down / Bottom land in the same order in jf-web after a refresh (the
  sequential-replay invariant is unit-tested; verify a real server agrees).
- [X] **Unsupported entries**: a playlist with music entries shows them in
  the editor (type column) and they can be removed. The ✏ Edit button is
  still offered when a playlist holds ONLY unsupported entries (no Play All),
  so the strays can be cleaned out.
- [X] **Shuffle playlist**: playlist → 🔀 Shuffle plays the supported items
  in a random order (Play All keeps playlist order).
- [X] **Rename**: ✏ Edit → ✎ Rename → new name applies (verify in jf-web and
  that the tile/title updates); empty or unchanged name is a no-op.
- [X] **Public/Private**: ✏ Edit → the Public checkbox reflects the server's
  current visibility (loads before it's enabled); toggling it makes the
  playlist visible to all users / owner-only (verify with a second user).
- [X] **Delete playlist**: ✏ Edit → 🗑 Delete playlist → confirm → the
  playlist is gone in jf-web (videos untouched), and the browser drops back to
  the playlist list (not a dead editor/detail view). Cancel does nothing; a
  server refusal shows an error and keeps the editor.
- [X] **Add-dialog modes**: Add to playlist… → with the name box empty the
  primary button says **Add** (adds to the highlighted playlist) and no
  Private box shows; typing a name flips it to **Create new** and reveals the
  Private box (default checked). Empty box + nothing selected → Add is a safe
  no-op. Unchecking Private creates a public playlist.
- [X] **Quick remove**: right-click an item inside a playlist → Remove from
  playlist (single entry, no editor).
- [X] **Add to playlist**: right-click any tile → Add to playlist… → picker
  lists playlists; adding a SERIES expands to its episodes server-side;
  Create new seeds a playlist with the item.
- [X] **Collections toggle**: a **Collections** checkbox appears next to
  Favorites on **Movie** libraries only (not TV/other, not offline). Checking
  it switches the grid to list the server's Collections (BoxSets, explicit
  IncludeItemTypes=BoxSet request); clicking one opens the collection; sort/A–Z
  apply. Unchecking returns to the movie list. No client-side exclusion — the
  main movie request renders whatever the server groups.
- [X] **Collections**: Add to collection… on movie/series tiles; inside a
  collection grid, right-click → Remove from collection refreshes the grid;
  Create new makes the collection (may need a library scan to appear as a
  tile — that's a Jellyfin quirk, not a bug here).
- [-] **Failure paths**: pull the network mid-edit → error message and the
  editor reloads the server's real order; offline mode shows no edit
  affordances at all.
  - Offline detection mode switching currently needs more work, current
    assumption is users will restart on offline if not detected.

### 9. Music — Phase A (playlists + now-playing bar) — hand-test items
Phase A plays/queues/downloads music **via playlists only** (no album/artist
browse yet — that's Phase B). Needs a live server.
- [X] **Music playlist plays**: a playlist with Audio no longer shows "no
  supported media"; tiles appear, clicking a track plays the whole playlist as
  a queue from that track; Play All / Shuffle work.
- [X] **No album-art window**: audio playback does NOT pop an mpv window
  showing embedded cover art (audio-display=no). Video/music-videos unaffected.
- [X] **Now-playing bar**: a bottom bar appears while AUDIO plays (and only
  audio — hidden for video): shows title — artist; ⏮ ⏯ ⏭, seek slider that
  moves smoothly and scrubs on release, volume slider, ♥ favorite (persists to
  server), 🔁 repeat. Bar hides on stop / end of queue.
- [X] **Repeat**: none → all (queue wraps at end) → one (current track loops
  via mpv) → none. Heart/repeat glyphs reflect state; RepeatMode shows in the
  Jellyfin dashboard.
- [X] **Transport**: prev/next move within the queue; play/pause toggles and
  the glyph flips; the bar updates within ~5s even for changes made elsewhere
  (remote/keys).
- [X] **Download a music playlist**: ⬇ Download Playlist grabs the Audio
  tracks as ONE unit (one "Playlist: X" block in Downloads, not N songs);
  offline, the playlist plays its downloaded tracks. **(probe: offline audio
  container/playback — flagged risk.)**
- [X] **Music playlist = tabular track list**: a playlist containing ANY Audio
  renders a tabular list (row per track: small album art, position, title,
  artist, duration), NOT tiles; clicking a row plays the playlist from that
  track. Non-music playlists still use tiles.
- [X] **Per-type volume persists**: set music volume via the bar, restart →
  it's remembered; video volume is tracked separately (change video volume via
  mpv keys, restart → video remembers its own level, music unaffected). Volume
  slider click-to-set works like seek.
- [X] **Downloads scale**: downloading a music playlist shows ONE "Playlist: X
  · N of M · size" line (no per-song rows, no flicker); with 100+ separate
  downloads, completing one only rebuilds its section, not the whole list.
- [X] **Art placeholders**: tiles with no server art (the Collections tile,
  most music) show a placeholder — a ♪ for audio, else the item's initial —
  instead of a blank rectangle; real art replaces it when present.
- [X] **Bar polish**: hovering a bar button shows a tooltip; clicking anywhere
  on the seek track jumps to that spot (not a few-second nudge); the slider no
  longer flashes white on hover.
- [X] **Repeat is music-only**: set repeat one/all during music, then play a
  VIDEO — the video must NOT loop and the queue must NOT wrap (repeat only
  applies while audio plays).