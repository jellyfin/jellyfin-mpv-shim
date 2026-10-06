# v3.1.0 hand test

Tested on: 2026-10-04.
- **Linux:** run from source on `hand-test-fixes` at `4532456f`. The fixes below were made
  during the pass, and the items they touch were re-tested on that commit.
- **Windows:** a smoke test of the CI installer built from `origin/working` at `4532456f`.

**The release must ship this tested tree.** `4532456f` is the tested application. What came
after it on `hand-test-fixes` is tests and this record only:
`git diff 4532456f <release> -- jellyfin_mpv_shim` must be empty. Packaging files (version
bumps, the appdata changelog) are the one expected difference, and they get the same check
with only those files listed.

Kept for the same reason as `REGRESSION_CHECKLIST_2026-09-07.md`: a point-in-time record of
what was actually exercised, with the results inline. The tester's notes are verbatim. Only
what the automated suites cannot do was on the list; the end section says what they replaced.

## Found in this pass, and how each ended

| Report | Outcome |
|---|---|
| Offline: a mark or a half-played video's progress shows, then is "forgotten" ~5 s later and on returning to the page | Fixed, `a70c3205`: the offline library kept the catalog as it was when it went offline; every page load now re-reads it. The e2e tests were changed to wait past the re-reads and re-open the page (`a1476c77`); with the fix disabled they fail with the two reported symptoms. |
| Season page "Next Up" plays at the series level | Fixed, `0bc48b6a`: "Next Up" only when the show's next episode is in this season; otherwise "Play" from the season's start. Pre-existing since `c28cb878`. |
| "Preferred Language" did nothing | Fixed, `abd19a80`: shown only while a Dubbed/Subbed preset reads it, with a note. |
| Cast of a music album / photo album: "list index out of range" | Not ours. The tester's own server sent `ItemIds: []` (logged by the guard in `4532456f`). The crash is now a logged decline. A cast with several items runs as the browser's Play All does, so a photo album is a slideshow. |
| Missing episode: "Failed to load", then a 404 | The QA server refuses the past-dated virtual episode by id, even to the account that may see it. The future-dated one with an otherwise identical row loads. Treated as a fixture or server quirk. |
| Downloads and online resume (a question, not a bug) | Downloads do not affect it. `13a7d230` plays a downloaded copy online and checks that the page shows the server's state, not the copy's. |

## 1. Whole-machinery paths (one pass each; they touch the most shared code)

- [X] **Fresh start, Windows installer build** (CI build of `4532456f`): install → sign in → Home → play a film with
      subtitles → HUD (seek, subtitle menu) → stop → the page says **Resume** at the right time
      → Resume.
- [X] **Offline round trip (firejail, not `work_offline`), Linux:** download an episode → go
      offline → play part of it → mark something watched → come back online → jellyfin-web shows
      both.
     - When offline, when I mark something as played or come back from a half-played video, it briefly displays
        the progress and then the UI refreshes after ~5 sec and it "forgets" it, even when backing out and coming back to the page. (This feels like the kind of bug that adversarially evolves to pass tests.......) -- Fixed!
     - Offline playstate sync IS confirmed to work despite the UI not showing the updates while offline.
- [X] **Two profiles, one with a PIN:** switch by the top-bar menu, PIN prompt, the right
      library and ticks for each.
- [X] **Cast from a phone / jellyfin-web:** a film, **music** and **a photo** (the suite casts
      films only).
     - TV works
     - Music album gives error:
       2026-10-04 13:24:18,907 [   ERROR] websocket: error from callback <function WSClient.run.<locals>.<lambda> at 0x7fb4146aba60>: list index out of range
       2026-10-04 13:24:18,907 [   ERROR] JELLYFIN.jellyfin_apiclient_python.ws_client: list index out of range
     - Music playlist works.
     - Movie works.
     - Photo album does not work.
       2026-10-04 13:27:23,279 [   ERROR] websocket: error from callback <function WSClient.run.<locals>.<lambda> at 0x7f6acc6a7a60>: list index out of range
       2026-10-04 13:27:23,279 [   ERROR] JELLYFIN.jellyfin_apiclient_python.ws_client: list index out of range

- [X] **Music:** a playlist or an album with the now-playing bar; next/prev, then a film,
      then back to music.

## 2. Look at it (visual taste, new in 3.1.0)

- [X] **Languages:** switch to a well-translated one and a half-translated one (Settings >
      General): restart banner, labels fit, nothing clipped. (The switch itself is automated.)
     - Works! However there is no way to select "English (United States)" if the system language isn't that.
     - The half translated UI makes me really want to have an optional LLM translated option... Maybe later.
- [X] **#777:** Nebula and Purple Haze: rounded corners on every tile in a row, focused or not.
- [X] **Season page:** the description, the Shuffle button beside Next Up, and a Shuffle run
      that skips missing episodes.
- [X] **Missing episodes** (`DisplayMissingEpisodes` on): the badge and no Play button.
     - Clicking on a missing episode in the season list shows "Failed to load. Check the connection."
     - On second load, "Unaired Episode" works, "Missing Episode" does not and gets a 404 from the server. Probably a fixture issue not a client issue. Shuffle did not play missing episodes
- [X] **Settings > Subtitles & Languages:** the language-filter and preset notes read right.
- [X] **New tonight:** Settings > General with **Browser Fullscreen** on: Playback's
      **Fullscreen** box shows **checked and greyed out** (ruling 3).
- [X] **New tonight:** a stream that dies mid-film (stop Jellyfin while playing): back on the
      film's page, status line "Could not play this item: … — The server stopped sending it
      partway through.", and Resume at the point it broke.
- [X] **HiDPI:** the trickplay preview fills its bubble, in the HUD and in thumbfast.
- [X] **Themes:** a glance at all seven on Home.

## 3. Needs a real window manager, device or GPU (the suite runs under xvfb)

- [X] Start minimised to the tray, restore; tray show/hide/quit; close-to-tray.
- [X] Maximise/unmaximise with a video; move to a second monitor mid-playback.
- [X] A second launch raises the first window (over a film it only raises; your ruling).
- [X] Drag-resize the window, then open **and leave** a comic (the jump on leaving is still an
      open finding, F4.2); hover still follows the pointer after a resize.
- [X] **#771 HDR:** HDR file → stop → HDR file again, several times: still HDR
      (`target-colorspace-hint` in the log).
- [X] Gamepad.
- [X] uosc / modernz (`osc_style: custom`) over a comic and a film: no doubled bar.
- [ ] Windows 11 slow mouse shows the controls (you only have Windows 10; ask a reporter?).
- [ ] Multimedia keyboard Back/Forward page the library; bind `kb_nav_back` and use it at the
      root (quick; the key sweep is unit-tested).
- [ ] **Flatpak build of the tip:** the screen does not blank during playback (#772).

## 4. Known open (not blocking unless you say so)

- `q`/`f`/`F11`/`p` in the library are dead (a product decision; 6.5).
- Subtitles turning themselves on mid-season: needs a reproduction (10.3).
- F36 multi-version: needs a stdjflib fixture (10.4).
- Auto-download "the other profile's copy stays": unit-tested only.
- Quit during an **automatic** download: only a manual download is tested (9.2).

## What the suite now does instead of you (all real app, keys, real server, both backends)

- **Offline sync, all 11 plan scenarios** (`test_offline_ui`, 28 tests), plus the sweep hold, the
  reaper under real timers, a damaged catalog or users.json, twin playlists on two servers,
  resume after a cut or kill, and a Move across volumes including kill-mid-copy. Your Saturday
  B1–B8 each have a test that failed first. **Windows:** see the VM run in the register tonight.
- Your stack items now automated: auto-download cleanup, SyncPlay with a downloaded copy, sort
  persistence (#758), Resume on libmpv and ext mpv, sign-in again, reconnect.
- Settings (every form tab survives a restart, search, the Logs tab), window resize and the two
  fullscreen preferences, profiles (PIN gates, switch races, Quick Connect twice), the page
  refresh after playback, and a stream dying mid-film.

## Additional Bugs Found:

- [X] Clicking the "Next Up" play button on a *Season* starts playback at the *Series* level.
