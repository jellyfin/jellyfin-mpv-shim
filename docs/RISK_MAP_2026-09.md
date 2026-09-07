# Risk map — where defects can hide that nobody has looked

**Kind: measurement, not a plan.** This is the *measure* step of measure →
ratify → move the code (`~/Desktop/temperature-and-geometry.md` §3.2). Nothing
here is a decision. Several entries are verified defects that have deliberately
not been fixed, so that the map exists before the code moves under it.

Produced 2026-09-06 from five independent sources: a git-history mining pass, a
cross-feature invariant crawl, three risk-mapping surveys (accumulating state /
ordering / configuration cross-product), codex (a different vendor's model,
asked to refute rather than converge), plus direct measurement of churn and
co-change.

## How to read the status column

| status | meaning |
|---|---|
| **verified** | I read the cited lines myself and the claim holds |
| **reported** | a survey's claim, cited, that I did not independently check |
| **refuted** | claimed and checked and found wrong — recorded so it is not re-found |

Nine of eleven "verified" rows below were checked at `file:line` during the
session. Two agent claims were checked and **refuted** (see the last section);
the base rate matters when reading the "reported" rows.

---

## 1. The metric: would a human have trouble reasoning about this?

Izzie's framing, and it turns out to be the separator that matters. It is not
"is this code complex" — most of this codebase is unusually well documented and
individually legible. It is **can a person hold the thing that decides
correctness in their head at the moment they are editing.**

Four sources of difficulty, in rough order of how badly they defeat review:

1. **The rule is elsewhere.** Correctness depends on a paragraph in another
   file, or on a sibling call site 500 lines away. The line in front of you
   looks right, and is.
2. **The order is not in the code.** Two calls must land in an order that no
   lock, no flag and no call-site position enforces — only lock contention that
   varies with load.
3. **The state is invisible at the point of use.** A value set minutes ago in
   another feature decides what this line does, and nothing at this line names
   it.
4. **The symptom lands in another subsystem.** Even after the bug fires,
   attention goes somewhere else — which is why these survive user reports, not
   just review.

A finding that scores on 1 and 4 together is the year-later kind: invisible
when written, misattributed when found.

---

## 2. Broken multi-site rules — the evidence log

**This is the repo's dominant defect shape**: not a wrong rule, but a right
rule, written down in prose, applied at some sites and not others. Every row
has the code's own comment as the spec it violates, which is exactly why review
does not catch it — the comment raises the reader's prior instead of lowering
it.

| # | The rule, and where it is written | Where it is not applied | Status |
|---|---|---|---|
| R1 | `_NAV_KEYPRESS`: `"ok"` is resolved per press "because it is the one the user may remap" (`player.py:4858`) | `"back"`, two lines below, frozen as `"ESC"` while `kb_menu_esc` is equally remappable | **verified** |
| R2 | `app.py:1854` states the browse-block mechanism in full — "a forced binding that returns does not hand the key back" | `_shell_claimed_keys` hardcodes the literal `"SPACE"` instead of deriving from the bound `kb_*` set | **verified** |
| R3 | `ui_select_key`: "the renderer stops force-binding ENTER when this moves, which is the point" (`conf.py:730`) | `renderer.lua:5855` `PHUD_SUMMON_KEYS` holds a literal `'ENTER'`; `phud_wake_key()` on the adjacent line IS resolved | **verified** |
| R4 | `AutoDownloader.tick`: "the worker passes its own `should_stop`" (`sync/auto.py:164`) | `sync/manager.py:1649` `self.auto.tick()` passes none — while `self._download(row, stopping=stopping)` five lines below does | **verified** |
| R5 | `set_fullscreen`: "browsing writes browser_fullscreen, playback writes fullscreen" | the line below asked `_video is not None`, so music wrote the video key | **verified — FIXED** |
| R6 | `_library_showing()` is the question, never `_video is None` (`player.py:4934`) | `show_picture` refused a comic during music | **verified — FIXED** |
| R7 | `reset_picture_view` grew a `_video is None and not _loading` guard (`player_window.py:591`) | `set_picture_view` has none and reaches the player through the same deferring `_act` | **verified — open (F15)** |
| R8 | `apply_browser_fullscreen`'s docstring warns `set_fullscreen` also records `fullscreen_disable`, "a *user intent* flag" | `player_window.py:401` writes it ABOVE `if not persist: return`, so `update_check.py:258`'s app-initiated un-fullscreen latches it | **verified** |
| R9 | mpv-config discipline (`hwdec_pinned_by_config`) applied at four sites in `mpv_options.py` | missed at the fifth, ~500 lines away, under `mpv_ext` + `mpv_ext_no_ovr` | **reported** |

**Two rules in this repo already enumerate their own sites** and are the model
for repairing the rest: `tools/audit_stale_captures.py` (via
`tests/test_no_stale_captures.py`) and `tests/test_source_invariants.py`.

Three lints fall straight out of the table: every `kb_*`/`ui_select_key` value
against every literal key string; every `_video is None` test against
`_library_showing()`; every gateway `_act` property write against a
`PlayerManager` owner method.

---

## 3. What needs coverage or de-risking

Ranked by the metric in §1.

### 3.1 mpv teardown and re-creation residuals — **highest**

Scores on all four difficulty sources. `_init_mpv` resets 14 fields (plus 4
constructions); **75 constructor-bound names are not re-set, and nothing states
which of them are handle-scope.** Recreation therefore decides field by field,
from memory.

- Genuinely unenrolled: `_swept` / `_swept_ptr` (the key sweep of mpv #1 used
  to build mpv #2's input section — and the `except` path caches an *empty*
  sweep permanently, killing every key claim for the session behind one
  `log.debug`); `_key_claims` / `_key_actions`, which survive but self-heal **by
  accident** via `_bind_mpv_handlers` ordering that nothing declares.
- **mpv 0.40 cannot be dropped** (live distros ship it), and on that path
  minimising can require quitting and re-creating mpv — so this transition is
  on an ordinary UI path for a real share of users, not an edge case.

**Two caveats that rule out a naive checker.** `speed` is handle-scope but
lives *inside mpv*, not on the object, so no field enumeration can catch it.
And `_aspect_override` / `_deinterlace_override` survive **and are re-applied**
to the new handle — a two-category checker would flag them wrongly; the
enumeration needs a third category, "survives and is re-pushed".

The cheaper repair is a **place, not a checker**: one
`_reset_handle_scope_state()` called from `_init_mpv`, with the deliberate
survivors named in its docstring. Today the 14 resets are scattered across 240
lines and `_teardown_player` releases three resources and no bookkeeping.

### 3.2 `run_action`'s two execution modes — **high**

The same operation runs inline (lock free) or deferred (lock busy), and the
lock is held for the whole of a playback start. So the API encodes **no
happens-before contract**, and any two `_act` calls straddling the moment the
lock frees land backwards. Named independently by codex and by the ordering
survey; it is the mechanism behind two already-fixed bugs (the stretched film,
F15's construction).

Difficulty sources 2 and 4. A test can stage it (`test_playback_failure.py:220`
already holds the lock on a thread) but only *per pair*, and nobody has
enumerated the pairs.

### 3.3 Input ownership has no arbiter — **high**

Six independent claimants decide who owns a key: the user's resolved mpv
bindings, player-wide claims, the OSD menu's section, browser shell and page
claims, renderer-side modal/widget priority, and the gamepad's synthetic keys.
Some claims are cached across active/inactive transitions, so **history
matters**. Named by codex (#5), by the configuration survey (cell 3) and by the
multi-site table above (R1–R3) — three sources, three directions.

### 3.4 Configuration cells that switch code paths — **medium-high**

Verified: `headless=True` × `start_minimized=True` boots a kiosk to black
(`ui.py:353-368` — the `if browser.headless:` block is **not** an `else`, so
`minimize()` releases the window and the cast screen is then driven against a
window-less mpv). `auto.py:267` dispatches a **permanent, irreversible**
tombstone on `reason.startswith("un")` — the English prefix of a prose string.

Reported, unverified: `prefer_downloaded` (default on) makes every
`always_transcode`/codec setting inert for downloaded items, with a "Retry with
Transcode" button whose flag `offline_media.py:135` accepts and ignores;
`remember_window_size=False` leaves a stale `window_maximized=True`
write-gated and read-ungated, with the key hidden from the settings form.

### 3.5 State with no clear path — **medium-high**

Verified: `_sync_path` (SET 1 / CLEAR 0) is a never-cleared mirror that **wins
over** the saved setting the visible field was drawn from, and drives a
recursive relocation of the download store; `ThumbnailStore._gone` records
401/403 as permanent absence, keyed without server or user; `mpv.TIMEOUT` is
lowered 120s→5s for teardown and never restored, from a call site that also
runs on the *minimize* path.

Reported: `_login["pass"]` (SET 3 / RESET 0) leaves a cleartext password that
re-seeds the Add Server form — the adjacent PIN field has two clear sites.

### 3.6 Seam coverage has no number — **worth building**

`~/Desktop/temperature-and-geometry.md` §2.4 names this directly: *"there is no
common metric for seam coverage — the fraction of module-pair boundaries
exercised by something with both sides real. `jellyfin-mpv-shim` has this as a
harness but not as a number."* The harness exists (both backends × fake and
real mpv, plus the e2e tiers); the measurement does not.

---

## 4. Measured temperature and coupling

Churn over 18 months, code files only, sweeping refactors excluded.

**Temperature** — the UI stack has had no time to settle. `mpvtk_browser/app.py`
203 commits, **203 of them in the last 120 days**; `player.py` 181/162;
`mpvtk/renderer.lua` 123/123; `mpvtk_browser/ui.py` 82/82. There is no cooled
layer under the thing that hosts every feature.

**Repair ratio** — share of a file's commits that read as repair rather than
construction: `media.py` 35%, `mpvtk_browser/repository.py` 32%, `player.py`
31%, `sync/manager.py` 30%. The contrast is the signal: `mpvtk_browser/app.py`
is the *hottest* file at only 22% — new code being built. `media.py` and
`sync/manager.py` are **not new and a third of their commits are repair**,
which is unsettled for structural reasons rather than youth. Neither was
touched by this session's work.

**Coupling** — files that change together across subsystem lines:

```
47  conf.py            <-> mpvtk_browser/config.py     (managed: settings-curation.md)
32  mpvtk/renderer.lua <-> mpvtk_browser/app.py
30  mpvtk_browser/app.py <-> player.py
26  mpvtk_browser/ui.py  <-> player.py
20  mpvtk/renderer.lua <-> player.py
18  conf.py            <-> mpvtk/renderer.lua
18  mpvtk/renderer.lua <-> mpvtk_browser/config.py
```

`renderer.lua` co-changes with four Python modules **88 times**. That is the
Python↔Lua protocol: no schema, no type checking, a language barrier, and
settings plumbed by hand from `conf.py` into Lua.

**But the protocol surface itself is clean** — enumerated and checked: 18
`mpvtk-*` messages Python→Lua all registered, 3 `shim-trickplay-*`, 2 coming
back, no orphans in either direction. The names are all hot-path, so they fail
loudly and stay honest. The cold-path risk in that boundary is **payload
semantics**, especially the `mpvtk-debug` channel — where a silent no-op costs a
*test that quietly proves nothing* rather than a user-visible break. Measured
instance: `debug(cmd="rclick")` on a library tile does nothing, and only a
negative control revealed it.

---

## 5. Checked and found sound — do not "fix" these

Recorded so the ground is not re-walked. Additional to `docs/do-not-fix.md`.

- **epub is not a seam producer.** `pages/reader.py` asks the player only for
  `book_download_state`, which reads; the page is drawn with Pillow. "A film
  after a book" IS "a film after the browser". (`pages/comic.py` calls three
  mutating methods, which is why the comic *is* a producer.)
- **Live TV is not distinguishable as a predecessor** — no live-specific mpv
  state anywhere in `player*.py`, no `is_live` concept in `media.py`.
- **The Python↔Lua message surface has no orphans** (§4). A previous "two
  unhandled event types" finding was a **regex artefact** — `t = '[a-z_]+'`
  also matches `se`**`t = 'pbcopy'`** and `even`**`t = 'down'`**.
- **`_deinterlace_override` is well covered.** An agent reported it as having
  "no cross-feature test at all"; `tests/integration/test_picture_options.py`
  has `test_returning_to_the_library_ends_it` and
  `test_both_ways_out_of_playback_end_it`, the latter driving *both* doors
  through the gateway. **Refuted.**
- **The `_start` / failed-start key-claim strand** was reported and withdrawn by
  its own author on inspection: `claimed_keys` exists on exactly two pages,
  neither of which offers a Play button, and remote starts take a path where
  `load.error` is never set.

---

## 6. Method, and what it could not see

Five sources; three of them converged independently on the same three loci
(`run_action`'s dual semantics, lifetimes sharing one object, input arbitration),
which is the strongest evidence in this document. Codex was asked to refute
rather than confirm and did: it judged the cross-feature property-leak framing
"one layer too low", on the grounds that presentation mode, execution order and
object lifetime are implicit state machines and resetting properties one at a
time leaves the reachable-state problem intact. That judgement is why §3.1 and
§3.2 outrank the property work this session shipped.

Not assessed: the Lua side's own state (`renderer.lua`, `mouse.lua`,
`thumbfast.lua` hold state across mpv's script lifetime); persisted on-disk
state; a real Wayland compositor, real HiDPI, a real tray, macOS, a real
gamepad, a 12.0 server, a permission-restricted account. **No timing was
measured** — every race window in §3.2 is argued from code, not observed.
