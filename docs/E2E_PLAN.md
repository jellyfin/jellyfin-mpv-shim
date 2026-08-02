# End-to-end testing against a real server — plan

Branch `e2e-testing`. The server is [stdjflib] `serve` against
`~/Desktop/std-jf-lib` (`--live-tv` wires in [faketvsource]).

The suites are large — ~2900 unit tests, 154 Lua, an integration matrix on
both mpv backends — and they are green while real bugs ship. So the question
this document answers is not "what should we test", it is **"what has actually
broken, and why was every existing test structurally unable to see it"**. The
test list falls out of that.

Sources mined: `docs/REGRESSION_CHECKLIST_2026-07-25.md`,
`docs/UI_UX_FIXES_2026-08.md` (three hand-testing rounds),
`docs/ISSUES_TO_VERIFY.md`, `docs/archive/`, and the fix commits themselves.

## Six failure classes the current suites cannot see

### 1. The request is wrong, and the fake accepts it

Every browse test runs against a hand-written fake source. A fake never
validates a request — so a wrong parameter name, a wrong type, or a right
parameter sent to the wrong endpoint is invisible until a human opens the
screen.

| What broke | How it presented | Commit |
|---|---|---|
| `get_channel_listing(enable_images=False)` — no such argument | `TypeError` before the fetch; the Live TV channel page simply did not open | `b97dd523` |
| Category flags derived as `"is_" + name`, so `is_movies` | `TypeError` on every filtered Guide/Channels fetch. **The unit test asserted the wrong spelling, which is why it was green** | `4bc952c1` |
| Categories sent to `LiveTv/Programs` as well as `LiveTv/Channels` | Empty guide. `IsMovie` is a column predicate, `IsSports`/`IsNews`/`IsKids` become a tag filter, so two categories AND to nothing | `4bc952c1` |
| `SyncPlay/Ping` sent a float against a `long` DTO | 400 on **every ping this client has ever sent**; the server silently used its default latency instead | `600022e8` |

`tests/test_source_invariants.py` now walks the AST for `api.*(kw=...)` and
checks each keyword against the client's signature, which closes the first two
rows. It cannot close the last two: those are *semantics*, and only a server
answers about semantics.

`CLAUDE.md` already records a dozen more claims of this kind as prose —
DisplayPreferences must use client `emby`, guide prefs must be the *strings*
`"true"`/`"false"`, times are UTC with seven fractional digits and an
offset-less bound answers the wrong window, per-library Latest rows bypass
`LatestItemsExcludes` and must apply it client-side. Every one of those is an
assertion about a running server that nothing executes.

### 2. DTO fields the fake fabricates

The fake source returns dicts that are *plausible*. The bugs are all in the
same direction: the real DTO is **less** populated than the fake, or carries
the field somewhere else.

| What broke | Missing field | Commit |
|---|---|---|
| Downloaded shows rendered as squares, not posters | synthesized offline Series DTO had no `PrimaryImageAspectRatio`; `auto_geom`'s no-ratio fallback is square | `8a946e39` |
| Opening a season while offline listed nothing | synthesized offline Season DTO never set `SeriesId`, so `get_episodes` filtered every episode away | `5847cd20` |
| A `.strm` queue froze on the last frame | `RunTimeTicks` is absent on the Item and present on the MediaSource; no duration disabled the EOF fallback | `8589cc4c` |
| Stopping a photo printed a traceback | a photo has no `playback_info` at all; `get_timeline_options` was taught that, `_report_stopped_offline` was not | `690fb82f`, `662d4b06` |

**This is the highest-leverage thing in the document.** A conformance test that
compares the real source's answers against the fake's — and fails when the fake
is *richer* than reality — keeps ~2900 fake-based tests honest for as long as
they exist. There is precedent in the repo:
`tests/test_mpvtk_fake_conformance.py` does exactly this for `FakeMPV`.

### 3. The playback loop with a real server actually in it

`test_realmpv_smoke` plays a real clip through a real mpv — and fakes the
Jellyfin session, deliberately (see `tests/integration/README.md`). So the
shim's half of the loop is asserted and the *round trip* is not: whether
progress lands, whether the resume point comes back, whether the next episode
plays.

This is the cluster with the worst history — auto-advance and watched-marking
are one of the two largest open-bug groups in the tracker (#157, #323, #458,
#541), and the 07-25 checklist calls close-mpv-then-cast-again "the
highest-value single item on this page".

It is also **cheap**: `stdjflib` episodes are 10 seconds long. "Play an episode,
watch it finish, watch the next one start" costs about 25 seconds of wall
clock.

### 4. Server state that another client can see

Nothing in the suite has ever had a second session.

- Continue Watching updating after something finishes elsewhere (#560).
- The Home Screen tab saving without degrading the same user's jellyfin-web
  layout — section types the shim cannot draw are meant to be *preserved*.
- `qa-onesession`: a second login must evict the first.
- SyncPlay against a real group.

### 5. A route that never renders

`strict_builds` is off in production, so a scene-build exception keeps the last
good frame. A broken route does not look like a crash; it looks like a UI that
ignored the click. The 07-25 checklist's procedure is: walk every route by
hand, **then grep `log.txt` for `scene build failed`**.

That is a human doing a loop's job, and it is how `b97dd523` would have been
caught before a human found it.

### 6. Real latency, real ordering

`tests/_shell_harness.py` uses a synchronous pool: pages arrive instantly and
in order. Against a server neither is true.

- The guide-prefs race — saving repaints, repainting refetches, and the fetch
  is submitted *before* the save (`4bc952c1`). Only a real scheduler loses that
  race.
- `a9883418` — `load()` published a primary-only batch mid-refresh, taking the
  Latest rows away and putting them back.
- #617's own checklist flags "fast repeated drags" as **the one to watch**, and
  the "least confident" note at the end of the round is thumbnail pressure
  under free scrollbar dragging. The `Bulk *` libraries are ~1000 items each
  and exist for precisely this.

## Structure

`tests/e2e/`, with **no `__init__.py`** — the same trick `tests/integration/`
uses so `python3 -m unittest discover tests` never recurses into it. Own
runner, `tests/e2e/run_e2e.py`, reusing `tests/integration/_harness.py`'s
capability probes.

Server discovery by environment, skipping cleanly when absent, the same
discipline as the existing capability gating:

```sh
JMS_E2E_SERVER=http://127.0.0.1:8096 python3 tests/e2e/run_e2e.py
```

Accounts are stdjflib's fixed twelve, password `stdjflib`. **Do not import
stdjflib from the shim's tests** — that is a second cross-repo dependency on
top of the apiclient one, and the suite needs nothing from it but a URL. Talk
to the server through the apiclient and raw HTTP; standing the server up stays
an out-of-band step.

### Three tiers

**E1 — contract.** Drives `LibrarySource`, `gateway/` and `clients.py` straight
at the server. No mpv, no window, no browser. Classes 1, 2 and 4 live here, it
runs in seconds, and it is roughly two thirds of the value for a fifth of the
effort. Start here.

**E2 — playback.** Real mpv under xvfb, real server, no browser. Class 3. The
existing `test_realmpv_smoke` is the template; the change is a real
`LibrarySource`/session instead of the recording fake.

**E3 — the app.** Real browser attached to a real mpv against a real server —
`tests/integration/test_mpvtk_browser.py` is the template, with its `FakeSource`
swapped for the live one. Classes 5 and 6.

## Status

Green on both backends, 8/8 legs, about two minutes for the matrix. See
`tests/e2e/README.md` to run it.

| Module | Tier | Starter items | Covers |
| --- | --- | --- | --- |
| `test_account_policy` | contract | 17 | restricted libraries, Live TV access, the awkward logins |
| `test_source_conformance` | contract | 12 | the fake source still describes the real one |
| `test_live_tv` | contract | 13 | channels, guide windows, categories, guide prefs, timers |
| `test_route_walk` | contract | 18 | every screen loads and renders against the real library |
| `test_paging` | contract | 19 | virtual scrolling over ~1000 items at real totals (#617) |
| `test_keyboard_nav` | contract | 20 | keyboard reach/activation of real screens; duplicate node ids |
| `test_large_queue` | contract | — | 400-id queue metadata; the 414 request-line limit |
| `test_playback_advance` | playback | 1, 5 | queue advance + watched-marking + resume position |
| `test_playback_eof` | playback | 2, 3, 4 | last-in-queue, seek-to-end (#541), replay (#157/#323) |
| `test_playback_failure` | playback | 7, 8 | truncated, zero-byte, single-frame |
| `test_mpv_reopen` | playback | 6 | close mid-playback → re-open → auto-advance (#458) |
| `test_input_routing` | playback | — | real keys across every UI transition (f70ad1e7, #614) |
| `test_scroll_recovery` | playback | — | wheel-scrolling 1000 items in a real window; blank-tile recovery |

The contract tier never imports `player.py`, so it runs **once** and without a
display — the whole of it is under two seconds. Only the playback tier pays
for the backend matrix.

## Gaps and server behaviours the account tests turned up

None of these is a crash, and none is asserted as a failure; they are recorded
because each one is a thing somebody will otherwise rediscover.

**The shim reads no user policy fields.** Confirmed by grep: nothing in
`jellyfin_mpv_shim/` consults `EnableContentDownloading`,
`EnableMediaPlayback` or `SyncPlayAccess`. The only policy-derived behaviour
is Live TV, and that is inferred from whether the server put a Live TV view in
`/Views` rather than read from the policy — which does gate browsing
correctly, and `LiveTvAccessTest` pins that.

The two that matter are written up as work items in
`docs/PERMISSION_GAPS.md`: SyncPlay has three unconditional entry points and
never reads `SyncPlayAccess`, and the Settings → Home Screen editor offers
Live TV sections to users who cannot have them, producing a slot that renders
nothing forever.

**`EnableMediaPlayback: False` does not stop playback, and cannot.**
`qa-noplayback` plays a file start to finish: PlaybackInfo returns no error
and the server serves the `static=true` URL regardless. Jellyfin's video
endpoints are `AllowAnonymous` — as far as the API is concerned the item id
*is* the credential — so the server cannot structurally refuse and no
client-side check would make it able to. That account therefore cannot find
the spinner it was built to find, because there is no refusal. Worth knowing
before writing a test that asserts one.

**`qa-onesession` refuses the newcomer rather than evicting the incumbent.**
The account's description says a second login must evict the first; measured,
the server answers the second login 403 and leaves the first working.

**Sessions and devices leak unless you log out.** `client.stop()` closes the
socket and leaves the session registered, and the server keeps a Device record
per device id forever. Random per-session device ids left 119 of them behind
before this was noticed, and the accumulated sessions then exhausted
`qa-onesession`'s cap so its test failed on the *first* login — which looks
exactly like the cap working. `Session` now uses one deterministic device id
per account, `stop()` POSTs `/Sessions/Logout`, and the cap test purges the
account's devices as admin in `setUp`.

## Bugs found

Two real defects, both from `test_mpv_reopen`, both invisible to the ~2900
unit tests and the integration matrix because both need a real player and a
real server at once.

**1. Segfault closing mpv mid-playback — libmpv only. FIXED.**

Closing the window makes mpv end the file *and* shut down. The end-file event
queues `finished_callback`, which issues `self._player.command("stop")` on the
action thread, while `_on_shutdown_event`'s terminate thread is concurrently
inside `player.terminate()`. The existing guard is `except _mpv_errors`, and
that is enough on the external backend — there the command is a socket write
and the race surfaces as `BrokenPipeError`. On in-process libmpv the handle
has already been freed, so it is a use-after-free: SIGSEGV, which no `except`
clause can see. Reproduced 2 of 3 runs, stack confirmed by `faulthandler`
(one thread in `mpv.py:terminate`, one in `mpv.py:command`).

Fixed by checking `_mpv_alive` before the command — the idiom ~15 other sites
in `player.py` already use, and `_terminate_mpv` clears that flag *before*
calling `terminate()`, so the ordering favours it. 3/3 clean afterwards, and
the whole unit suite and integration matrix still pass.

**2. A short item aborted early is reported at full duration. PINNED.**

`_finished_at_eof` decides whether the end that just happened was genuine:

```python
return position >= duration * 0.95 or duration - position <= 10
```

The second clause is an absolute ten-second margin, there to absorb the
timeline tick interval and metadata duration drift. It has no lower bound
relative to the runtime, so for anything shorter than ten seconds it is true
at **every** position including zero, and under about twenty it is true across
most of the file. The abort is then reported at full duration and the server
records the item as watched.

Measured: a 10.0s episode aborted at 2.58s reported `session_stop` at 10.02s
and came back `Played=True`; the same on the 3h item reported 0.33s and came
back `Played=False`. Both backends. This is the residual caveat recorded
against #458 in `ISSUES_TO_VERIFY.md` ("a residual X-button freeze that also
*marks the item watched* via `get_timeline_options`") — which that document
flags as never verified. It reproduces, but only for short media; a
normal-length episode closed partway is reported correctly.

Pinned as `@expectedFailure` in `AbortReportedPositionTest`, so it becomes a
hard failure the moment the margin is bounded. Bounding it is a judgement call
about the right threshold and was left for separate triage. Note the arithmetic
itself would be cheaper to pin as a unit test on `_finished_at_eof`; the e2e
test is there for the server-visible consequence.

## Server and harness behaviours confirmed

Each of these produced a failure that read as a shim bug and was not:

- **`NameStartsWith` matches SortName, not Name.** SortName strips the leading
  article, so `NameStartsWith="The Standard Show"` returns nothing while
  `"Standard"` returns it. The harness looks up client-side because of it.
- **The server discards a resume position below `MinResumeDurationSeconds`**
  (300 by default), on top of the 5%/90% clamp. A 10-second episode can never
  hold one, so resume tests need `x-long`.
- **`playerManager` is process-wide**, so terminating it per test class breaks
  the *next* class — and only on the external backend, where mpv is a separate
  process rather than an in-process handle that quietly re-creates itself.
  The backend matrix caught this on its first run.
- **Closing mpv is a race, so do not build an assertion on it.** Whether
  `finished_callback` or the shutdown teardown wins decides which report path
  runs, so a test hung off a window close passes about a third of the time.
  Tests that need the abort path drive `send_timeline_stopped(finished=True)`
  instead; the close itself is still exercised, but only for outcomes that do
  not depend on who won.
- **The playback legs must not use the real audio device.** They decode real
  media, so mpv opens a real output: audible, contending with the desktop, and
  able to fail on a device something else holds. The runner owns one null sink
  for the matrix and addresses it explicitly, never touching the default sink.
- **A truncated file behaves correctly** — reported at 0.29s, not marked
  watched — and a zero-byte file is refused with no session at all. Neither is
  a route to the margin defect above; they were checked as candidates.

## Starter set, priority ordered

Each line names the bug it would have caught. Nothing here needs a fixture that
does not already exist in the library.

### E2 — playback

1. **The Standard Show S01E01 plays out, auto-advances to E02, both report
   progress, E01 comes back watched.** The whole point. (§4 of the 07-25
   checklist, #157/#323)
2. The **last** episode of a queue played to the very end is marked watched —
   it ends via `playback-abort`, not `eof-reached`. With `force_set_played` on
   and off. (§4)
3. Seek to the last second → EOF fires → advances. (#541)
4. Replay an already-finished episode → starts at 0, is not re-marked and is
   not skipped. (#157/#323, reported external-backend-only — run both)
5. Stop mid-episode → the server's resume position is right → reopening
   resumes there. (§4 ReportingMixin)
6. **Close the mpv window mid-playback, cast again → it re-opens, plays, and
   the next episode auto-advances.** The historic stale-queue bug. (§2 —
   "the highest-value single item on this page", #458)
7. `Test Media/Structure/x-truncated` fails cleanly with an error, does not
   hang, and is not marked watched.
8. `x-zero-byte` is refused cleanly; `x-single-frame` does not divide by zero
   in the progress arithmetic.
9. `x-chapters` (12 named chapters): chapter nav from just before a boundary,
   exactly on one, and from the last chapter (no-op). (`9f7a394f`)
10. `x-many-audio` (six tracks, `deu` flagged default): track selection and
    language preference. (AudioMixin, §2)
11. `x-long` (three hours, 36 chapters): seek far from the start, resume point,
    progress over a session that outlives a token.

Every one of these runs per backend — external mpv is the least-tested path and
one of the two largest open-bug clusters.

### E1 — contract

12. **Fake conformance**: every `LibrarySource` method, real answer vs
    `_shell_harness`'s fake, failing when the fake invents a field. (Class 2)
13. Live TV: two categories at once return a **non-empty** guide; window bounds
    are asymmetric and UTC; timer create/cancel; `single_timer_state` vs
    `timer_state` on a showing covered by a series rule. (`4bc952c1`)
14. DisplayPreferences round-trip under client `emby`; guide prefs written as
    strings; a home-layout save preserves section types the shim cannot draw.
15. `LatestItemsExcludes` — applied client-side for per-library Latest rows,
    server-side for Continue Watching and Next Up.
16. `SyncPlay/Ping` is accepted. A 400 here is invisible in normal use.
    (`600022e8`)
17. Per-account behaviour, one test each: `qa-restricted` sees two libraries and
    the rest are **absent**; `qa-nodownload` has downloads refused rather than
    offered; `qa-noplayback` browses but cannot play; `qa-kid` gets empty rows
    and no Live TV; `qa-nopassword` can log in at all.

### E3 — the app

18. **Route walk**: visit every route once against the real library, then assert
    zero `scene build failed` in the log. Replaces a manual step and would have
    caught `b97dd523`. (Class 5)
19. `Bulk Movies` (~1000 items): drag to the middle and get *that*
    neighbourhood; fast repeated drags do not storm the thumbnail workers.
    (#617, Class 6)
20. Continue Watching updates after a second session changes playstate. (#560)

## Hygiene, decided up front

**Never bake an item GUID into a test.** IDs are server-assigned and change on
every reprovision. Look items up by name or path. The library is otherwise
fully deterministic by design — `<lockdata>true</lockdata>` everywhere, dates
from `config.EPOCH`, pseudo-randomness derived by SHA-256 rather than `hash()`.

**Mutating tests need an owner.** Playstate, favourites, DisplayPreferences and
timers all persist. Either give each mutating area its own account, or reset in
`tearDown` through the API. The twelve accounts make the first option cheap.

**Live TV is relative to now.** faketvsource generates its guide against the
clock, so assert relatively — "something is on air", "now/next disagree" — and
never against an absolute time.

**Known fixture gap: there is no `.strm` in the library**, so `8589cc4c`'s
freeze-on-last-frame cannot be reproduced. Worth adding upstream to stdjflib;
it is a `Recipe`-adjacent one-liner and the bug class (Item DTO vs MediaSource
disagreeing about runtime) is not otherwise reachable.

**The server takes minutes to provision.** E1 and E3 want the `Bulk *`
libraries scanned; E2 does not. Worth knowing which legs can run against a
smaller build.

[stdjflib]: ~/Desktop/stdjflib
[faketvsource]: ~/Desktop/faketvsource

## Attempted and blocked: the mpv console's keyboard handover

Candidate 2 of the transcript survey — `45346365`: "the renderer's ENTER and
arrow bindings are FORCED and so outrank its input: typing a command and
pressing ENTER summoned the playback HUD and toggled pause instead of running
the command, with no way back but ESC."

The renderer's contract is the property `user-data/mpv/console/open`, which
mpv's console script sets and `renderer.lua` observes to hand the nav, summon
and skip groups over and take them back. A test would open the console, assert
arrows no longer move focus, close it, and assert exactly the groups that were
taken come back — including the case where they must *not*, during plain
playback where the arrows are mpv's seek keys.

**Both ways in are blocked as the harness stands:**

- The property cannot be injected from Python. `handle.command("set",
  "user-data/mpv/console/open", "yes")` returns without error and changes
  nothing, and both a typed read and `set_property` answer "mpv property does
  not exist" — `user-data` subtrees are node-typed, and the renderer reaches
  them from Lua (`mp.set_property_native`), which python-mpv's typed path does
  not reproduce. Measured: arrows kept moving focus with the console
  ostensibly open, i.e. the observer never fired.
- The real console is not loaded. `mpvtk.app._SPAWN_OPTS` sets
  `load_scripts: "no"`, so pressing `` ` `` in the test handle does nothing.

The faithful fix is the second one: spawn this test's handle with scripts
enabled and drive mpv's actual console, which tests the real precedence rather
than a simulated signal. That is a change to how `_spawn_handle` builds the
handle, so it wants doing deliberately rather than as a flag on one test.
