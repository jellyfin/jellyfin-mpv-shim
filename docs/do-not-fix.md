# Code that looks like a bug and is not

Kept because this ground has been covered twice and the second pass cost as much
as the first. Everything here was read, understood and deliberately left alone;
several entries are *adjacent* to a real bug that has since been fixed, which is
exactly what makes them look wrong to the next reader.

If you are about to "fix" something on this page, the bar is a new argument, not
a fresh reading of the same code.

## 1. Load-bearing code that reads as a defect

| Site | Why it stays |
|------|--------------|
| `configure_streams`' `not video.is_transcode` gate | Correct. Transcoded audio is baked into the stream; selecting a track client-side is a lie. Pinned by `tests/e2e/test_track_selection.py`. A track fix belongs upstream of it — see `docs/track-selection.md`. |
| `_apply_remembered_tracks` not checking `explicit_tracks` | Deliberate, and the only step of the track chain that does not. It is how a deliberate pick reaches the *next* episode: the memory carries the choice and re-matches it, where the flag would carry a stale stream index. `docs/track-selection.md` section 5. |
| `app.py` `failed()` not epoch-gated | Deliberate: an error is a rollback, and a route you navigated away from must still hold its error when you return. Distinguishing two loads at one epoch is a separate load token, not epoch-gating `failed`. |
| `_claim_page_keys` called unconditionally | Deliberate — it is what makes *leaving* a page drop its claim. A guard here breaks claim release. |
| `_apply_auth_headers` returning False | Not a failure path. False means "the URL carries its own token", which is the safe fallback. `docs/auth-headers.md`. |
| `_move_tree` skipping names already in the destination | Deliberate anti-clobber, and what makes the rollback safe: anything that was in the destination before the move started was never a candidate, so undoing cannot touch it. |
| `_expand`'s broad `except` | Correct for every type except Playlist, where failure and empty had to be told apart (`ExpandFailed`). The catch is not narrowed. |
| `mpv_options.py` OrderedDict insertion order | Deliberate and documented. |
| `HomePage._unique` renumbering a colliding row id | Deliberate, and kept even though renumbering is positional -- the one thing a row id may not be. It cannot fire: `_row_id` drops the key only when it is falsy, and a falsy key needs a library view with no `Id`. Measured on the QA server (12.0.0): 19 of 19 views carry one, and `BaseItemDto` marks 152 of its 155 properties `nullable: true` -- `Id` is one of the three it does not. Pinned by `test_the_backstop_is_unreachable_through_the_real_producers`. Deleting it does not remove the defect, it trades it for a worse one: duplicate node ids, where `layout()` warns and events "target only the last occurrence", so a row's tiles go unreachable. That is the bug `2fa969b3` was written to fix. |
| `keysweep` caching the sweep | Deliberate: a re-sweep would see our own non-weak lines and drop every claim. |
| A mixed-script line sitting ~3px low (`mpvtk/pilfont.py`) | The reserved metrics come from `script_of`'s face and the shared baseline from the tallest run. The alternative re-typesets every wrapped line in the symbol face and draws RTL as boxes. Every caller draws into a margin that absorbs it. |
| The Latin ligature block on the CJK face (`pilfont.py`) | Measured — NotoSansCJK draws `ﬁ` fine. Moving it is churn against nothing. |
| `log_utils.py` `ring_handler`'s non-forced formatter | With `sanitize_output: false` the in-app log viewer and "copy logs" hand out unredacted text while `log.txt` stays clean. That is the point: it is a hidden dev setting, and switching it off means you want live URLs for debugging. |

## 2. Traced and clean — do not re-audit

Every outbound credential path other than the mpv header (13 raw request sites,
thumbnails, the sync downloader, cast art, external links); `db.py`'s three
`userdata_json` read-modify-writes (fully inside `_lock`); `SessionReporter`
(single-worker FIFO by design); the server's `POST UserItems/{id}/UserData` (a
genuine partial patch, not a whole-document RMW); `trickplay.py` (re-checks the
video after every blocking step; `_covers` records the *asked* span);
`tile_renderer._request_image` (backoff, max attempts, 4xx negative cache);
`pages/reader.py`; every `_start_daemon` poller; `headless`; `update_check.py`;
the pointer/hover path across suspend/resume; the HUD across mpv's console on
both backends.

**Investigated and not promoted**, with the reason, so the same read does not
produce a finding a third time:

- the websocket redial loop's missing `_switching` check — `WebSocketDisconnect`
  only fires from `stop_client`, and all four callers silence or drain first, so
  it is unreachable. Hardening, not a defect.
- strip composites in flight across a theme change — mechanism real, trigger
  unverified.
- `cast_ready` — write-only since the Tk browser was deleted. A stale comment.
- `_sync_playstate` never retiring a permanently-rejected entry — a poll, not a
  repaint loop. The download side grew `_record_permanent_failure` for exactly
  this and the playstate side does not need it.
- a page claim raising the renderer's forced section above mpv's console —
  mechanism demonstrated, no ordinary workflow reaches it. A latent hazard.

## 3. Settled design decisions

- **`enable_osc` from a 2.9.0 config is ignored on purpose, and must not be
  migrated to `osc_style: none`.** It is gone from the schema, so an upgrader
  who had it off gets `Config item enable_osc was ignored` in the log and
  lands on the `mpvtk` default. That reads exactly like a missing migration
  and is the intended destination.

  In 2.9.0 mpv's OSC was the only OSC, so turning it off meant "this OSC is
  not good enough". The Jellyfin UI is new in v3 and is markedly better than
  every other option because the shim integrates with it at the player level
  — so the upgrade *answers* that complaint rather than contradicting it.
  Mapping the old flag to "no controls at all" would take the new one away
  from precisely the users it was built for. [iw].

  `resolve_osc_style`'s comment that "none is where the old enable_osc
  setting went" is about where the *switch* went as a user-facing choice, not
  about migrating anyone onto it.
- **`osc_style: "default"` no longer being offered is not a lost feature.**
  It folded into "MPV UI" at CONFIG_VERSION 5 — the two only differed in who
  loaded the OSC, and once the shim used mpv's own for both, `default` was
  just the one that forgot to suppress the idle logo. `custom` covers "I run
  my own OSC" and `none` covers "no controls". It is now an inbound legacy
  value only: `resolve_osc_style` accepts it and returns "mpv", and nothing
  resolves *to* it. `build_mpv_options` still maps it, because that function
  is a pure style → options mapping and "let mpv decide" is still what the
  value means — not because a caller can produce it.
- **`thumbnail_osc_builtin` is deleted with no migration, like `enable_osc`
  above, and a `false` in an old config is ignored on purpose.** Its one
  documented meaning was "use your own custom osc but leave trickplay
  enabled" (`acbc3e9d`'s README), which is exactly `osc_style: custom` — and
  `mpv_scripts` loads thumbfast under every style, so `custom` keeps the
  previews that sentence promises. Nothing it could express was lost, so
  there is nothing to carry across.

  It is not migrated to `custom` automatically because the flag is a
  default-*on* switch somebody turned off, not proof that a replacement OSC
  exists, and `custom` sets `osc=False` and loads nothing — the one outcome
  a silent upgrade must never produce is a user with no controls at all.
  [iw]. Someone who really is running uosc sets `osc_style: custom` once and
  can see that they have.

  Do not re-add the key as a compatibility shim. The `resolve_osc_style`
  branch it had was the last thing that could resolve to `"default"`, which
  is why that value's status changed in the entry above.
- **The startup PIN is parental control, not a security boundary.** It stops a
  kid on an HTPC opening R-rated films. So the work is enumerating the doors and
  a catch-all test — *not* deferring connection until unlock.
- **Track rules resolve against the item's own `MediaSources`**, which is what
  the details page already shows its pickers, so the screen and the stream
  agree. No second round trip.
- **`explicit_tracks` is the "the user overrode it" signal** and is correct as
  designed. `docs/track-selection.md` for what checks it and what deliberately
  does not.
- **`work_offline` is a dev setting and does not lock out the API client.**
  Every check in the tree is a connect/reconnect gate (websocket redial, cast
  verify, health check) plus browser source selection; there is no gate in the
  request path and none in the apiclient. So an already-connected client keeps
  serving requests and the sync worker streams to completion. Offline testing is
  done with firejail — real network isolation — which is why this is not worth
  fixing rather than why it is not broken.
- **The orphan sweep identifies by name shape, and that is deliberate.** A
  reviewer will point out that `_looks_like_item_id` proves a directory
  *resembles* an item id, not that we wrote it, and will demonstrate the sweep
  deleting a hand-made `<root>/<server_id>/<32-hex>/` that has no adoptable
  `item.json`. That is accepted. Reaching it means picking a download folder,
  passing the refusal of any non-empty destination, and then placing a file
  inside a guid-named subdirectory of a server-id-named subdirectory of it.
  The store owns its root and says so; guarding against that costs the sweep
  its purpose. See `docs/offline-sync.md` §5 for what the sweep *does* refuse.
- **`play()` cancelled during `_warm_shader_scope` returns with the new
  server's header still installed.** Not a leak to fix at that `return`:
  `_apply_auth_headers` clears `http-header-fields` at the top of every start,
  which is exactly the mechanism that covers it, and nothing is playing to use
  the stale value in between. Adding a third "revoke before returning" beside
  the two that already exist would be the duplication this tree keeps paying
  for. See `docs/auth-headers.md`.
- **#726 — the right button not dragging the window with the HUD up is
  deliberate.** With `mouse_click_pauses` on and the HUD summoned, `on_rclick`
  (`renderer.lua:3954-3993`) has no `begin-vo-dragging` branch, so a right-drag
  over a summoned bar does nothing. Maintainer verdict: as designed [iw]. The
  right button pauses in that modality, and a button cannot both pause on
  release and start a drag on press without one of the two becoming
  unpredictable. Dragging is the left button's job, which is exactly what
  turning `mouse_click_pauses` off buys.

  Recorded because it reads as a gap in a table — A6 in the mouse matrix of
  `docs/POSTMORTEM_3.0.0.md` §3.3 — sitting next to A4, which **is** a defect.
  Two adjacent empty cells, one deliberate and one not; do not close them
  together.
- **#727 item 1 — no window controls in full screen is deliberate.** In full
  screen there is no title bar to replace and nothing to restore down to, so
  the buttons would be furniture over the video. The escape hatch exists and is
  a real setting: `window_controls_fullscreen` (`conf.py:280`, default
  `false`, documented in `docs/configuration.md`). Anyone who wants them back
  turns that on, which is why this is a default and not a limitation.
- **Prefetching the library id on the detail page was considered and rejected.**
  It only helps items reached *through* a detail page — not Play All, not a
  queue advance, not a cast — so the play path needs the lookup as a fallback
  anyway and nothing is saved.

## 4. Still open

Small enough to keep here; the sequencing and progress logs that used to
surround them are in git history (this file was `RELEASE_FIXES_2026-08-31.md`
through commit 91e5c8a5, and the review's own probes and logs were archived and
then dropped in the same commit).

| Tag | Site | State |
|-----|------|-------|
| F15 | `player_window.py` `set_picture_view` guard asymmetry | **Unverified.** Construct the interleaving before fixing. |
| F25 | `sync/manager.py`, `sync/auto.py` | A live `work_offline` toggle leaves the download worker streaming on a metered link. Low priority per section 3; "won't fix" and removing the setting are both on the table. |
| F26 | `cast.py` | Cast parks the last composite. |
| F29 | `player.py` load gate / `_on_cache_pause` | Field report, below. |
| F35 | `renderer.lua` `phud_skip_bind` | Binds literal `'ENTER'` for the Skip button whatever `hud_wake_key` says, so a moved wake key leaves ENTER accepting a skip. Found while doing #717 and deliberately left: it is a `hud_wake_key` bug, and it wants a decision about whether the idle Skip offer follows the wake key or `ui_select_key`. |
| F36 | `media.py` `get_playback_url` | Asking for a track pins `MediaSources[0]`, so a multi-version item loses the unplayable retry. Accepted for 3.0.0; below. |
| F37 | `renderer.lua` `keyclaim.block_take` | `browse_block_keys` swallows `q`/`f`/`p` in the library, defeating the player's STANDING fullscreen claim. Two claim mechanisms; below. |
| F38 | `player_window.py` picture path | **Unverified.** The playback HUD stops appearing after several photo/video handoffs. Needs evidence; below. |
| F39 | `player_window.py` `clear_picture` / `set_browse_window` | The window jump moved from opening a comic to LEAVING one. Cosmetic, and the open half is fixed. |
| F40 | `player_window.py` `_apply_browse_fullscreen` | Reported edge case: the browse preference does not leave fullscreen when `fullscreen` is unset. Not yet reproduced. |
| F41 | `mpvtk_browser/app.py` `_yield` | **Diagnosed and closed.** A yield overtaken by `enter_browse` engaged the HUD over the library. Below, kept for the shape. |
| F42 | `settings/general.py` `_sync_path` | The dict holding the typed download folder is created once and never cleared, so a value can outlive the field that produced it. Below. |

### F29 — sleeping NAS, not reproduced

**Report:** Windows 11, external mpv (shinchiro), NAS drives asleep. Playing a
video straight after waking the HTPC makes mpv loop ~2-3 s until the NAS spins
up; skipping back a few seconds recovers it. On 2.10 the screen stayed blank
instead.

**Established.** The change in symptom is explained by `672ef1ac` ("stop gating
the start on a duration that may never arrive"). Before it the start waited for
`duration`; a stalled SMB source never reports one, so the wait ran out the
whole `playback_timeout` and stopped playback — a blank screen, exactly as
reported for 2.10. After it the wait is also satisfied by `file-loaded`, which
fires as soon as mpv has the tracks, so the start proceeds against a source that
is not delivering. That commit is right about its own case (a live channel never
reports a duration) and this is its cost.

**Not established:** what produces the ~2-3 s loop. Candidates read but not
confirmed — the offset seek applied while the demuxer is starved, or mpv
replaying its small cache. Nothing was reproduced, so nothing here is a
diagnosis.

**One real gap found while looking.** `_on_cache_pause`, the observer on
`paused-for-cache`, returns immediately unless SyncPlay is enabled. Outside a
SyncPlay group the client does **nothing** when mpv stalls on its cache: it
neither surfaces the state nor recovers from it, so a starving source is
indistinguishable from a broken one. Not the loop's cause, but it is why the
user has nothing on screen telling them the NAS is still spinning up.

**Before acting, ask for** the shim's `log.txt` from the affected run — grabbed
*before* relaunching, it is rewritten on every start — and mpv's own log, plus
whether `direct_paths` is on (SMB and a NAS implies it).

### F36 — the source pin collapses a multi-version item

**Accepted for 3.0.0.** Pinned by `MultiVersionSourcePinTest` in
`tests/test_track_negotiation.py`, which asserts the current behaviour
*including where it fails* — so a fix cannot land silently, and the tests are
expected to fail when one does.

**What happens.** PlaybackInfo silently ignores `AudioStreamIndex` unless
`MediaSourceId` is sent with it, so `get_playback_url` derives one whenever a
real (non-negative) index is being asked for. `remember_audio_track` defaults
on, so from the second item in a queue onward almost every play carries one.
The derived id is `source_for_track_rules()`, i.e. `item["MediaSources"][0]`,
and the server then answers with **that source alone** — measured on the QA
server: `Pilot` has three versions, and PlaybackInfo returns three sources
without an id and one with it.

Two things downstream are then unreachable: `get_best_media_source`'s
preference for the highest-bitrate source that will *direct play*, and the
`if url is None and len(playback_info["MediaSources"]) > 1` retry — so an
unplayable primary now fails outright instead of falling back.

**Why it is accepted rather than fixed.** `MediaSources[0]` is not arbitrary.
Jellyfin sorts the list (`SortMediaSources`, in
`Emby.Server.Implementations/Library/MediaSourceManager.cs`) by: the queried
item's own source first — the comment there says "so it stays the default that
gets played" — then `VideoFile` over other video types, then non-3D, then
**descending video width**. So [0] is the version the user clicked, else the
highest-resolution one. The shim direct-plays most formats where bandwidth is
not the constraint, and the alternative — dropping the pin — loses the
remembered audio and subtitle track on *every* episode advance, which is the
more visible regression by a wide margin.

The residual risk is narrow and real: a library holding a 4K version the client
cannot play alongside a 1080p one it can. The sort is by resolution, not by
playability, so the pin picks exactly the version most likely to need
transcoding, and the fallback that existed for that case is gone.

**The fix, when it is time.** Derive the id only when the item has a single
`MediaSource` — nothing to choose between and nothing to fall back to, so the
pin costs nothing there. The multi-version case then needs the source resolved
*before* the negotiation (from the item DTO, weighed the way
`get_best_media_source` weighs it) and that source pinned, rather than [0].
That is a real change to the negotiation order and wants its own round.

**Note for whoever picks this up:** the review that found it framed this as
"the pin takes the source choice away from the server", which is not quite
right and points at a worse fix — restoring the server's choice would take the
remembered tracks with it. The server sorts by width; the shim sorts by
playability; they disagree exactly where this bites.

### F37 — the key block defeats the player's own claims

**Deferred past 3.0.0 by decision** [iw]: "ALT+F4 or settings can escape
fullscreen." It is a change to the input arbiter — the surface with the worst
regression record here (three in 48 hours, `tests/e2e/test_input_routing.py`)
— and the ask is a convenience, not a repair of something that used to work
for a user.

**What happens.** `browse_block_keys` (default on, #730) installs a forced
`any_unicode` binding that swallows every printable key while the library is
up, so `q`, `f` and `p` do nothing there. A forced binding that returns does
not hand the key back, so "not handled" means "gone".

**Why `f` is the interesting one.** `_bind_mpv_handlers` sets a **standing**
claim — `self._key_claims["fullscreen"] = {keysweep.FULLSCREEN}` — with a
comment explaining that recording "the user asked for fullscreen" is always
wanted. That claim is installed as an mpv input section, and the block's
`any_unicode` binding shadows it. `block_take` does consult a claim set, but
it is `state.keys`, the **renderer's** set (what a page claimed through
`claim_keys`), not the player's.

So this is not a missing entry in a list. It is two claim mechanisms that do
not know about each other, and the block honours one of them.

**What a fix has to answer.** How a blocked key reaches the player's claim
section, given that the forced binding cannot pass a key through. The
existing machinery is on the Python side: `_swept_keys()` already maps key →
(semantic, arg), and `_on_claimed_key` already carries out fullscreen, pause
and seek through the operations that know about SyncPlay and about
remembering the choice. So the plausible shape is the renderer routing a
blocked-but-claimed key to that dispatcher rather than swallowing it — but
that is a new channel through the arbiter, and it wants a real-mpv matrix
run, not a unit test.

**And the ESC half, now confirmed twice** [iw]: in the comic reader and the
epub reader there is "no keyboard only way to kill the focus ring without ESC
which also exits the reader". ESC should drop the ring first and only page back
on a second press. It belongs here rather than in its own entry because it is
the same decision -- what the library's keyboard policy is -- and splitting it
would produce two guards where the repair is one rule. The claim-swallowing
half of that report IS fixed (`keyclaim.nav_names` in `keyclaim.take`); this is
what is left.

**Also wanted in the same pass** [iw]: `p` while music is playing, and a
check that none of it breaks text entry. `m` and SPACE already work during
music (they are claimed and routed), which is the behaviour the rest should
match. The focus-ring half of the same report is fixed — see
`keyclaim.nav_names` in `keyclaim.take`.

### F38 — the HUD stops appearing after photo/video handoffs

**Reported, not reproduced.** "Photo -> video -> photo ... this kills the HUD
after a few video skips, not sure why." Several handoffs in, the playback HUD
no longer summons.

**Ruled out, by driving them:** the browser's `on_playstate` is correct across
every photo/video alternation tried (photo, photo->video, photo->video->photo,
four alternations, five video advances, and each with a `stopped` push in the
middle) -- `hud.state` stays set and `set_hud(True)` keeps being sent. The
renderer's own lifecycle is correct across the equivalent message sequence in
`tests/lua/`, including from an auto-hidden HUD. `_release_page_grabs` drops a
key claim and the pan model and touches neither.

**Reproduced, in part** (`tests/lua/`, 2026-09-07). Izzie's ordering was
photo x3 -> video -> video, and repetition is the point rather than the
photo/video mix. The `mpvtk-hud` handler early-returns when the mode already
matches:

    if want == state.phud.mode then return end

Summoning the HUD unbinds the wake key -- correct, it is already up. The next
item's `set_hud(True)` then hits that early return, so `state.phud.shown` stays
true for an item that is gone and **nothing re-binds summon**. Measured: after
the second video the wake binding does not exist and mouse moves produce no HUD
event at all, because the renderer believes it is already showing.

**FIXED**, once a second report gave the trigger: changing an audio or
subtitle track during a transcode deletes and re-creates it, so the loading
screen comes up and `LoadFeedback.clear()` hands off through `_yield()` --
which engages while the renderer is ALREADY in HUD mode, hits the early
return, and re-establishes nothing. The bar the user changed the track in was
still up, so `phud.shown` stayed true for a stream that had ended.
Intermittent because it depends on the bar still being up when the handoff
lands; the auto-hide firing first re-binds summon and it recovers on its own,
which is why the first reproduction looked self-healing.

The decision the fix needed: the renderer cannot tell "a new stream started"
from "a setting changed" -- `hud.engage()` is both -- but the PYTHON side can.
`_yield()` is a handoff by definition, so it now engages with `reset=True`,
which cycles `set_hud(False)`/`set_hud(True)` and returns the HUD to a clean
idle. Every other caller is a re-send (settings, SyncPlay, a fresh renderer)
and must NOT hide a bar somebody is using; that is the control test.

**The other suspect, still open, is the picture path and its deferral.** `show_picture`
/ `clear_picture` reach the player through `run_action`, which defers whenever
the player lock is busy -- and it is busy for the whole of a playback start.
`clear_picture` guards on `self._video is not None` **with no `_loading`
half**, where its sibling `reset_picture_view` guards on `self._video is None
and not self._loading`. `_video` is not assigned until the duration wait
succeeds, so a deferred `clear_picture` landing mid-start passes its guard and
calls `set_browse_window(True)`. That is F15's asymmetry with a symptom
attached, and it is the first evidence for it -- but it is a hypothesis, and
the last two fixes made from a hypothesis in this area both had to be redone.

**Before acting, ask for** `log.txt` from the affected run, grabbed *before*
relaunching (it is rewritten on every start). `wlog` logs every
`set_browse_window` with its caller, which is exactly the line that would
settle this.

### F39 — the comic window jump moved rather than went away

`f583dc7f` gave `show_picture` the `_sync_window_geometry` call that every
other load already had, and the reported jump on **opening** a comic is gone.
The hand pass then found it on **leaving** one instead.

Not diagnosed. The shape to check first: the reader borrows `keepaspect`, so
the window can change size while a page is up; `set_browse_window(True)` on
the way out turns it off and re-arms nothing, so the geometry armed before the
comic is what the VO reconfig re-applies. Whether that is a jump or a restore
is a product question as much as a bug.

Cosmetic, one window resize, and the half that draws over the page is fixed.

### F40 — browse fullscreen and the playback setting

Reported: "fullscreen library browser does not exit fullscreen when regular
fullscreen setting is not set." Not reproduced -- `_apply_browse_fullscreen`
reads `browser_fullscreen or headless` for the ON direction and
`_library_showing()` for the OFF one, and `settings.fullscreen` is not in
either. So either the path is a different one (`set_fullscreen`, or the
live-apply in `apply_browser_fullscreen`) or the report is about a state
neither of us has pinned down. Wants a reproduction before a fix.

### F41 — a HUD engage while the library is on screen, caller unknown

**The symptom is fixed and the cause is not found.** `HudController.engage`
refuses while `_browsing` (659bb8c7), which closes the reported bug: a video
-> music playlist advance left the renderer in HUD mode with the library
drawn, so the cursor hid, the library vanished after `hud_hide_secs`
(`phud_hide` calls `ui_suspend`) and came back on motion (`mouse-pos` is
observed and needs no input section).

**The caller, named by the log on its first use:**

    Player is busy; deferring UI action to the action thread
    window: browse=on <- gateway.playback.on_browse_enter:23
    refusing a HUD engage while browsing <- app._yield:2294

`_yield` clears `_browsing` **first** and engages **last**, and the work in
between is not atomic. `_tell_controller("on_browse_leave")` reaches the
gateway, `run_action` defers because the player lock is held for the whole
of a playback start, and the next queue item -- the song -- runs
`enter_browse()` inside that window. The engage that follows is **stale by
the time it runs**: one call spanning a re-entry, not a fifth caller and not
a flag read early, which is why every call-site guard looked correct and why
neither harness reproduced it from a message sequence.

Pinned by `TheCursorNeverHidesOverTheLibraryTest`, which re-enters browse
from the leave callback -- exactly where the log shows it happening -- and
fails without the guard.

**Why the repair stays in `engage()`** rather than becoming a second check
inside `_yield`: the stale-engage shape belongs to any caller whose work can
span a re-entry, and `_yield` is simply the one that does. One authority, at
the point that can see the current answer. It can only ever refuse an
engage, never cause one.

The lesson worth keeping is about the instrument, not the bug: three
reproduction attempts from message sequences failed, and a one-line
`_caller()` on the refusal named it the first time it fired. Same reasoning
as `player_window._caller` -- "which caller it was IS the finding".

### F42 — the download-folder field remembers across pages

`_sync_path` is created once (`app.py`, `self._sync_path = {}`) and written
by the folder TextBox's `on_change`. **Nothing ever clears it.** So a path
typed once, on any visit to Settings, stays in that dict for the life of the
browser -- and the Move button reads it in preference to the value the field
is showing.

Found while fixing the reported "moving to an empty folder is a no-op",
which was the sibling bug in the same expression (`get("path") or val`
could not tell "not edited" from "cleared"). That half is fixed and tested;
this half is not, and it is the risk map's
"`_sync_path` is Tier 1 and destructive -- Move relocates the store to a
path the visible field is not showing".

**Why it is not fixed here**: the obvious repair -- seed the dict from the
current setting when the row is built -- runs on every repaint and would
clobber an edit in progress, which is the standing footgun of this shell
(docs/browser-shell.md: a screen is rebuilt from scratch on every repaint).
The right fix is to clear it when the settings route is entered or retired,
and that wants a look at `_retire_page` rather than a line in a builder.

Reachable, but it needs a typed-then-abandoned edit followed by a Move on a
later visit, and the destination is still confirmed for the empty case.
