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
