# v3.0.0 hand regression checklist

Tested on: 2026-09-07, against `fixes-for-3.0.0`.

The hand pass that preceded the v3.0.0 release. Kept for the same reason as
`REGRESSION_CHECKLIST_2026-07-13.md`: a point-in-time record of what was
actually exercised, with the results inline, so the next release can see what
was covered rather than guessing.

**Aimed at seams rather than smoke**, because the automated suites already
cover the parts they can reach: ~5.7k unit tests, an 8-leg integration matrix
across both mpv backends, and 49 e2e legs against a live server. What they
could not see is *composition* — two features sharing one mpv window — and
that is where every defect in this pass lived.

## What it found

Fourteen defects, all of them in seams:

* the four sites that disagreed about "is this item audio" — the last of
  which blanked the library when a music playlist played;
* three feedback loops, all with the same signature (a guard comparing
  against a value the guarded action itself produces): the epub reader
  measuring the node it had just sized, the HUD auto-hide suspending the
  library that hosted it, and the Logs tab redrawing for its own render
  line;
* a HUD engage that spanned a re-entry (`_yield` overtaken by
  `enter_browse`), found only because the refusal logged its caller;
* two half-rules — the Move button's `or` fallback, and "Seek to Skip"
  offered for a gesture that was switched off.

Findings that were left open are in `docs/do-not-fix.md` as F37-F42, each
with what was ruled out and what evidence would close it.

## Legend

[X] Tested fine
[*] Tested, has problems
[ ] Not tested

# Main regression sweep

## 1. The five composite states

Two features sharing one window. One screenshot each; these are the states the
suite structurally cannot judge.

- [X] 🟢 Comic, default theme — page fills correctly, nothing painted over it
- [X] 🟢 Comic under **jf-wmc** — the gradient must *release*, not cover the page
- [X] 🟢 Comic with **osc_style: custom** — the forced background must release too
   - Verified working under uosc and modernz
   - Only oddity is how photos interact with custom OSCs, but it works enough that I am not going to gate v3 on it.
- [*] 🟢 Comic opened **while music is playing** (R6 — this used to refuse outright)
   - *Mostly* works now, killing the music playback takes out the VO but it recovers on scroll. Likely not worth fixing for v3.0.
- [X] 🔴 **Video started straight after a comic** (R7/F15) — watch for stretched
      video. This is the `keepaspect` handoff that made *every* film play
      stretched with no comic anywhere in the session
- [X] Same five again after a quit/relaunch, in case anything is order-dependent

## 2. Comics

- [X] Open a CBZ from the library, page forward and back to the end
- [X] Fit mode: width vs page (default is still `width` on this branch; the
      change to `page` was held back on `enrich-e2e-tests`)
   - We should keep that default change on this branch, fit width is a little odd the more I think about it.
- [X] Pan a zoomed page in all four directions, then page-turn while panned
   - Works fine, panning is preserved between pages.
- [*] Resize the window **by dragging**, then open a comic — the reported bug
      was a jump on open when live size ≠ armed geometry 🔴
   - Geometry jump now only happens on leaving a comic, not opening one.
- [X] Leave a comic → back to library → open a different comic
- [X] Leave a comic → start a video (see §1) → back to library
- [X] Comic while music plays, then stop the music with the comic still open
   - Music stops, next page turn displays page correctly.

## 3. Music × library

The `_video is not None` trap: audio keeps `_video` set **and** keeps the
browser up, so anything asking that question answers wrong during music.

- [X] Play an album; confirm the library stays navigable and the now-playing bar
      is present
- [X] 🟢 Toggle fullscreen during music, stop the music, return to the library —
      the *browser's* preference must be what persisted, not the video's (R5)
   - There is no way to toggle fullscreen in the library browser now apart from settings or WM bindings because we block default MPV keyboard shortcuts. Might be a good idea to re-bind F11 at a minimum.
   - Checking fullscreen library browser correctly fullscreens the app during music playback, but unchecking it does not. The checkbox does work when not playing music.
- [X] Mouse thumb buttons (Back/Forward) during music — these died for the whole
      of music and audiobook playback once
- [X] 🔴 Press the menu key during music with the library on screen — does the
      HUD gear menu open over a track? (R11: verified as written, needs your
      product read, not a fix)
   - Nothing happens, as expected. (We block keybinds in the library browser.)
- [X] Navigate to a different library while music continues
- [X] Start a video while music is playing
   - The UI flashes a little oddly, but nothing actually breaks.
- [X] Play a playlist containing mixed types

## 4. Video ↔ picture ↔ reader handoffs

- [X] Photo → video → photo
   - Works, note as designed if you click a video in a mixed content library, next and back don't do anything. Next and back do work for photos, but only photo sibblings are played, videos are ignored. The Play All button on the entire folder works as designed.
- [X] Photo viewed while music plays
   - Photo wins, music stops as expected.
- [X] EPUB reader open, then start playback, then return
- [X] Audiobook: resume position across a stop/start (resume is in **minutes**
      for audiobooks — a wrong unit looks like "resume is broken")
   - Yes it works, provided the audio is >5mins (upstream jellyfin behaviour).
- [X] Book → comic → video, in one session, without relaunching

## 5. Window geometry & fullscreen

- [X] Resize the window in the library; the layout reflows and tiles stay square
- [X] Fullscreen in the library, leave, re-enter — preference sticks
- [X] Fullscreen during playback, stop, return to library
   - Setting is remembered for playback only, as expected.
- [ ] 🔴 Be fullscreen when an **update notice** arrives (R8) —
      `fullscreen_disable` is written above its own `persist` gate, so an
      app-initiated un-fullscreen can latch a flag meant to record *user* intent
   - This one is a pain to test due to update notifier requiring manual mutation testing.
- [X] Start minimized (tray), then restore
- [X] Maximize / unmaximize with a video playing
- [X] Multi-monitor: move the window between screens mid-playback, if you have one

## 6. Keyboard & input across transitions

The surface with the worst record — three regressions in 48 hours, all "a key
section never re-enabled after a transition".

- [X] Arrows/ENTER/TAB/MENU work in the library **from launch**, before anything
      has played
- [X] Same keys after: library → play a video → back to library
- [X] Same keys after: summoning the HUD and clicking Back on a visible bar
      (the common way to leave a film)
- [X] Arrows during plain playback **seek** — the UI must not be holding them
- [X] `q` and other printable mpv shortcuts in the library — **known dead** under
      `browse_block_keys` (deliberate #730 fix). Decide whether `q` passes through
   - Known dead, we should probably re-allow `q`, `f`, and `F11`. And `p` while playing music.
   - Need to make sure these don't break text entry. (`m` and `space` don't break during music and work.)
   - ESC should dismiss keynav and NOT act as a back button until key nav is dismissed. (Keynav drops the space keyboard shortcut for music control, that is how I found out.)
- [X] Mouse: hover highlights follow the pointer after a window resize (a WM grab
      spanning a resize used to strand the hover flag)
- [X] Wheel over an open HUD gear menu — **known** to seek (accepted for #711)
   - It doesn't seek though, it updates volume control on my MPV. Accepted as-is for v3.
- [X] Gamepad, if you test it: confirm nav and Back in library and playback

## 7. Themes × OSC styles

Themes: `jf-appletv`, `jf-blueradiance`, `jf-light`, `jf-purplehaze`, `jf-wmc`,
`nebula`, `superdark`. OSC styles: `mpvtk` (default), `mpv`, `none`, `custom`.

- [X] Each theme: library home renders, text is legible, no black-on-black
- [X] 🟢 jf-wmc specifically over a comic and over a video (§1)
- [X] Switch theme **mid-session** and confirm the library repaints. Note: mpv's
      own window background does *not* move on a live theme change — that is
      expected, not a defect
- [X] `osc_style: mpv` — mpv's built-in OSC appears and the mpvtk HUD does not
- [X] `osc_style: none` — nothing appears
- [X] 🟢 `osc_style: custom` with a third-party OSC (uosc) — no doubled bar
- [X] HUD on **duration-less content** (Live TV, or a >1h item in a narrow
      window) — **known** to shed a control
   - It actually doesn't, seek works it just lets you seek back in livestream history and the duration is just the history length. This is standard MPV behaviour, no desire to break it.

## 8. Less-used surfaces

Under-covered and easy to skip; a year-later bug lives here.

- [X] Live TV: channel list, guide, tune a channel, timers
- [X] Live TV → back to library → a normal video
- [X] Search, including a query with non-ASCII characters
- [X] Filter panel: apply, clear, and confirm the grid actually changed
   - Category filter is KNOWN BROKEN this is an upstream issue not us.
- [X] Collections / playlists / Next Up / Continue Watching tiles all play
- [X] Cast: send to another client, and cast-and-play. **Known**: casting no
      longer raises the window by default
   - Works, but "seek to skip" shows when casting even though HUD is supposed to suppress it.
- [X] SyncPlay, if you have a second client
- [X] Trickplay/seek-preview bubble on a long item; then seek repeatedly

## 9. Downloads & offline

- [X] Download an item, watch it complete, play it offline
- [X] 🔴 Quit the app **while an auto-download is running** (R4 —
      `auto.tick()` is called without `stopping=` while the line below it passes
      one). Confirm a clean exit and an intact catalog
- [X] Watched state written offline surfaces on the server after reconnect
- [X] Delete a downloaded item and confirm the store and the catalog agree
- [X] Startup cost: note whether launch feels slower — **known**, ~4 extra
      catalog scans plus a sqlite backup run synchronously

## 10. Track selection & F36

- [X] Pick a non-default audio track, then let the episode advance — the
      remembered track carries over and the HUD agrees with what you hear
- [X] Same for subtitles, including turning them **off** and advancing
- [ ] **Known**: subtitles can now turn themselves on mid-season
- [ ] 🔴 If you have a **multi-version** item (4K + 1080p of the same title):
      play with a remembered track and confirm the version that plays is the one
      you expect. F36 — the pin selects `MediaSources[0]`, which Jellyfin sorts
      as the *highest resolution*, not the most playable
   - TODO for later: Add this to stdjflib
## 11. Settings

- [X] Every settings tab opens and renders
- [X] Change a setting that applies live (theme, HUD options) — takes effect
- [X] Change a setting that needs a restart — the restart banner appears
- [X] Settings search finds a key by name
- [X] 🔴 Settings → Downloads → **Move** the store: confirm the field afterwards
      shows the path it actually moved to (`_sync_path` is Tier 1 and
      destructive — the visible field was showing the wrong path)
- [X] Quality/bitrate override, then play — the stream honours it
   - Works, but this test uncovered that changing subtitle/audio tracks during a transcode sometimes causes the HUD to die from the loading page coming up and then not dismissing correctly. -- Can't reproduce, should be fixed now.
## 12. Startup, shutdown, tray

- [X] Cold start with no config → first-run flow → log in
- [X] Restart with saved credentials → straight to the library
- [X] Second launch while the first is running (single-instance)
- [X] Tray: show/hide, quit from the tray
- [X] Quit **during playback**, and quit **during a comic**
   - Comic "quit" is really minimize to tray, video is a proper quit to tray
- [X] Confirm no orphaned mpv process after each exit
- [X] Startup PIN gate, if you use it

# Fix regression tests

## 🔴 Not reproduced — this pass IS the test

### 1. Playlist: video → song, library UI auto-hides
`4c8a385c`. **I could not reproduce your symptom**, so I fixed the class it
belongs to rather than the instance. Two real defects found on the way:

- `ItemActions.play` tested `Type == "Audio"` only, so an **audiobook**
  (`Type="AudioBook"`, `MediaType="Audio"`) launched down the *video* branch,
  which clears `_browsing` and hands the window over
- a film's `hud.state` outlived it across a queue advance, and
  `reassert_window_state` reads that as "a video is in flight, re-enter HUD mode"

- [X] The original: playlist with a video **then** a song. Leave the mouse
      still through the song. Library must stay up. **(confirmed fixed)**
- [X] **Song → video, back in the same playlist: the HUD comes back.** This
      is the regression `4c8a385c` caused and `a3d8bd96` repairs — a late
      audio push (the ticker sends one a second) wiped the HUD state off the
      video that had just started and pulled the renderer out of HUD mode.
      Move the mouse / press the wake key; controls must appear.
- [X] Video → song → video → song, twice round, in case it needs a repeat
- [X] Let the HUD auto-hide during the second video, then summon it again
- [X] Reverse order: song **then** video, then back to a song
- [X] Audiobook launched from a tile — does the window flash/hand over before
      coming back? That is the half-rule, and it should be gone
- [X] A song playing when mpv is re-created (change a setting needing a
      restart of the player, if you have one) — the HUD must not appear

---

## 🟡 Diagnosed from your screenshot and fixed

### 2. EPUB reader + music
`00ee2c3d`. The reader is meant to measure the hole its bars left and
rasterize to it. **That measurement had never once succeeded**: a plain Row
emits no scene node whatever id it carries, so `node_rect("rd-area")` was
always None and the fallback (`window − 44 − 48`) was the only path that ever
ran. Right whenever the reader owns the whole window; too tall by exactly the
now-playing bar otherwise, and centred, so it spilled over both of the
reader's own bars. AREA_ID now sits on a transparent Box, which draws nothing
and measures.

- [X] Reader open, then start music — the top bar and the page controls stay
      visible, page reflows to the smaller area
   - Not actually possible to do this, so successful by default.
- [X] Music first, then open the reader
- [X] Stop the music with the reader open — the page grows back
- [X] Resize the window while reading with music playing
- [X] **Control**: reading with nothing playing still uses the whole window
      (this is the case the old fallback got right, and a bad fix would
      shrink it)
- [X] Page turns, the reader menu, and text selection/copy still work
   - Doesn't actually support text selection, but copy works.

---

## 🟡 Fixed, mechanism confirmed, but the surface is twitchy

### 3. Focus ring no longer eats non-nav claims
`1ad8d4ea`. `keyclaim.take` refused **every** claim while a focus ring was up;
under `browse_block_keys` a refused claim is *swallowed*, so keynav killed
SPACE for music with no keyboard way back. Now scoped to the keys the ring
actually uses.

- [X] Music playing, arrow around the library to raise the ring, press SPACE —
      pauses
- [X] Same with `m` (mute)
- [X] **The control**: with the ring up, arrows still move focus and do not
      leak to the player
- [X] In the comic reader: DOWN into the bottom bar, then LEFT/RIGHT steps
      between buttons rather than turning the page (this is the behaviour the
      old blanket refusal existed to protect)
   - Works, but no keyboard only way to kill the focus ring without ESC which also exits the reader.
- [X] In the epub reader, same
   - Works, but no keyboard only way to kill the focus ring without ESC which also exits the reader.
- [X] Text entry (search box, login form) still types normally
- [X] ESC still backs out where it used to

### 4. Comic + music, geometry, fit-page
`d3ef8ef1` + `607442f9`, cherry-picked from `enrich-e2e-tests` — the three
changes you independently confirmed were right to have made.

- [X] Comic opened **while music plays**: music stops, page displays
- [X] Drag-resize the window, then open a comic — **no jump**
- [X] A comic opens at **fit page**, not fit width
- [X] Video started straight after a comic — not stretched
- [X] A comic while a *video* plays is still refused (the control)
   - Not actually possible to do this, so successful by default.

### 5. Fullscreen during music, and the theme background
`572a9b5a`. Three sites of the `_video`-vs-`_library_showing` rule.

- [X] Music playing: **untick** "fullscreen library browser" — it leaves
      fullscreen now (this was the reported half)
- [X] Music playing: tick it — still goes fullscreen (the half that worked)
- [X] With a **video** playing, the browse preference does not yank it
   - Not possible to change the setting while playing video.
   - Generally behaves except one edge case, fullscreen library browser does not exit fullscreen when regular fullscreen setting is not set.
- [X] Change theme **while music plays** — the background behind the library
      updates immediately, no longer only after stopping
- [X] Press the menu key / use a **remote's cog** during music — the OSD menu
      must not open over the library

---

## 🟢 Small and self-contained

### 6. Next Up vs Continue Watching
`eeb299af`. Now sends `EnableResumable=false`, matching jellyfin-web.

- [X] A part-watched episode appears in Continue Watching **only**
- [X] Next Up still lists the genuinely-next episodes
- [X] Next Up tiles still have artwork (the query moved off the apiclient
      helper, so the image params moved with it)
- [X] Autoplay-next at the end of an episode still finds the next one
      (different call, deliberately unchanged)

### 7. Adding a server
`eeb299af`.

- [X] Add a second server from the server manager — you land on **its** home
- [X] Quick Connect login — same
- [*] Re-authenticate a server already in the list — you stay where you were
   - I don't think there is a way to do this without delete/re-create.
- [X] Restart: still opens on the last server you browsed

### 8. Casting
`eeb299af`.

- [X] Cast something with an intro — no "Seek to Skip" on the OSD
- [X] Locally with the mpvtk HUD, the Skip button still appears in "ask" mode
- [X] With `osc_style` set to `mpv` or `none`, the OSD prompt still appears
      (that is the only surface left there)


---

## Regression sweep

Cheap, and these are the seams the six commits touched.

- [X] Library → video → library: keys, focus, window size all normal
- [X] Library → music → comic → video, in one session
- [X] Photo → video → photo
- [X] Quit during playback and during a comic; no orphaned mpv
- [X] Downloads still list and play (untouched, but `is_audio` moved)
   - Download smoke test went well.

## Notes / defects found

1. Background color does not update when changing theme while playing music. Background change does apply after stopping music. -- Confirmed fixed.
2. When you log onto a new server added via the server manager page, it goes to the homepage of the previous server not the newly added one. -- Confirmed fixed.
3. We should see if enableResumable=false and enableRewatching=false should be added to the NextUp request. Right now partially watched episodes show in both Continue Watching and Next Up, they should only show in Continue Watching. -- Confirmed fixed.
