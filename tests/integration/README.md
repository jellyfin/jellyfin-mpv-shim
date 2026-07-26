# Integration / concurrency test harness

This suite targets the maintainer's single biggest historical pain: **race
conditions and state-management bugs** in the lock-heavy singletons
(`clients.py`, `player.py`, `syncplay.py`, `sync/manager.py`,
`single_instance.py`). The existing `tests/` suite is pure-logic and fast; this
directory holds the heavier, concurrency- and mpv-oriented tests that would be
too slow or too environment-dependent to live in the fast path.

## Design principles

1. **Force interleavings, never sleep-and-hope.** Every race test uses a
   `threading.Barrier` / `Event`, a controllable fake clock (captured
   `set_timeout`), a blocking fake `authenticate`, or a hand-pumped task queue
   to pin the *exact* ordering the bug needs. There are no "sleep 0.1 and hope
   the other thread got there" tests.
2. **Injectable seams over real I/O.** mpv, the network, and the server are
   replaced with fakes so a test can drive a specific state transition
   deterministically. The one exception is the Tier 2 real-mpv smoke, whose
   whole point is a real decoding player.
3. **Do not slow or break the fast suite.** `tests/integration/` has **no
   `__init__.py`**, so `python3 -m unittest discover tests` does not recurse
   into it — the fast suite stays at its 68 tests / ~0.6 s and never launches
   mpv. These tests run only via their own runner (below).

## How to run

```bash
# Full backend matrix (libmpv + jsonipc; real-mpv legs auto-wrapped in xvfb
# when headless):
python3 tests/integration/run_integration.py

# One backend, Tier 1 only (no real mpv):
python3 tests/integration/run_integration.py --backend jsonipc --no-real

# A single module directly (defaults to the libmpv backend):
python3 -m unittest tests.integration.test_player_state_machine
JMS_TEST_BACKEND=jsonipc python3 -m unittest tests.integration.test_player_state_machine

# Real-mpv smoke on a headless box:
JMS_TEST_BACKEND=libmpv xvfb-run -a python3 -m unittest tests.integration.test_realmpv_smoke

# Tk browser UI on a headless box (self-skips if no display and no xvfb):
xvfb-run -a python3 -m unittest tests.integration.test_mpvtk_browser
```

The runner prints a per-leg / per-backend PASS/FAIL summary so an
external-mpv-only regression is unmissable. Legs that lack their capability
(no mpv, no ffmpeg, no display) **self-skip**, which the runner counts as pass.

### Capability gating

`_harness.py` probes `mpv` (libmpv), `python_mpv_jsonipc`, the `mpv` binary,
`ffmpeg`, a display, and `xvfb`. Tier 2 tests are decorated with
`@require_real_mpv` and skip cleanly when the box can't run a real player. Tier 1
needs none of that — it runs anywhere Python does.

## The fakes

### FakeMPV (`_harness.py`)

A scriptable stand-in for an mpv backend object. It supports the two surfaces
`PlayerManager` uses:

* **registration** — the `on_key_press` / `property_observer` /
  `event_callback` decorators used in `_init_mpv`, plus the jsonipc-style
  `bind_property_observer`. Registered callbacks are stored so a test can
  **fire them later, on any thread** (`fire_property`, `fire_event`) to
  reproduce observer-ordering races.
* **control / properties** — `command`, `play`, `show_text`, … are recorded;
  the scalar properties (`pause`, `playback_abort`, `playback_time`, `duration`,
  …) are plain attributes a test sets to script player state.

`import_player_with_fake_mpv()` installs FakeMPV as the imported backend module
and imports `jellyfin_mpv_shim.player` against it, so player.py's module-level
`PlayerManager()` singleton constructs **without a real player or window**. It
also pins `XDG_CONFIG_HOME` to a temp dir, primes the arg parser (the app parses
`sys.argv` when resolving the config dir, which the test runner's argv would
otherwise break), and quiets the heavyweight optional features
(trickplay/shader-pack/OSC) so construction is light.

It then **puts the real backend module back into `sys.modules`**. player.py has
already bound the fake, so the state-machine tests are unaffected — but that
entry is process-wide and permanent, and leaving the fake there handed it to
every later importer too. `test_mpvtk_browser` / `test_mpvtk_hud` do `import
mpv as libmpv` to spawn a real handle; with the fake in place they waited 15s
each for a renderer that could never start, which is why the suite looked
flaky as a whole run while every module passed alone. `test_harness_isolation`
pins this contract. Anything that needs a *real*-mpv-bound player module must
check `player.mpv`, not `sys.modules` — see `test_realmpv_smoke`.

`build_player()` then hands back a `PlayerManager` built via `__new__` with just
the state the state-machine methods touch wired up — the tests drive the epoch /
lock / queue logic in isolation rather than re-testing mpv option plumbing.

### Fake server / session

The Tier 2 smoke does **not** stand up a socket `http.server`. The bytes come
from a local ffmpeg clip played by a real mpv (a file path — no network, no
transcode), and the Jellyfin *session* side (`session_playing` / `progress` /
`stop`) is an in-process **recording fake** on `video.client.jellyfin`. This is
deliberate: a real socket server adds port-allocation and timing flakiness
without exercising any more of the shim's own code — the session calls just hand
off to the third-party `jellyfin-apiclient-python`. We assert the shim makes the
right calls with the right payloads, which is the shim's actual contract. (The
sync-manager tests likewise fake `requests` rather than run a server.)

### Concurrency-forcing helpers

* `run_concurrently(target, n)` — starts N threads, joins them, and
  **re-raises** any worker exception in the caller (a silent thread death would
  otherwise hide a corrupted-state failure); flags a deadlock if a thread
  doesn't finish.
* `spin_barrier(n)` — a `threading.Barrier` racing threads line up on so the
  critical section is entered simultaneously.
* `CapturingTimeout` (syncplay tests) — replaces `set_timeout` so a scheduled
  callback is captured, not threaded, and fired by hand to model a timer that
  expires at an arbitrarily late moment.

## Backend matrix (libmpv vs external/jsonipc)

The mpv backend is chosen **at import time** in `player.py`
(`if not settings.mpv_ext: import mpv` else `import python_mpv_jsonipc`), which
also sets `is_using_ext_mpv` and the `_mpv_errors` tuple. Because that selection
and the interdependent module-level singletons (`playerManager`, `actionThread`,
`timelineManager` importing each other) are baked in at import, **we flip the
switch with a subprocess per backend, not `importlib.reload`.** The runner sets
`JMS_TEST_BACKEND` and re-invokes `unittest` in a fresh interpreter for each leg;
`_harness.BACKEND` reads it and, for fake-mpv legs, sets `settings.mpv_ext` and
installs the matching fake module before player.py is imported. The fake-mpv and
real-mpv legs run in *separate* processes even within a backend, since one
imports player against a fake and the other against the real backend.

What is asserted **identical** across both backends:

| Divergent spot | libmpv | jsonipc | Test |
| --- | --- | --- | --- |
| `_mpv_errors` tuple | `(BrokenPipeError, ShutdownError)` | `(BrokenPipeError, TimeoutError)` | `BackendMatrixTest.test_mpv_errors_tuple_matches_active_backend` |
| disconnect guard catches the *divergent* member | catches `ShutdownError` | catches `TimeoutError` | `BackendMatrixTest.test_backend_specific_disconnect_error_in_task_is_handled` |
| `wait_property` observer dispatch | `observe_property` | `bind_property_observer` + `skip_initial` | already covered by the fast suite's `tests/test_wait_property.py` (both surfaces) |
| play → progress → EOF → auto-advance → stop | in-process decode | real `mpv` binary over JSON IPC | `test_realmpv_smoke` (run per backend) |

A guard that caught `ShutdownError` but not `TimeoutError` (an audit-era,
external-mpv-only class of bug) fails the jsonipc leg while the libmpv leg
passes — exactly the visibility the maintainer asked for.

## What each test would catch (audit-era race map)

* **clients** — duplicate/lost client under concurrent connect; leaked
  `_connecting` reservation; a connect resurrecting a client `stop()` already
  drained; the `validate_client`-vs-reconnect **identity race** tearing down a
  healthy replacement; health-check ticks building duplicates.
* **player** — the **cast-at-EOF epoch race** (a stale finished-callback marking
  the just-cast item played and skipping it); abort-vs-EOF (an errored stream
  wrongly marked watched); `_video` nulled mid-callback; both finish observers
  racing the `_finished_lock`; the queued shutdown teardown running even after
  mpv died; `update()` surviving a failing task (action-thread survival).
* **syncplay** — a scheduled play/pause/seek/speed callback firing after
  disable / leave-then-rejoin / supersede, guarded by `sync_generation`.
* **sync/manager** — delete-at-commit (S4) honoured via a real deleter thread
  racing the worker at the commit barrier; short-read → PENDING with
  stall-escalation → ERROR; transient `RequestException` → PENDING + resume (not
  ERROR) vs 4xx → ERROR; `stop()` joining a mid-download worker and closing the
  DB with the `.part` preserved.
* **single_instance** — real multi-process election: exactly one primary under a
  race, a wedged listener still blocking a duplicate, different config dirs both
  winning, lock release allowing a new primary.

## Coverage map — what each file covers

| File | Leg | Covers | Known gaps |
| --- | --- | --- | --- |
| `test_clients_concurrency` | agnostic | connect/disconnect registry races (see above); **#295/#344** failed-health-check reconnect *without a restart* (dead-code fix `077a42d`) | — |
| `test_sync_manager_races` | agnostic | download worker / delete / stop races | — |
| `test_syncplay_generation` | agnostic | `sync_generation`-guarded callbacks | — |
| `test_single_instance_multiproc` | agnostic | multi-process primary election; **#505** no orphaned child/forkserver survives acquire→release→exit (process-group scan) | — |
| `test_player_state_machine` | per-backend fake | EOF/abort/shutdown epoch + `_finished_lock` races, backend `_mpv_errors` matrix; **#458** close-crash mid-`update()`/`send_timeline()` survives + torn-down item not reported at full duration/watched; **#503** external broken-pipe in `send_timeline` (jsonipc only); **#157/#323** resume-at-EOF not watched/advanced (both backends, incl. `resume_playback=False`) | — |
| `test_mpv_lifecycle` | per-backend fake | commit `012961c` lifecycle: leak-free re-open (`_teardown_player` stops old trickplay `join=False`, `TrickPlay` daemon); **`StaleQueueDrainTest`** — `_teardown_player`/re-open drains the outgoing mpv's stale `_handle_mpv_shutdown`/`finished_callback` tasks so the new `_video` survives and eof still queues (the real re-open-wedge fix); `idle_quit()` gating (video/menu/syncplay/webview no-op; fires on in-process libmpv **and** managed external; no-ops only for user-launched ext) with backend globals patched to drive both branches on both fake legs; intentional-quit `handle_shutdown` guard stays silent (vs. teardown control); `_ensure_mpv` re-open clears `_idle_quit` | real clip re-play is covered by `test_realmpv_smoke` |
| `test_keyboard_controls` | per-backend fake | key-binding **routing** on the real singleton: stop/next/prev/watched/unwatched queue the right task; media keys honour `media_key_seek` (seek vs. next, intro-skip); pause toggles vs. confirms menu; menu open/close and the loading guard; nav keys route to `menu.menu_action` when shown else seek (right/up skip intro); `ok` always → `menu_action("ok")`; `esc` back vs. leave-fullscreen; fullscreen toggle; a full-sweep mis-wiring/crash guard | `kb_debug` (~) never pressed — its handler calls `pdb.set_trace()` and would hang; `kb_kill_shader`'s `settings.save()` is mocked (no config path under the fake harness). Handlers are asserted at the routing layer (queued task / stubbed collaborator), not by running full playback. |
| `test_lifecycle` | per-backend fake | `ActionThread` / `TimelineManager` tick → survive a collaborator exception → `stop()` joins promptly + dead; action-thread final drain; `PlayerManager.terminate()` → stop + trickplay stop + (jsonipc only) player terminate; `ClientManager.stop()` prompt with an in-flight reconnect sleep + idempotent; `gui_mgr.on_browser_died` detaches log/sync callbacks and nulls the browser cmd queue | `on_browser_died` is driven with the child **process mocked** (a real fork under the test runner is flaky) — the detach/leak path is what's pinned, not a live child crash. |
| `test_realmpv_smoke` | per-backend real (xvfb) | real decode → progress → EOF auto-advance → stop; **#541** seek-to-end fires EOF/auto-advance; **012961c** idle-quit lifecycle: fires + re-opens then **auto-advances** on both libmpv (in-process re-create) and jsonipc (fresh process); `IdleQuitReopenIsolatedTest` runs the full idle-quit→re-open→auto-advance in a subprocess (ADVANCEs on both backends) | benign `ResourceWarning` on jsonipc teardown |

## Roadmap — implemented vs. remaining

**Implemented (all green; libmpv + jsonipc):**

* Tier 1: `test_clients_concurrency` (8), `test_player_state_machine` (19,
  incl. backend matrix + issue regressions #458/#503/#157/#323),
  `test_syncplay_generation` (6), `test_sync_manager_races` (7),
  `test_single_instance_multiproc` (6), `test_keyboard_controls` (17),
  `test_lifecycle` (9), `test_mpv_lifecycle` (17) — commit `012961c` mpv
  process-lifecycle (incl. the stale-queue re-open drain lock).
* Tier 2: `test_realmpv_smoke` (6) — real play → timeline post → EOF
  auto-advance → stop, seek-to-end EOF (#541), and the `012961c` idle-quit
  lifecycle (fires + re-opens + auto-advances on both backends, plus a
  subprocess end-to-end auto-advance check), per backend under xvfb.
* Tier 3: `test_mpvtk_browser` / `test_mpvtk_auth` — the real browser attached to a real mpv under
  xvfb against a fake `LibrarySource`.
* The runner + backend matrix orchestration.

**Remaining / future work:**

* **A real-HTTP fake server leg** if end-to-end coverage of the apiclient wire
  format is ever wanted (deliberately skipped for determinism today).
* **jsonipc real-mpv teardown** emits a benign `ResourceWarning` (the spawned
  mpv is reaped at interpreter exit); a stricter teardown could silence it.

## Bugs found while building this harness

Writing the harness surfaced two real defects (reported to the maintainer, not
fixed here) plus one that led to a product fix:

1. **`SyncPlayManager._rearm_sync` is undefined.** It is referenced 3× in
   `syncplay.py` (introduced by commit `fccd69a`) but defined nowhere, so every
   SyncPlay Unpause / skip-to-sync re-arm raises `AttributeError` — including the
   common "join a group that's already playing" path. Pinned by
   `test_syncplay_generation.test_playing_now_rearm_sync_is_defined` (an
   `@expectedFailure` that flips to a hard failure the moment the method is
   added).
2. **`conffile.get` has a check-then-`makedirs` TOCTOU race.** Two fresh
   processes starting at once both see the config dir missing and both call
   `os.makedirs`, so one gets `FileExistsError`. Surfaced by the multi-process
   single-instance test (worked around there by pre-creating the dir).
3. **Re-open wedge: stale queued tasks from the outgoing mpv null the new
   `_video` (commit `012961c`, FIXED).** When mpv is re-opened (idle-quit, and
   the pre-existing crash-recovery / user-close paths), the *outgoing* instance
   is terminated while its dying shutdown/eof observers have already
   `put_task`ed `_handle_mpv_shutdown` (and stray `finished_callback`s) onto
   `evt_queue`. Those tasks were never drained before the new player started, so
   the action pump ran them against the **new** session — `_handle_mpv_shutdown`
   nulls `self._video`, after which the re-opened player's `eof-reached` sees
   `_video is None` and does nothing, so auto-advance silently stops. It *looked*
   libmpv-specific (libmpv STALLED, jsonipc ADVANCED in `_idle_reopen_child.py`)
   purely because of shutdown-event timing; libmpv re-creates in-process fine.
   **Fix landed:** `_teardown_player` now, after joining the terminate thread
   (old event thread dead → nothing re-queues), **drains `evt_queue`** before the
   re-open builds a new player. idle-quit is back to firing on both libmpv and a
   managed external mpv (only a *user-launched* external mpv — `mpv_ext_start`
   False — is excluded). As a bonus this also fixes crash-recovery / user-close
   re-open (#458) on both backends, since it is the same stale-queue mechanism.
   Regression-locked (backend-agnostic) by
   `test_mpv_lifecycle.StaleQueueDrainTest`, and validated end-to-end by
   `test_realmpv_smoke.IdleQuitReopenIsolatedTest` (child ADVANCEs on both
   backends) plus the per-backend fires/re-open/auto-advance tests in
   `RealMpvSmokeTest` and the gating tests in `IdleQuitGatingTest`.
