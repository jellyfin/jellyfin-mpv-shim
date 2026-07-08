# local-ui regression checklist

Tested on: 2026-07-07

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
