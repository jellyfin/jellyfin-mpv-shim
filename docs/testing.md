# Testing

Run the suite: `xvfb-run -a python3 -m unittest discover tests`
(stdlib unittest, no extra deps). The integration matrix:
`xvfb-run -a python3 tests/integration/run_integration.py`.

`CLAUDE.md` carries the two rules you need before running anything (select with
`-k`, never a module name; always use `xvfb-run`) and the three standing
disciplines. This file is the long version: what each specialised suite exists to
catch, and the case histories behind the rules.

## 1. Why `xvfb-run`, and why for the unit suite too

`player.py` creates its `playerManager` singleton at module scope and
`PlayerManager.__init__` ends with `_init_mpv()`, so *importing* the module opens a
real mpv window. Eight unit modules import it, including pure-AST ones like
`test_no_tkinter` (which imports every module to prove none pulls tkinter). Without
a nested X server they land on your desktop and steal clicks.

## 2. Why `-k`, and never a module name or `-p`

Importing almost anything under `jellyfin_mpv_shim` reaches `conffile.confdir` →
`args.get_args()`, which calls `parse_args()` on the real `sys.argv` **at import
time**.

`discover tests` is safe because unittest replaces `sys.argv` with `['test']` before
it imports anything, and it consumes `-k` and `-v` itself. But a **module name**
(`python3 -m unittest tests.test_foo`) stays in argv as a positional, and a `-p
PATTERN` is left over — so both die with the app's own usage line and
`invalid choice: 'tests.test_foo'` / `unrecognized arguments: -p`. That reads as a
broken test module and is nothing of the kind.

The e2e suite is exempt: `tests/e2e/` has no `__init__.py`, is never discovered, and
its modules *are* named directly (`python3 -m unittest tests.e2e.test_route_walk`),
because nothing in it imports the config layer at module scope.

## 3. SyncPlay is tested against a modelled server, not by hand

`tests/_syncplay_server.py` is the group state machine ported from
`MediaBrowser.Controller/SyncPlay/GroupStates/`. `tests/_syncplay_network.py` seats
*several real `SyncPlayManager`s* on one group with a message bus between them, and
`tests/test_syncplay_e2e.py` asserts the property the feature exists for: after
anything anyone does, every member is at the same position, in the same state, and
nobody is holding the group in `Waiting`.

**Convergence is checked against the *group*, not only between members.** Two clients
that agree with each other and are both a minute behind the server have failed
identically, and a members-only check is exactly the one that passes.

The bus bounds the message count, so a livelock fails a test instead of hanging the
suite.

When changing anything in `syncplay.py`, **verify the tests can still fail**:
reintroduce a bug (drop `report_ready=True`, ignore a command type, skip
`_set_ignore_wait`) and check the right tests go red.

### The same property, again, against a live server

`tests/e2e/test_syncplay_group.py` (+ `tests/e2e/_syncplay_live.py`): two real
sessions, two real websockets, one real group, no mpv. Run it with
`JMS_E2E_SERVER=http://127.0.0.1:8096 python3 -m unittest tests.e2e.test_syncplay_group`
(~37 s).

The modelled suite is the one to reach for first — it is 50 ms and can force states a
live server reaches only by luck — but it is a **port**, and a port is a belief about
someone else's code that has already been wrong twice here. **When the two disagree,
the model is what is wrong.**

Live tests earn their keep only if they are stable. This one was flaky three separate
times, **every time because a wait was satisfied by stale evidence** — the previous
test's video, or the one member already known to have arrived. Wait for *every*
member, against a mark taken *before* the action.

### The player↔SyncPlay wiring is a separate liability from the protocol

`tests/test_syncplay_player_contract.py` extracts every `self.playerManager.X` from
`syncplay.py` (and every `self.syncplay.X` from the player) and asserts the real
objects — *and all four stand-in players* — provide them. That second half matters: a
fake that implements a subset does not leave a path untested, it makes the path raise
where nothing is looking, and all four were missing
`has_video`/`send_timeline`/`timeline_handle`/`upd_player_hide` when this was written.

`tests/e2e/test_syncplay_playback.py` is the only SyncPlay suite with a **real
PlayerManager, real mpv and a real stream** in a real group (the other member is a
stand-in — the friend is not what is under test). It pins: stop halts rather than
leaves, stop *does* leave when `syncplay_menu_reachable` says no, a halted player is
not driven, and resume replays the group's content.

When asserting "the group did not drive us", watch the **player boundary** (spy on
`seek`/`set_paused`/`set_speed`/`play`), not its effects — with nothing loaded a stray
seek changes almost nothing, and an effects-based assertion passes while every command
is being applied.

## 4. A stand-in that omits a field is how a property goes untested

This is the most common failure mode in this repo — four in one sweep. It does not
leave a path uncovered; it makes the path **unreachable while reporting a pass**,
because the thing the test is named after has nowhere to live.

The review question for a new fake is: *which field of the real object did I not
model, and is that the field the test is named after?*

The case histories, because the abstract rule is easy to nod at and hard to apply:

- **`FakeQueue` had no `has_next`**, so nothing could see `Media.replace_queue`
  freezing it.
- **`FakeThumbs.get_cached` was `return None`**, so nothing could see every decoded
  image being kept forever.
- **`FakeManager.enqueue` wrote no row**, so every auto-download pass saw a virgin
  catalog and a multi-pass property was unobservable.
- **`_SyncPool` runs work at submit time**, so no browser suite had ever had two jobs
  in flight. Use `_DeferredPool.release(index)` for an interleaving.
- **`FakeSource.backdrop_spec` answered `None` unconditionally**, so *no shell test
  had ever rendered a header that has artwork* — and a header with a backdrop lays out
  differently from one without, because the heading is baked into the bitmap.
- **`FakeMPV` had no `unbind_property_observer`** — the call `wait_property` makes on
  its way out — so **no fake-backed load had ever completed**. Nor did it have
  `eof_reached`, `core_idle`, `chapter_list` or `window_maximized`, every one of which
  production reads inside a broad `except Exception` that turned the gap into a
  silently-taken "mpv would not answer" branch. It also carried *both* backends'
  observer APIs at once, which is the thing `mpv_events` dispatches on, so the matrix
  leg named "libmpv" was exercising jsonipc's.

`tests/integration/test_playback_start.py` is what fixing `FakeMPV` unlocked: the
three ways a start fails, which a real mpv cannot be asked to perform on cue.

The cheap half of this is checkable from the source, so it is:
`tools/audit_fake_contracts.py` diffs what production code reaches on a collaborator
against what each stand-in provides, and `tests/test_no_fake_gaps.py` runs it. Same
standing as the stale-capture audit — a lead generator, with an `accepted` list per
pair for what genuinely needs no modelling. It knows what is *reached for*, never
whether the answer is honest.

## 5. "In which order" — the journal

Each stand-in used to keep its own recorder: a list of commands, a list of played
urls, the last value written to an attribute. **Two recorders on two objects cannot
be compared at all**, which is why every ordering claim in `_play_media`'s comments
(volume before the file so the track never blares at the default, the menu down
before the handover, the geometry armed before the load) was checked by nothing.

`_harness.Journal` is one stream every fake writes into — `pm.journal` in
`build_player` — and `mark()` puts the *test's* own events in it so a claim can name a
moment nothing else does.

**Assertions are subsequences, never equality.** That is the whole design: a log
compared as a whole fails the day somebody adds an event, which makes the journal a
tax paid by deleting assertions. `tests/test_fake_journal.py` spends half its tests on
that tolerance rather than on the ordering.

Two events are deliberately kept apart: `set:` (the shim wrote it) from `prop:` (mpv
reported it). An ordering of one pattern is refused outright, because it is satisfied
by anything that happened at all.

What this catches that an end-state assertion cannot: **a handoff that reaches the
right state by the wrong route** — the browse window re-armed after the yield ends
with mpv configured correctly and the window torn down and rebuilt in front of the
user on the way.

## 6. Assert the property over several steps, not the mechanics of one

The recurring bug shape in this repo is **state feeding back into the input that
produced it**, and one-step tests cannot see it:

- the auto-download lookahead anchored on what it had already downloaded, so it walked
  whole series;
- `reap()` deleted the only record of a failed download one call before the planner
  re-queued it;
- the Guide never re-seeded its window;
- leaving a SyncPlay group left `is_buffering` latched for the next one.

Each had a test that drove the operation *once* and asserted that step was right.

Anything a scheduler, poller, health check or websocket can re-run gets a **loop of
≥3** and an assertion that the observable did not walk.

## 7. Three green-but-worthless shapes that have all shipped here

- **Uncollected** — a test that never runs. (`Un-swallow three tests that had never
  run`.)
- **Tautological** — asserts the code against itself.
- **Self-agreeing** — a fake written to agree with the code under test, so it cannot
  disagree.

## 8. Firing `ready` poisons scene snapshots

Firing `MpvtkApp`'s ready dispatch in a test installs measured font metrics
*globally*, so seven unrelated scene snapshots fail — and only in the full run, never
when the module is selected alone.

## 9. Running the unit suite in parallel

`tools/run_tests_parallel.py` runs the same tests as `discover tests`, one
process per module:

```
xvfb-run -a python3 tools/run_tests_parallel.py
```

**~64s against ~7 minutes**, same 4,905 tests. It is stdlib-only, like the
suite, and it exists because the suite got slow enough that re-running it to
see a failure you did not keep costs more than the failure did.

`xvfb-run` goes in front of *this script*, not around the workers: `-a` picks
a display by probing for a free one, and sixteen of those probing at once race
for the same number. The workers inherit `DISPLAY` and share one server.

### What it has to get right

- **`sys.argv` is cleared before anything under `jellyfin_mpv_shim` is
  imported** (§2). A worker that takes arguments of its own would otherwise
  die on the app's usage line.
- **The repo root goes on `sys.path` explicitly.** A script in `tools/` has
  `tools/` as `sys.path[0]`, so `jellyfin_mpv_shim` resolves to whatever is
  pip-installed — silently, and it *runs*, against the previous release. This
  is the "run from the repo root" rule reached from a direction the rule does
  not cover, and it cost an hour here.
- **Selection is `TestLoader.discover(start_dir, pattern=…)`**, not a module
  name, so each module is imported exactly as `discover tests` imports it.
- **The result comes from a line the worker prints, not from its exit
  status.** A module holding a real libmpv can abort with "pure virtual method
  called" during interpreter teardown when several dozen are torn down at
  once — after passing. The worker reports, then `os._exit`s. A worker that
  dies *before* reporting is still a hard failure.
- **Each worker is its own process group, and a timeout kills the group.** A
  killed run that leaves mpv and Xvfb children behind is how a machine
  accumulates a graveyard of them; one on the dev box outlived its run by five
  days.

### Jobs, and why the default is half the CPUs

Every worker that imports `player.py` creates a real mpv window (§1), and they
share one single-threaded Xvfb. Measured on a 32-CPU box: `-j32` finished in
50s idle and **starved** under ambient load — four modules that normally take
12-17s were unfinished at 90s. `-j16` is 64s and has not wobbled. Raise it on
a quiet machine; the floor is the slowest single module (~43s), because that
is one process and nothing splits it.

`tests/test_parallel_runner.py` pins the one invariant the runner cannot check
about itself: the modules it would run are the modules discover collects. A
module it silently skips reports exactly like a module that passed.

### The integration matrix stays serial

`run_integration.py` runs its legs one at a time deliberately, and its last leg
runs everything in one process specifically to catch cross-module interference.
Parallelizing it would remove the thing it is for — and it drives real mpv
through the keyboard, which is where contention bites hardest.

## 10. Driving real mouse input against a real mpv

Five things that cost a session each, none of which has a line in the tests to
sit on — they are all about the test you are *about* to write.

**A click is `keydown MBTN_LEFT` + `keyup MBTN_LEFT`, not `mouse <x> <y> 0`.**
That last form delivers the button with neither state bit set, which mpv reports
as a *press* — and `defaults.lua` routes a press to the **release** half of a
`set_key_bindings` pair, so it fires `on_mouse_up` with no press before it. That
does nothing at rest (it bails without `state.pressed`), so the test clicks
nothing and passes; mid-drag it is worse than nothing, because it takes the
slider/scrollbar branch and commits a gesture the test never made. Position the
pointer with `mouse <x> <y>` first: the press is resolved against mpv's own idea
of where the pointer is.

**Going through mpv is the point, when the question is which binding wins.**
`app.debug(cmd="click", id=...)` calls the renderer's handlers directly, so it
answers yes however the input sections were left. Only a real button press walks
mpv's section stack — which is what
`test_mpvtk_hud.py:test_the_console_gives_back_the_hud_it_left_with` is for.

**`mouse <x> <y>` repairs `mouse-pos.hover` on the way past**: mpv synthesizes
MOUSE_ENTER for an artificial move that lands inside the window (`command.c`,
`cmd_mouse`). So a stranded hover flag — the #700 state, where mpv believes the
pointer is outside a window it is sitting in the middle of — cannot be reached
through *that* command, and an integration test driving the pointer with it can
only pin the rest of the path. The decision logic is pinned in `tests/lua/`,
against the real observer. (An out-of-bounds `mouse <x> <y>` synthesizes
MOUSE_**LEAVE** by the same rule, which is a second way to leave from outside
the process.)

It **is** reachable with real X input, which is what `tools/probe_hover_strand.py`
does: grab the pointer, move the window under it with `xdotool`, ungrab, and the
restoring EnterNotify arrives as NotifyUngrab and is dropped. That is a manual
probe rather than a test because Xvfb, openbox, xdotool and python-xlib are none
of them test dependencies, and a suite that skips reports exactly like one that
passed (§7). Reach for it before shipping another guess at #700 — two attempts
went to the reporter without anyone here having seen the state, because openbox
and kwin ungrab before they resize and Cinnamon/muffin does not.

**The repair is `keypress MOUSE_ENTER`, not `mouse <x> <y>`.** Both feed the
enter artificially, but the latter also rewrites the pointer position — and
`mouse-pos` reports the *consumer* coordinates, advanced when a queued move is
dequeued, while the unchanged-position early return compares the *producer*
ones. Handing back the position just observed can therefore replace a newer
pending motion, and with built-in dragging live can cross the deadzone into
`begin-vo-dragging`. Its bounds-derived hover repair is also mpv 0.33+, where
`keypress` predates 0.29. Note that `mp.commandv` *reports* a rejected command
(`nil` plus a message) rather than raising it, so a bare `pcall` around it
proves nothing — read the returned value.

**A real leave does not look like the one your test writes.** `keypress
MOUSE_LEAVE`, and the Lua fake's `{same x, same y, hover=false}`, both produce a
leave whose position is unchanged — which is the *rare* shape. mpv clears the
flag when the LeaveNotify is fed but commits a motion's position when the
command is dequeued, drains the whole input queue per iteration, and reports a
property once per drain: so an ordinary flick of the pointer out of the window
arrives as **one** notification carrying the last in-window position *and*
`hover=false` (measured at 27 of 30 crossings; a fast exit reports a position
from the middle of the window). Any renderer logic that keys off "the position
changed" is testing a shape X11 almost never sends. What the renderer does
instead — treat one such event as provisional and let a grace timer decide — is
in the `mouse-pos` observer.

**Writing a user-data flag: python-mpv sends every scalar as a string.**
`handle._set_property("user-data/...", True)` stores the *string* `"yes"`, and a
string node reads back as nil under `MPV_FORMAT_FLAG` — which is the format
`renderer.lua` observes mpv-console's flag in, because that is what the console
writes. A test that sets it the obvious way sets nothing the renderer can see and
then passes whatever the code does (§7). Write it with `MPV_FORMAT_FLAG` through
ctypes on libmpv; jsonipc's JSON `true` arrives as a bool already.
