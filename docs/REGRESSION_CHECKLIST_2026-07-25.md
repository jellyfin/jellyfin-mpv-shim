# Regression checklist — the `mpvtk-ui-refactor` branch

Tested on: 2026-07-25

Scope: ~50 commits of decomposition on top of `local-ui-mpvtk`. `player.py`
became `PlayerManager(AudioMixin, ReportingMixin, WindowMixin)` plus
`mpv_options.py`; the browser shell became `pages/`, `gateway/`,
`components/`, `Navigator`, `AsyncRunner`, `ScrollState`, `TileRenderer`,
`Paginator`, `ItemActions`, `HudController`, `LoadFeedback`,
`window_chrome`; `settings.py` became a package of per-tab mixins. Plus two
things that are **not** moves and therefore need their own attention: the
SyncPlay protocol fixes and the scroll-offset fix.

This list is deliberately much shorter than the 2026-07-13 one
(`docs/archive/`). That pass was acceptance-testing new features. This one is
checking that a decomposition changed nothing — a different question, with a
much narrower set of things worth a human's time. What was dropped and why is
at the bottom, so the cuts can be argued with.

## Run these first

- Unit suite (~16s): `xvfb-run -a python3 -m unittest discover tests`
- Integration matrix: `xvfb-run -a python3 tests/integration/run_integration.py`

**Use `xvfb-run` for both.** Importing `player.py` opens a real mpv window,
and eight unit modules import it.

## What the automation already proves

Knowing this is what makes the cuts below defensible:

- **`tests/test_scene_snapshots.py`** pins the exact node list `layout()`
  pushes to the renderer for seven screens. A matching snapshot is matching
  pixels — that is the property a decomposition needs and no behavioural test
  provides.
- **`tests/test_late_bound_calls.py`** statically resolves every late-bound
  call across the seam, so a method that moved and left a caller behind is a
  test failure, not a runtime `AttributeError` on a screen nobody opened.
- **`tests/test_page_contract.py`** holds every route to the `Page` contract
  and caps the `ctx.shell` escape hatch, which can only shrink.
- **`tests/test_mpv_options.py`** (33) covers the option dict without opening
  a window; **`tests/test_settings_mixins.py`** the settings package;
  **`tests/test_player_controller.py`** the browser↔player seam.
- **The integration matrix** runs the mpv-dependent modules once per backend
  in a fresh interpreter, so a libmpv-only or jsonipc-only break is reported
  per leg. This is what covers the `property_observer` bound-method
  divergence and the per-call backend imports.
- Each extraction was verified by an **AST leaf-statement diff** against the
  pre-move file: the moved code is the same statements, not a rewrite.

None of that sees a real server, a real audio device, a real window manager,
or a second client. That is what the rest of this document is.

# Legend

    [ ] Not tested yet
    [-] Didn't bother testing
    [X] Test pass
    [*] Test had issues (subnote explains)

Where a line reads `lib [ ] ext [ ]`, run it on **both** backends — libmpv
(default) and external mpv (`mpv_ext: true`). External is the historically
under-tested path and this branch moved code across that boundary.

# 1. Every route still draws — against a real library

Twenty-odd routes became `Page` classes. Snapshots cover seven of them, and
against fabricated data.

- [X] Walk each route once: home, library grid, person, detail, series,
      season, search, playlists, playlist editor, queue editor, music library
      (all five tabs), album, artist, genre, downloads, settings (all six
      tabs), cast screen.
- [X] **Then grep `log.txt` for `scene build failed`.** This is the important
      half. A build exception keeps the last good frame in production
      (`strict_builds` is off), so a broken route looks like a UI that simply
      did not respond to the click — it does not look like a crash. A silent
      entry here is a route that never rendered.
- [X] Right-click menus on tiles, and the tile menu's actions, on at least a
      movie, an episode, a series, an album and a track.

# 2. The player mixins — both backends

The three mixins share one object and one `RLock`, and each reaches outside
in ways only real hardware answers for.

## AudioMixin
- [-] Passthrough: set each `audio_mode` (auto / stereo / optical / HDMI) and
      confirm the output actually changes on your receiver — this is the one
      whose failure mode is silence, not an error.  lib [ ] ext [ ]
  - Lack hardware, although I can confirm it is setting the config
  - I get the following lines on my PC, but I don't have spdif output. Am suspicious of the codec parse failure for optical.
    2026-07-25 14:03:57,630 [ WARNING] mpv: ad: Failed to parse codec profile.
    2026-07-25 14:03:57,630 [   ERROR] mpv: swresample: unsupported conversion: spdif-ac3 -> floatp
    2026-07-25 14:03:57,630 [   ERROR] mpv: swresample: libswresample failed to initialize.
    2026-07-25 14:03:57,630 [   ERROR] mpv: af: Disabling filter jfac3 because it has failed.
    ALSA lib conf.c:5695:(snd_config_expand) Unknown parameters AES0=6,AES1=130,AES2=0,AES3=2
    ALSA lib pcm.c:2722:(snd_pcm_open_noupdate) Unknown PCM default:AES0=6,AES1=130,AES2=0,AES3=2
- [X] Night mode on/off during playback; per-type volume still remembered
      separately for music vs video across a restart.
- [X] A file whose audio track the profile can't do (DTS-HD on an optical
      path) still plays rather than failing to open.

## ReportingMixin
- [X] Progress appears and advances on the Jellyfin dashboard; stopping
      clears the session.  lib [X] ext [X]
- [X] Resume position is right after a stop mid-episode.
- [X] Discord Rich Presence still shows and clears (optional dep — also
      confirm the app is fine with `pypresence` absent).
  - Should move into main menu, also should report if checked but pypresence is absent.
## WindowMixin
- [X] Fullscreen toggle, `remember_window_size` across a restart, `raise_mpv`
      on cast.  lib [X] ext [X]
- [X] **Close the mpv window mid-playback, then cast again** → re-opens,
      plays, and the next episode auto-advances on EOF. The historic
      stale-queue bug; the highest-value single item on this page.
      lib [X] ext [X]
  - On EXT MPV when casted, when I click back in the UI it does close, but it briefly re-opens with a blank screen before closing again and staying closed. Cosmetic issue, doesn't cause any actual problems.
- [X] idle-quit (`mpv_idle_quit: true`, short `mpv_idle_quit_secs`): fires
      when idle, does **not** fire while playing / menu open / SyncPlay group
      active / cast screen up / user-launched external mpv.  lib [X] ext [X]

# 3. mpv option assembly

`build_mpv_options` moved wholesale, and the dict's insertion order is
load-bearing.

- [X] Each `osc_style`: `mpvtk` (default), `mpv`, `default` — the right
      controls appear and take input.  lib [X] ext [X]
- [X] Shader-pack profiles switch and actually apply (a visible profile, so a
      silent no-op is visible).
- [X] A user `mpv.conf` / `input.conf` in the config dir is still honoured,
      and a custom `mpv_ext_path` is still used.

# 4. Playback core — unchanged, but everything moved around it

`_play_media` / `update` / `finished_callback` were deliberately **not**
split. They are surrounded by moved code, so the paths through them still
want exercising.

- [X] Multi-episode queue plays straight through; each advances and reports.
      lib [X] ext [X]
- [X] Last episode played to the very end is marked watched (it ends via
      `playback-abort`, not `eof-reached`). With `force_set_played` on and off.
- [X] Cast a new item while something is playing → the **right** item at the
      **right** resume position, not the old file seeked to the new offset.
- [X] Server drops mid-playback and comes back → remote control and casting
      resume without an app restart.

# 5. Scrolling and pagination — a real fix, not a move

Fixed 2026-07-25 from the smoke test: a virtualized grid that left the scene
and came back was windowed around the offset it had before it left, while the
real container had been reset to the top — so it drew a screenful of blank
spacers. Covered by
`tests/test_shell_paging.py::TestAReturningScrollContainerStartsAtTheTop`, but
that models the renderer rather than being it.

- [X] **The reported repro**: music tab → tick Paginated → untick → tiles are
      there. Scroll deep first, which is what armed it.  lib [X] ext [X]
- [X] Same on a library grid, and on a person's filmography.
- [X] Scroll a library deep, change the sort → the grid comes back at the top
      with tiles, not blank. (The same defect by the other door: the reload
      drops to the busy screen, which takes the scroller with it.)
- [*] Scroll deep, open an item, come back → lands where you left it.
  - UI does NOT currently preserve scroll position on back-nav
- [X] Paginated mode itself: First / Previous / Next / Last, typing a page
      number, and that the page size follows a window resize.

# 6. SyncPlay

Five protocol fixes plus the ping fix. **Stress-tested 2026-07-25 with
repeated seeking across a group: solid, no misbehaviour.**

- [X] Seek in a group → every member resumes without anyone pressing play.
      The one that used to hang and get worked around by pausing and
      unpausing.
- [X] **`log.txt` has no `400 Client Error ... /SyncPlay/Ping`.** The 400 was
      the one problem the stress test surfaced: `PingRequestDto.Ping` is a
      `long` and the client sent a float, so every ping this client ever sent
      was rejected and the server compensated the group's unpause with its
      default latency instead of ours.
- [*] Another member stops the group → this client stops too **and is still
      in the group** (a later play from another member reaches it).
  - JF-web doesn't let me stop, just halt playback on the specific client
  - When I stop on mpv shim, it leaves the group, there is no resume local playback option
  - These semantics are honestly fine
- [X] Throttle the network so mpv stalls on its cache → the group waits for
      this client instead of leaving it behind and yanking it back.
- [X] Local pause, then local unpause → both reach the group. The unpause
      used to be swallowed intermittently.
- [X] Join a group that is already playing; leave a group mid-playback → no
      phantom pause/seek afterwards.

Known and **not** fixed: an ABBA lock inversion between `_lock` and `_tl_lock`
reachable only with SyncPlay enabled, which matches the historic hard hangs.
See `docs/archive/SYNCPLAY_FINDINGS.md` — it wants a redesign of how timeline
validity is enforced, not a lock-ordering patch.

# 7. Settings, now a package of per-tab mixins

- [X] Each of the six tabs opens, and a change on each **persists across a
      restart** (that is the whole surface: read a value, write a value).
- [X] Downloads → change the folder with existing downloads on another drive
      → progress advances, the UI stays responsive, files and `catalog.db`
      land at the new path, downloads still play, restart prompt appears.
- [X] Logs tab tails the live log and does not wedge on a large one.
- [X] Home Screen tab: reorder sections, save, and confirm **jellyfin-web's
      own home screen for the same user is not degraded** — section types the
      shim can't draw are meant to be preserved, not rewritten.

# 8. Platforms

- [X] Windows build runs and the installer works (`gen_pkg.sh --skip-build`
      then `build-win.bat`). The refactor touched `win_utils` imports.
- [-] macOS, which forces `mpv_ext = True`, still launches and plays.

---

# Deliberately not re-tested

Cut from the 2026-07-13 list, with the reason. Argue with any of these.

- **The music Phase A/B feature matrix, playlist and collection editing,
  the jellyfin-web parity batch, the offline download lifecycle, single
  instance, the UI-review fixes batch.** These are feature-acceptance
  matrices for features this branch did not change the behaviour of. Their
  logic moved into `pages/` and `gateway/` under characterization coverage,
  and §1's route walk touches each of them once.
- **Offline playback and the sync/download races.** Covered by the
  integration matrix's `test_sync_manager_races` and the offline unit
  modules, and untouched by the decomposition.
- **Everything already marked `[-]` or deferred on the previous list**
  (failure paths mid-edit, offline detection while browsing, leave-group-1
  join-group-2). Still deferred for the same reasons; carrying them forward
  as unticked boxes just makes the list look unfinished.
- **The duplicated `REGULAR MPV` / `EXTERNAL MPV` halves.** The previous list
  repeated sections 1–4 verbatim under both headings, which is 100 lines that
  drift apart the moment one is edited. Replaced by the `lib [ ] ext [ ]`
  pairs on the lines where the backend actually matters.
