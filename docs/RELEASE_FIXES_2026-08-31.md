# Release fix plan — v2.10.0 → f0c9de69

Plan of record for the 26 confirmed findings from the release review and its three
pattern audits. Same role as `UI_FIXES_4.md`: the findings live here, the sequencing
lives here, and a commit that closes one references its tag.

**Source material**, archived from `/tmp` on 2026-08-31:
[`docs/archive/release-review-2026-08-31/`](archive/release-review-2026-08-31/README.md)
— both review documents, all 15 reproduction probes, and the run logs. Every finding
below has a probe there.

Every finding below was re-verified against source. Nothing here is agent output taken
on trust.

---

## Decisions (settled 2026-08-31 — do not relitigate)

**Scope widened 2026-08-31, after Phases 1-3 landed.** Also in 3.0.0: F5, F8, F24, F9,
F22, F23, F10, F16, F17, F18, F19, F20. The criterion was "not a massive change to a hot
path", and none of them is — the largest are F10 (one serialiser over four call sites)
and F23 (a guard plus an in-flight marker). F24 is folded in with F8 because they are
the same function. **Still deferred: F15, F25, F26, F29, F30.**

F10 was conditional on the settings calls being slow enough to matter. Measured against
the QA server: GET 4.8 ms, POST 5.3 ms, so ~10 ms per read-modify-write on localhost —
but the race window is the whole round trip, and the real servers are remote HTTPS where
that is 50-200 ms. Two checkboxes on one settings tab, a fifth of a second apart, is an
ordinary action. It stays in.

**Original scope (superseded): 3.0.0 ships Phases 1-3 only.** Phases 4-5 are held. The reasoning is not
that they matter less: the app has been through several pre-releases and the bug
tracker has largely gone quiet, and Groups N/R are broad edits to browser navigation
and retry logic — the highest-risk place to be making 19 extra changes against a
release date. **A quiet tracker is an asset to protect, not a budget to spend.**

| Question | Decision | What it changes |
|----------|----------|-----------------|
| Is the startup PIN a security boundary? | **No — parental control.** It stops a kid on an HTPC opening R-rated films. | Phase 3 is the gate enumeration + catch-all test, **not** a startup-ordering redesign. Do not defer connection until unlock. |
| F13: which media source pre-negotiation? | **The item's `MediaSources`, as the details page already shows.** If the user picks something else, their pick wins — they consciously overrode it. | Reuse `pages/detail.py:327-337`. No second round trip. This also **confirms `explicit_tracks` is correct as designed** — it is the "user overrode it" signal, so leave it alone. |
| F25: in-flight download when Work Offline is toggled? | **Low priority — it is a dev feature**, and offline testing is now done with firejail (real network isolation) rather than this setting. Accepted as-is for this release. | Bottom of Phase 5. Given the use case is superseded, **"won't fix" or removing the setting are both on the table** — decide before spending effort on it. See the correction below. |
| F2: recovery contract for a failed relocation? | **Halt the copy, tell the user the disk is full, wait for them to fix it.** No automatic rollback. | See below — halting alone does not close the finding. |
| F17: behaviour change on upgrade? | **Accepted.** Remote arrows will start honouring `input.conf`. | Changelog line. |
| `sanitize_output` / "copy logs" | **Not a defect. Closed.** It is a hidden dev setting; switching it off means you want live URLs out of the log for debugging. | Removed from §5 as an open question. |

**Correction on F25, recorded because the decision was made on a different premise:**
work_offline does **not** lock out the API client. Every check in the tree is a
connect/reconnect gate — `clients.py:750` (websocket redial), `:845` (cast verify),
`:1064` (health check) — plus browser source selection. There is no gate in the request
path and none in the apiclient, so an already-connected client keeps serving requests
and the sync worker streams the file **to completion**. The decision to deprioritise
stands on "dev feature", not on "it halts on the next request".

**F2 needs more than the halt.** "Halt and report" is the right UX and settles what the
user sees, but it does not by itself stop the data loss, because the destruction
happens *before* the failure is noticed. Halting is only safe once the mechanism is
fixed too:

1. **Do not delete originals as you go.** Copy the whole tree, verify, then remove
   sources. Halting mid-copy is then non-destructive by construction — the originals
   are all still there and the partial destination can be cleaned or resumed.
2. **`_reconcile_disk` must never run against a catalog it just created.** This is what
   turns "the move failed" into "the downloads are gone": the empty catalog makes every
   surviving media directory look like an orphan. Gate the sweep on having opened a
   catalog that already existed.
3. **Fix the message.** It currently says the downloads were left in place, which is the
   one thing that was not true.

A free-space precheck is a nice-to-have on top; it is not a substitute for (1) and (2),
since ENOSPC can arrive from another writer filling the disk mid-copy.

## Merge gate

**A green integration matrix is the merge gate for this branch** (agreed 2026-08-31).
Not the unit suite, and not e2e: both pass without touching the integration harness's
stand-ins, and this branch was red for several commits with both green because
`RealVideo` did not model two methods `play()` had started calling. Four legs failing,
invisible to 5,177 unit tests.

Run it as `xvfb-run -a python3 tests/integration/run_integration.py` and **read the leg
table, never `$?`**. The exit code has now been masked twice in one session by things
appended to the command — a `| tee`, and a trailing `echo "RC=$?"` — and the runner
also exits 0 by design when a leg's tests all skip. `N/M legs passed` and the per-leg
`[N run, M skipped]` counts are the evidence.

## 0. Read this before changing anything

Twenty-six fixes across four subsystems is where a release review starts producing
regressions of its own. Three rules, each of which this pile has already earned:

**Rule 1 — nine of these findings sit under a comment asserting the behaviour is
correct.** `player.py:3484-3490` promises stop-then-mark ordering; `app.py:762`
promises Back cannot land on an unfinished load; `_seek_like_the_keyboard`'s docstring
argues in detail for behaviour it does not implement; `_warm_library_later`'s docstring
justifies a deferral that is safe for shaders and unsafe for `hwdec`. **Fix the comment
in the same commit as the code.** A stale comment here is not cosmetic — it is what
made these survive review the first time.

**Rule 2 — nine of these have an existing test that structurally cannot fail.** Not
"uncovered": the test exists, is green, is named after the property, and the property
has nowhere to live in it. See §4. **For those, rewrite the test first and watch it
fail**, or the fix is unverifiable and you have bought a green suite and nothing else.

**Rule 3 — do not "fix" the things in §5.** Several correct decisions in this code look
exactly like the bugs listed here.

---

## 1. Do NOT change these

Load-bearing code adjacent to a fix, which will look like the bug when you are three
findings deep:

| Site | Why it stays |
|------|--------------|
| `player.py:3904` `configure_streams`' `not video.is_transcode` gate | Correct. Transcoded audio is baked into the stream; selecting a track client-side is a lie. Pinned by `tests/e2e/test_track_selection.py`. **F6/F13's fix is upstream of it, never here.** |
| `app.py:1272` `failed()` not epoch-gated | Deliberate: an error is a rollback, and a route you navigated away from must still hold its error when you return. **F22's fix is a distinct load token, not epoch-gating `failed`.** |
| `app.py:2592` `_claim_page_keys` called unconditionally | Deliberate — it is what makes *leaving* a page drop its claim. **F19's fix is ownership serialisation, not a guard that breaks claim release.** |
| `_apply_auth_headers` returning False | Not a failure path. False means "the URL carries its own token", which is the safe fallback. |
| `_move_tree` skipping names already in the destination | Deliberate anti-clobber. A rollback must preserve it. |
| `_expand`'s broad `except` | Correct for every type except Playlist. **F12 distinguishes failure from empty; it does not narrow the catch.** |
| `mpv_options.py` OrderedDict insertion order | Deliberate, documented, and not related to any finding here. |
| `keysweep` caching the sweep (`player.py:1839`) | Deliberate: a re-sweep would see our own non-weak lines and drop every claim. |

---

## 2. Findings, grouped by the code they touch

Grouped because **findings in one group edit the same functions and will conflict
textually if worked in parallel branches.** One work stream per group.

### Group S — `sync/` durability · 5 findings · **contains all three data-loss bugs**
| Tag | Site | Defect |
|-----|------|--------|
| ~~F2~~ **DONE** | `sync/manager.py:359` `_move_tree` | Copy-then-delete per entry; catalog moves first, media copy fails, reopen builds an empty catalog, `_reconcile_disk` deletes the survivors as orphans. Error says "left in place". **Destroys downloads.** |
| ~~F11~~ **DONE** | `sync/auto.py:243` → `:324` | Reaper snapshots complete auto rows, spends seconds in per-row HTTP, then deletes by `item_id`; `delete_item` never re-reads `origin`, so a download the user claimed mid-pass is deleted. **Destroys media.** |
| ~~F12~~ **DONE** | `sync/manager.py:595` via `:752` | `_expand` swallows all errors → `[]`; `enqueue` has no empty guard; `_record_playlist([])` calls `delete_playlist`. Returns 0, does not raise, UI reports success. Ownership loss is permanent. **Destroys the playlist record.** |
| ~~F7~~ **DONE** | `sync/manager.py:1021` | Snapshot → network → `clear_playstate(ids)`; `upsert_playstate` updates in place, so the ack deletes unsent progress. |
| F25 | `sync/manager.py:918`, `sync/auto.py:149` | `work_offline` appears nowhere in `sync/` except playback source selection. Live toggle leaves the worker streaming on a metered link. |

### Group P — the playback start sequence · 4 findings · **highest interaction risk**
| Tag | Site | Defect |
|-----|------|--------|
| ~~F1~~ **DONE** | `player.py:2294` → `media.py:594` | Auth header installed before the URL exists; a direct-path `Http` source sends the token to a third-party host. |
| ~~F13~~ **DONE** | `media.py:830` vs `:838` | `language_config` resolved *after* `get_play_info`, so it is inert for every transcode. Reaches every queue advance (`get_next` drops `explicit_tracks`). |
| ~~F6~~ **DONE** | `player.py:2705` | Remembered episode tracks applied after the URL loads; same root cause as F13, narrower reach. |
| ~~F14~~ **DONE** | `video_profile.py:576` vs `player.py:2524` | Library-scope shader profile resolves off-lock and lands after `hwdec` was written and the decoder initialised. |

### Group L — lifecycle gates and identity · 4 findings
| Tag | Site | Defect |
|-----|------|--------|
| ~~F3a~~ **DONE** | `clients.py:206` | `PeriodicHealthCheck` starts in `__init__`, so it reconnects regardless of the PIN gate. |
| ~~F3b~~ **DONE** | `app.py:2451` | `set_source()` clears `_locked` unconditionally. |
| ~~F21~~ **DONE** | `app.py:901` | `on_nav_command("home")` never checks `_locked`; a remote `GoHome` walks past the gate **and permanently disarms it** (`show_locked`'s idempotence guard then no-ops forever). |
| ~~F4~~ **DONE** | `clients.py:933` | `switch_user` does not invalidate in-flight auth; publication checks shutdown/removal, and the switch clears `_removed_uuids` itself. |

### Group N — browser navigation and loading · 3 findings
| Tag | Site | Defect |
|-----|------|--------|
| F8 | `app.py:727` `_land_back` | Does not restart a load the epoch dropped → permanent spinner. |
| F24 | `app.py:701, 715` `_land_back` | Its two reload branches drop `_items` but not `_pages`/`_win_tried`, so a deleted item is served from the stale page cache. **Same function as F8.** |
| F22 | `app.py:1270` | `_load_ep` stamps the *epoch*; `refresh_home` loads without bumping, so two same-epoch loads are indistinguishable and a late failure drops a working session onto the offline catalog. |

### Group R — retry / in-flight markers · 2 findings
| Tag | Site | Defect |
|-----|------|--------|
| F9 | `pagination.py:342` | Failure clears `_page_loading`; `AsyncRunner`'s bare `finally` invalidates; next `ensure()` refetches. Unbounded loop. Windowed mode already has the fix (`_win_tried`). |
| F23 | `pages/comic.py:459-471` | Same defect on the `elif` arm (no `_error` guard, no in-flight marker) **plus** the mirror on the `if` arm: `close()` keeps `_error` while dropping `_comic`, so in-place recovery is unreachable after a Back. |

### Group W — shared-document writes · 2 findings
| Tag | Site | Defect |
|-----|------|--------|
| F10 | `repository.py:803, 697, 736, 1153` | **Four** unserialised GET-whole-DTO / mutate / POST-whole-DTO writers on one document. Zero locks in the file. Two share a settings tab. |
| F16 | `shader_overrides.py:91` | Bare `open(w)` truncates the whole override store; the only non-atomic whole-file write left in the tree. |

### Group K — input claims · 4 findings
| Tag | Site | Defect |
|-----|------|--------|
| F18 | `keysweep.py:161` | `_rank` never filters `priority < 0`, so a **disabled** non-weak binding outranks mpv's live builtin and gets activated. |
| F17 | `player.py:4512` | Remote seek: uppercase/lowercase key mismatch **and** a parsed tuple fed to a string parser. Both land on the stock default. Not a race — always wrong. |
| F19 | `app.py:2591` | A stale reader frame reinstalls SPACE after yielding to video; SPACE stops pausing for the session. |
| F20 | `menu.py:151` vs `:214` | Menu keys claimed on the outgoing mpv handle before `force_window` recreates it. |

**F14 — considered and rejected:** prefetching the library id on the detail page. It
would keep the play path free of the request entirely, but it only helps items reached
*through* a detail page — not Play All, not a queue advance, not a cast — so the play
path would still need the lookup as a fallback and nothing would be saved. Decided
2026-08-31; do not re-propose without a reason the fallback goes away.

### Group Y — found while fixing, not in the original review
| Tag | Site | Defect |
|-----|------|--------|
| ~~F28~~ **DONE** | `media.py` `get_playback_url` | **PlaybackInfo silently ignores `AudioStreamIndex` unless `MediaSourceId` is sent with it**, falling back to `DefaultAudioStreamIndex`. Measured on Jellyfin 12.0: six different requested tracks all returned the default; adding the id returned each one, in both query and body form. `media_source_id=self.srcid` is None for any ordinary play, so **audio track selection on a transcode was inert for every normal playback** — including the one F13 had just arranged to resolve at the right moment. Only sent when a track is actually requested, so a multi-version item still lets the server choose. Found by e2e and *only* by e2e. **NOT a v12 regression** — measured against a 10.11.11 container on the same fixtures: `aid=1/4/5` all return the source default (3) without `MediaSourceId` and the requested index with it, exactly as on 12.0. So this has been broken for as long as both versions have existed, and there is nothing to report upstream. |

### Group Z — reported from the field, NOT yet reproduced
| Tag | Site | Report |
|-----|------|--------|
| F29 | `player.py` load gate / `_on_cache_pause` | **User report, Windows 11 + external mpv (shinchiro), NAS drives asleep:** playing a video straight after waking the HTPC makes mpv loop ~2-3 s until the NAS spins up; skipping back a few seconds recovers it. **On 2.10 the screen stayed blank instead.** |

**What is established.** The change in symptom is explained by `672ef1ac` ("stop gating
the start on a duration that may never arrive"). Before it, the start waited for
`duration`; a stalled SMB source never reports one, so the wait ran out the whole
`playback_timeout` and stopped playback — a blank screen, exactly as reported for 2.10.
After it the wait is *also* satisfied by `file-loaded`, which fires as soon as mpv has
the tracks, so the start now proceeds and playback runs against a source that is not
delivering. That commit is right about its own case (a live channel never reports a
duration) and this is its cost.

**What is NOT established:** what produces the ~2-3 s loop. Candidates read but not
confirmed — the offset seek applied while the demuxer is starved, or mpv replaying its
small cache. Nothing was reproduced, so nothing here is a diagnosis.

**One real gap found while looking.** `_on_cache_pause` (`player.py:1567`), the observer
on `paused-for-cache`, returns immediately unless SyncPlay is enabled. Outside a
SyncPlay group the client does **nothing** when mpv stalls on its cache: it neither
surfaces the state nor recovers from it, so a starving source is indistinguishable from
a broken one. That is not the loop's cause, but it is why the user has nothing on
screen telling them the NAS is still spinning up.

**Before acting, ask for:** the shim's `log.txt` from the affected run (**grabbed before
relaunching — it is rewritten on every start**) and mpv's own log, plus whether
`direct_paths` is on (the report says SMB and a NAS, which implies it).

| F30 | `player_window.py:131` (`clear_media_title`) | **jsonipc only; libmpv passes.** `tests.e2e.test_playback_eof.WindowTitleTest.test_stopping_puts_the_title_back` fails against a real server: after `stop()`, `media-title` still reads the previous item (`'Pilot' != ''`), so the window names a stopped film over the library. The docstring states the assumption that breaks — *"Every caller therefore clears after its `stop` command, **which both backends complete before returning**"* — and its own second paragraph explains why that is fatal rather than cosmetic: clearing `force-media-title` while a file is still loaded does not empty `media-title`, it falls back to the file's own metadata title. Which is exactly the observed value. Mechanism inferred from the failure shape, **not measured**: likely `command("stop")` returning on the IPC ack rather than on the unload. Same family as the `python-mpv-jsonipc` observations in `~/bin/python-mpv-jsonipc/ISSUES-2026-08-31-*.md`. Found in the first e2e run of this session and filed late — it was not in the original review. **INTERMITTENT: it has not reproduced since.** Re-run on 2026-08-31 after Phase 2: passes on libmpv, passes on jsonipc, and passes three consecutive times on jsonipc alone. Nothing in Phases 1-2 touches `force-media-title` or the stop path, so this is very unlikely to be fixed — it is a race that lost once, which is exactly what the inferred mechanism predicts. **Do not close it on the strength of a green run**; it needs the measurement (does the IPC ack precede the unload?) or it stays open as a known flake. |

### Group T — tooling (not shipped code)
| Tag | Site | Defect |
|-----|------|--------|
| ~~F27~~ **DONE** | `tests/integration/run_integration.py` | The matrix **hangs indefinitely after the final leg has already passed**. A test mpv outliving its leg is reparented to PID 1 while still holding the write end of that leg's stdout/stderr pipe; the runner reads to EOF, which can never arrive. Signature: runner `WCHAN=pipe_read` at 0% CPU, its `xvfb-run` child `<defunct>`, and `/proc/<mpv>/fd/1` pointing at the very inode the runner blocks on. Unwedged by `kill <mpv pid>` — by PID, never `pkill -f`. Real fix: reap the child then read with a deadline, or give mpv its own process group and closed fds. The 2026-08-30 review saw this and could not trace it; traced 2026-08-31. Costs a ~7-minute run whenever it fires, and would hang CI forever. |

### Group X — isolated
F5 report ordering (`player_reporting.py:542` vs `media.py:900`) · F26 cast parks the
last composite (`cast.py:319`) · F15 `set_picture_view` guard asymmetry
(`player_window.py:516`, **unverified — construct the interleaving before fixing**).

---

## 3. Order of work

Sequenced by dependency and by blast radius, not by severity alone.

**Phase 1 — stop the bleeding (data loss).** Group S, in this order:
F2 → F12 → F11 → F7, with **F25 dropped to Phase 5** (dev feature, see Decisions).
F2 first because a botched relocation is unrecoverable and the other three are not.
F2's contract is settled: copy-all-then-delete, gate `_reconcile_disk` on a
pre-existing catalog, halt and report "disk full" without an automatic rollback. All five edit `sync/manager.py`; **do them as one
stream, not five branches.** F11's fix (re-read `origin` inside `delete_item`, or
version the snapshot) is the same mechanism as F7's (ack by value, not id) — and
`app.py:2698 clear_status_if` is the in-tree model for both: **it acks by value.**

**Phase 2 — Group P, as a single restructure.** These four must be planned together
because F1 pulls the header decision *later* (it needs the resolved URL) while F13/F6
pull track resolution *earlier* (before `get_play_info`). F13's source is settled:
resolve against the item's `MediaSources` exactly as `pages/detail.py:327-337` already
does, so the picker and the audio finally agree, and leave `explicit_tracks` alone —
it is the "user overrode it" signal and it is correct. Doing them separately means
restructuring `play()` → `get_playback_url()` twice, with the second pass invalidating
the first's reasoning. Note F1 is **not** a one-line addition: `media.py:295-301`
records the "decide before PlaybackInfo" inversion as deliberate, so it needs either a
second pass after the URL exists or the install moved below `get_playback_url()`.
The model to copy is in-tree: `sync/manager.py:_headers_for` + `_same_origin`, with
`tests/test_sync_auth_headers.py` enumerating call sites so a new one cannot regress.
F14 rides along — it is the one leak out of the otherwise-disciplined
`player.py:2478-2549` block.

**Phase 3 — Group L, as one "enumerate every door" change.** Scope is settled by the
parental-control decision: enumerate the doors and add the catch-all test. Do **not**
defer connection until unlock. F3a/F3b/F21 are three
holes in *one* gate; patching them individually invites a fourth. The model is
`headless`, which the audit found genuinely airtight *because it is catch-all
enumerated*. Give `_locked` the same treatment and add the enumeration test. F4 lands
in the same file and is independent (generation counter pinned at `_connecting.add`,
re-checked at register).

**Phase 4 — Groups N and R.** F8 and F24 edit the same function; do them in one commit.
F22 needs a distinct load token — **do not** reach for epoch-gating `failed` (§1).
F9 and F23 are the same fix shape twice; F9's answer already exists as `_win_tried`
eight lines away, and `pages/reader.py` is the correctly-written sibling of F23.

**Phase 5 — Groups W, K, X.** F10 is one serialiser covering four call sites
(`gateway/editing.py:24 playlist_move_many` is the in-tree model: run the sequence in
order on the calling thread). **F18 before F17** — F17's fix consumes the sweep's
output, so fixing the ranking first means F17 is written against correct data. F16 is
mechanical (tmp + `os.replace`, matching `conf.py:876` / `users.py:177`). F25 sits at
the bottom of this phase. F15 last, and only after the interleaving is constructed.

**Phases 4-5 are held out of 3.0.0.** They are the plan for the release after.

---

## 3b. Progress

**F2 — done** (branch `fix/release-phase1-sync`, uncommitted). Three changes, matching
the settled contract:

1. `_open_and_run` gates `_reconcile_disk` on the catalog having **pre-existed**. This
   was the actual destroyer — `_copy_tree` raised, so the copy loop never deleted the
   media; the *sweep* did, against the empty catalog the failed move left behind.
2. `_move_tree` moves `catalog.db` **last** and defers deleting copied sources until
   every entry is across, so a failure leaves the old root coherent.
3. A half-written destination is removed on failure, so a retry after freeing space
   cannot hit the "already there, skip it" guard and silently finish a partial tree.
4. ENOSPC gets its own message naming free space; the generic one no longer claims
   "left in place" for a case where that was untrue.

**The pre-existing `test_move_failure_leaves_downloads_in_place` was a §4 test and is
now flagged as one** — it stubs `_move_tree` wholesale, so no partial state can exist
and it could never fail. Kept (it covers the raise-before-starting case) with a
docstring saying why it is the easy half, plus four new tests. Both fixes were
**mutation-tested**: reverting the gate fails the media assertion, reverting the
ordering fails the catalog assertion, so neither test passes on the other's work.

Suite: 5,130 unit tests pass (5,125 before + 5 new). `base.pot` regenerated; no `.po`
touched.

**F12 — done.** `_expand` now raises `ExpandFailed` instead of swallowing into `[]`.
The catch was *not* narrowed (§1): it still catches everything, it just no longer
reports failure as emptiness. Two documented contracts above it were being defeated by
that one `return []` — `gateway.download_enqueue` ("Raises on failure") and
`gateway.download_estimate` ("a zero estimate made failure indistinguishable from
already fully downloaded", hiding the retry control) — so the whole chain above already
wanted this and needed no changes.

`AutoDownloader.fill` catches `ExpandFailed` **per item and continues**: letting it
reach `tick()`'s catch would abort the pass, and the next pass would hit the same item
first and abort again, never reaching the candidates behind it — the head-of-line block
`_next_runnable` exists to prevent, reintroduced one layer up. The import is per-call
(`_expand_failed()`), because `manager` imports `auto` and the module-scope version is a
circular import that breaks the package.

Three new tests, all mutation-verified. One caught a fixture bug of my own first:
`list_playlists()` only returns playlists with a COMPLETE item, so the original fixture
left its rows PENDING and the assertion would have read `[]` both before and after.
The fixture now asserts its own precondition.

Suite: 5,133 unit tests pass.

**F27 — done, and fixed first**, because the matrix could not be trusted to terminate
unattended and everything else in this plan is validated by it.

`_run` now pumps the leg's output on a thread and treats **the leg's exit** as
end-of-leg, never pipe EOF. The leg leads its own process group
(`start_new_session=True`), so on the way out anything it leaked is killed as a unit —
the discipline `tools/run_tests_parallel.py` has had from the start and this runner
never got. Two diagnostics: one when something is still holding the pipe (the hang),
one when a leg leaked a process whose output was already closed (the graveyard).

Validated end-to-end: a full matrix ran unattended in **9m27s, rc=0, all 8 legs**, and
printed `*** whole suite [jsonipc]: the leg exited but left a process holding its
output pipe` — i.e. it caught the real leak on the same leg that wedged the run before,
and recovered instead of hanging. Mutation-tested: restoring the inline EOF read fails
the new test in 25s with its own message rather than hanging the suite.

**Follow-up, not filed as a finding:** that leak is reproducible and specific to the
`whole suite [jsonipc]` leg — a test mpv is not being torn down on that backend. The
harness now tolerates it; nobody has found out why it happens. Worth a look before
trusting jsonipc teardown in production.

**F11 — done** (`9de2d1a0`). The claim is **atomic, not merely narrower**:
`db.delete_if_auto()` checks the origin and deletes under one lock acquisition, and
`update()` takes the same lock, so the window is closed rather than shrunk. Re-reading
the origin without the lock would have been the tempting fix and is not one.

`delete_item(only_if_auto=True)` deletes the row **before** removing the files, which
also settles the "Minor" ordering item the durability audit raised for this path: a
failed unlink leaves orphans the next reconcile sweeps, while files-first with a failed
row delete leaves a COMPLETE row pointing at nothing that the same sweep answers by
re-downloading it. The main `delete_item` path is unchanged and still files-first —
that half of the minor finding is open.

`_delete` now returns whether the row went, so a skipped item is not counted as reaped,
not credited as a cap eviction, and **not tombstoned** — a tombstone would be the reaper
recording a decision about an item that is no longer its business.

`FakeManager.delete` models `only_if_auto`; without it the fake deletes what the real
manager refuses to, leaving the race untestable while reporting a pass. Suite: 5,136.

**F7 — done** (`eb909b49`). `clear_playstate` takes `(id, position_ticks, played)` as
read and deletes only where the row still holds them; a row that moved on stays pending
and goes out next sweep. `app.py:2698 clear_status_if` is the model — it has acked by
value all along.

`IS` not `=`: both columns are nullable and `NULL = NULL` is NULL, which matches nothing
and leaves the queue **undrainable**, re-pushing the same mark on every reconnect. That
has its own test; swapping the operator fails it.

Three tests — the lost update, the control that an untouched row still clears, and a
three-sweep case per the multi-step rule pinning that a disturbed row eventually leaves
the queue rather than being replayed forever. Suite: 5,139.

---

### Phase 1 complete

F2, F12, F11, F7 done and committed; F25 was moved to Phase 5 by the Work Offline
decision. **All four data-loss findings are closed.** Group S is finished.

**F1, F13, F6 and F28 — done.** Phase 2's ordering conflict resolved more cheaply than
the plan assumed: F1 keeps the install where it was and **revokes** the header once the
origin is known, so the "mpv refuses http-header-fields, url carries the token instead"
fallback survives — a "decide later" restructure would have stranded it.

**Two things only e2e could find**, which is the lesson of this phase:

1. **F28.** F13's fix was correct client-side and had *no user-visible effect*, because
   the server drops a stream index sent without its source. The unit tests passed
   because they hand-build items; the real server does not behave like the fake.
2. **`TranscodedTrackTest` was tautological.** Its `setUp` said "the last one: never the
   server's default" — on the current fixture the last audio track *is*
   `DefaultAudioStreamIndex`, so the only transcode track coverage in the tree asked
   for the one index a client ignoring the request entirely would still return. It now
   picks a non-default track and asserts that premise. **Add this to §4's list of tests
   that cannot fail** — it was not in the original ten.

Also caught, by the unit suite this time: `play()` now calls a new hook on every video,
and `OfflineVideo.__init__` deliberately skips `super().__init__` — so the state had to
be a **class** attribute and `OfflineVideo` needed an explicit no-op override, or
offline playback raised AttributeError. The online tests could not see it.

Suite: 5,155 unit; 21 e2e track-selection tests pass against Jellyfin 12.0.0.

### Phase 3 complete

F3a/F3b/F21 landed as one enumeration (`dad789c5`) and F4 as a connection
generation (see below). **All of Phases 1-3 — the 3.0.0 scope — are now done.**

The PIN gate is enforced where headless already is: `Navigator.allows`, which
`navigate()` consults before touching the stack. **Removing that check makes the
catch-all report 30 reachable routes**, against the two doors that had individual
checks. `_default_route` is lock-aware for the same reason it is headless-aware —
every stack-emptying path backfills through it, so without that `set_source`'s reset
still landed on Home while the PIN was unanswered.

Clearing the flag moved to the unlock handler: `set_source` was doing double duty as
"a source arrived" and "the user answered the PIN", and only the first is what its
callers mean. Two pre-existing tests caught the move immediately.

Per the settled decision, connections still happen while locked — the gate is about
what is on screen, not the network — and `tests/test_mpvtk_locked.py` says so rather
than implying a boundary it does not provide.

## 4. Tests that must be rewritten *before* the fix

Each of these is green, is named after the property, and cannot observe it. Fixing the
code without fixing the test yields no evidence.

| Finding | Test | Why it cannot fail |
|---------|------|--------------------|
| F5 | `tests/test_playstate_mirror.py:498` | `pm._reporter = mock.Mock()` — the queued lambda never runs, so the assertion is over Python call order, which is exactly what is *not* the guarantee. |
| F13 | `tests/test_remote_playback.py:166` | Asserts `video.aid == 2` on the object, never that PlaybackInfo carried it. |
| F13 | `tests/e2e/test_track_selection.py` | The only transcode track coverage builds `Media(explicit_tracks=True)`, returning at `media.py:383` before the rule runs. Bypasses the path by construction. |
| F11 | `tests/test_auto_download.py` `ReapProtectionTest` | Pins only the static case; `FakeManager.delete` never re-reads origin and no test mutates the catalog mid-pass. |
| F12 | `tests/test_playlist_offline.py:146` | Pins the empty-expansion branch from an empty catalog with a *successful* server — and by pinning it, makes the bug look intentional. |
| F22 | — | `_load_ep` / `LOAD_EP_KEY` appear nowhere in `tests/`. |
| F24 | `tests/test_shell_delete_item.py:234` | Asserts only `assertNotIn("_items", ...)` — one step, and the stale `_pages` is the other one. |
| F23 | `tests/test_shell_comic.py:656` | Sets `_error` *after* a successful open, so `_showing` is True and the `elif` never fires; and it is a scene assertion, not a repaint one. |
| F10 | `tests/_shell_harness.py` | Each fake records to its own list, so **one shared document is not representable** — the clobber cannot be written as a test until the fake models the DTO. |
| F18 | — | No coverage of inactive non-weak sections. Needs a real-mpv regression test. |

Two standing rules from `CLAUDE.md` that most of the above violate, worth re-reading
before writing replacements: assert the property over ≥3 steps rather than the
mechanics of one, and **a scene assertion is not a repaint assertion** — F23 and F9
both need `invalidate` counted, not inferred.

---

## 5. Cleared — do not re-review

Recorded so this ground is not covered a third time. Traced and clean: every outbound
credential path other than F1 (13 raw request sites, thumbnails, sync downloader, cast
art, external links); `db.py`'s three `userdata_json` RMWs (fully inside `_lock`);
`SessionReporter` (single-worker FIFO by design); the server's `POST
UserItems/{id}/UserData` (a genuine partial patch, not a whole-document RMW);
`trickplay.py` (re-checks the video after every blocking step; `_covers` records the
*asked* span — the Pattern D fix written out); `tile_renderer._request_image` (backoff,
max attempts, 4xx negative cache); `pages/reader.py`; every `_start_daemon` poller;
`headless`; `update_check.py`; the pointer/hover path across suspend/resume; the HUD
across mpv's console on both backends.

Investigated and **not** promoted, with reasons: the websocket redial loop's missing
`_switching` check (`clients.py:743` — `WebSocketDisconnect` only fires from
`stop_client`, and all four callers silence or drain first, so it is unreachable →
hardening, not a defect); strip composites in flight across a theme change
(mechanism real, trigger unverified); `cast_ready` (write-only since the Tk browser
was deleted — stale comment); `_sync_playstate` never retiring a permanently-rejected
entry (a poll, not a repaint loop; the download side grew `_record_permanent_failure`
for exactly this); a page claim raising the renderer's forced section above mpv's
console (mechanism demonstrated, no ordinary workflow reaches it — latent hazard).

**Settled, not a defect:** `log_utils.py:159 ring_handler` uses a non-forced formatter,
so with `sanitize_output: false` the in-app log viewer and "copy logs" hand out
unredacted text while `log.txt` stays clean. That is the point — it is a hidden dev
setting, and switching it off means you want live URLs out of the log for debugging.
