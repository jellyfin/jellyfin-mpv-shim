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

- **The Lua suite's `strip_page` gave every row a fixed id**, which is a *value* the
  app did not send rather than a field left out — and it is the same failure with a
  different surface. The row bitmaps the browser pushes were keyed by their path in
  the widget tree, so the Stack that floats the hover play chip renamed the row under
  it; the fake pushed the scene itself, so it could only ever measure the ids it
  chose. Its whole "overlay slot order" block was therefore asserting the cheap case
  while every hover re-uploaded a whole strip. Where a fake supplies the *input*
  rather than standing in for a collaborator, ask what the real producer sends, and
  pin it on that side: `tests/test_tile_play_chip.py` is what holds the id still now,
  and `tests/integration/test_mpvtk_browser.py` asserts the cost against a real mpv.

`tests/integration/test_playback_start.py` is what fixing `FakeMPV` unlocked: the
three ways a start fails, which a real mpv cannot be asked to perform on cue.

### The field `FakeMPV` did not model was failure

It refused an unmodelled **read** and accepted an unmodelled **write** — and a
refused write is the whole subject of `mpv_guard` (docs/mpv-backends.md section 1).
So the fake takes an injectable absent-property set, empty by default, through
`property_is_absent("osd-border-style")`: one set and one branch in a
`__setattr__` that already existed, and the only way to exercise the version-skew
path without owning fourteen mpv builds.

Deliberately not the other way round. A fake that refused every name it does not
model would enumerate mpv's property list inside this repository and then assert
the shim agrees with it, which is the *self-agreeing* shape rather than a fact
about mpv — and it would fail on the first legitimate name somebody adds.

The assertion around it is `_harness.watch_refused_writes`, registered by
`build_player` on the case that called it and by `E2ETestCase.setUp` for every e2e
case, so **nothing opts in**. Integration has no shared base to put a cleanup in,
and a base added now would cover only the files somebody remembered to re-parent;
`build_player` is the one thing all 47 sites call, which is why its `test`
parameter is enforced by `tools/audit_build_player_calls.py` (via
`tests/test_no_unwatched_players.py`) rather than remembered.

What it is worth differs by suite, and saying so is the point. Against a real mpv
(e2e, and the three integration files that run the real `_init_mpv`) it is a
version-skew detector, and different boxes and CI images carry different builds, so
the population gets covered over runs rather than per run. Against `FakeMPV` it can
only fire for a property a test declared absent — a hook that cannot otherwise
fail, which is the shape section 7 is about. It is installed for what it stops
somebody doing later: making the fake lenient again with nothing to notice.

`tools/probe_hover_overlay_cost.py` is the number behind that last one: it drives a
real mpv, sweeps the pointer across a grid, and prints the overlay traffic per move
from the renderer's own `ov_adds` / `ov_bytes`. Measured on Windows (d3d11/WARP,
1000x740): 1 overlay-add and 0.01 MiB per move with the rows named, against 2.9 and
1.52 MiB with the name taken away — the same shape on Linux at 2.17 MiB. It is a
probe rather than a test for the usual reason (§7): it wants a window and a number
to read, not a threshold.

The cheap half of this is checkable from the source, so it is:
`tools/audit_fake_contracts.py` diffs what production code reaches on a collaborator
against what each stand-in provides, and `tests/test_no_fake_gaps.py` runs it. Same
standing as the stale-capture audit — a lead generator, with an `accepted` list per
pair for what genuinely needs no modelling. It knows what is *reached for*, never
whether the answer is honest.

**And it cannot see a field that is present and permanently dead.** The audit asks
which field a stand-in *omitted*, so a fake that names every column and populates six
of them satisfies it completely. Measured 2026-09-12: seven test modules built
downloads rows with `content_server_id` unset — an **orphan**, which is a separate
contract that never syncs — and one of them filed twenty-two userdata writes onto the
orphan key while the suite stayed green, because the write and its read were re-keyed
together and pass/fail cannot see that. The worst offender was
`{c: None for c in COLUMNS}`, which omits nothing.

Two instruments came out of it, and the split is the useful part.
`tools/audit_row_fixtures.py` (via `tests/test_no_orphan_fixtures.py`) is the lint: it
reads dict keys, subscripts **and keyword arguments**, the last because a helper ending
in `row.update(kw)` is how the honest modules set the column, and leaving keywords out
reported one of them as broken. The lint is file-level and cannot say which *row* is
unhomed; that needs the other instrument — wrapping the writers to record the physical
key each call resolves to, which is a measurement you run once, not a check you keep.
**When a column's absence changes meaning rather than coverage, the absence check is
the wrong shape.**

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

**Uncollected is checked mechanically** by `tests/test_no_uncollected_tests.py`:
a `test_*` inside the `__main__` guard, anything after `unittest.main()` (which
raises `SystemExit`), or a `test_*` on a class no loader collects. It was written
after `test_player_auth_scope` was found collecting **5 of 7** — four members had
drifted inside the guard, after the `main()` call, and the two tests among them
described a credential rule that had gone unenforced for as long as it existed.

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

**~30s against ~2m15s**, same ~5,500 tests (measured 2026-09-05 on a 16-core
box). It is stdlib-only, like the suite, and it exists because the suite got
slow enough that re-running it to see a failure you did not keep costs more
than the failure did.

Both figures had rotted -- they were written as 64s against 7 minutes, and the
serial suite has since roughly halved while the test count grew by 600. Quote
them as the shape of the wait rather than as a benchmark, and re-measure
before using either as evidence about a change.

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

### The parallel runner cannot see cross-module interference, and that is the trade

One process per module means a module that poisons the interpreter poisons only
itself, so `run_tests_parallel.py` is green on exactly the failures the
whole-suite run exists to find. **`discover tests` is still the command that has
to pass before shipping**, and this is why.

Measured, and it is not hypothetical. `tests/test_interface_language.py` patched
`gettext.translation` and called `i18n.configure()` — which stores what it gets
in the module global every `_()` in the app reads. The patch came off; the stored
MagicMock did not. Every module imported after it alphabetically got a `_()` that
returned a mock, so **around 340 assertions about user-facing strings failed**,
in modules with nothing to do with i18n, on both platforms. Per-module it passed.
Under the parallel runner it passed. It failed only under the one command that
runs everything in one interpreter, which had been red for long enough that the
noise read as the baseline.

Two things follow. A test that mutates process-global state restores it in
`addCleanup`, *including state a `mock.patch` context put there indirectly* —
the patch covers the function, never what the caller did with its return value.
And a whole-suite run that is red is not a baseline: it is a leak, and it hides
the next one.

### The integration matrix stays serial

`run_integration.py` runs its legs one at a time deliberately, and its last leg
runs everything in one process specifically to catch cross-module interference.
Parallelizing it would remove the thing it is for — and it drives real mpv
through the keyboard, which is where contention bites hardest.

## 9b. When the matrix hangs after the last leg passed

**Signature:** the runner sits at `WCHAN=pipe_read` and 0% CPU, its `xvfb-run`
child is `<defunct>`, and some surviving mpv's `/proc/<pid>/fd/1` points at the
very inode the runner is blocked on. A test mpv that outlives its leg is
reparented to PID 1 while still holding the write end of that leg's
stdout/stderr pipe, so the runner's read to EOF can never return — after the
leg has already passed.

**Unwedge it by `kill <mpv pid>`, by PID.** Never `pkill -f`: the pattern
matches the killing shell's own command line (see the global CLAUDE.md), and
`~/.claude/bin/safe-pkill` is the tool if a pattern is unavoidable.

Costs a ~7-minute run whenever it fires and would hang CI forever. The real fix
is to reap the child and then read with a deadline, or to give mpv its own
process group with closed fds.

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

## 11. The e2e fixture account is shared, mutable state

The QA server is not a fresh fixture per run. It is one long-lived account
that every e2e run on every machine authenticates as, so **anything a test
writes there is read by every later run, on both platforms, until something
overwrites it.** A module that reads such state without normalising it first
is measuring the last run that touched it — including a run that died
halfway and never reached its cleanup.

This has cost two sessions. Three e2e tests were recorded as "pre-existing
failures" in code nobody had changed, and were suspected defects in the
browser shell. They were three days of accumulated home-screen layout. On a
freshly rebuilt server the same tree ran 49/49.

**Restoring is not normalising, and the difference is the whole trap.**
`test_home_layout` — the only module that *writes* the layout — already
normalised in `setUp` and restored on cleanup, and its docstring had even
predicted this failure. But restoring writes back whatever was *found*, so
residue is preserved faithfully rather than decaying. The state cannot
recover on its own; only a reader that normalises can clear it.

So the rule is on the **readers**, where it is easy to forget because they
never write anything:

> Every module that renders the home screen calls
> `_e2e.normalise_home_layout(session)` before it builds a browser.

It writes `""` — "use this slot's default" — to every slot in
`home_sections.SLOT_COUNT`, touching no other key, since the guide settings
share the document. It deliberately does **not** restore afterwards.

Two symptoms to recognise, because neither names the layout:

* `latestmedia` in two slots makes the repository build one Latest row per
  library **per slot**. Those rows are keyed by library, so the ids collide
  and `test_keyboard_nav` fails on duplicate node ids. (The renderer-side
  half of that is a real bug and is fixed separately — see `HomePage`'s
  `_unique`.)
* A layout without `smalllibrarytiles` has no library-tiles row at all, and
  the same module fails with "no node id containing &lt;guid&gt;".

**Waiting for the server is part of this.** `/System/Info/Public` answers
long before the library is usable, and `stdjflib serve` runs a rescan at the
end. Poll `/ScheduledTasks` for `RefreshLibrary` to reach `State == "Idle"`
before trusting any result; alive is not correct.

## 12. A fixture is a suspect whenever it is what makes a repair provable

The review round on `sync-and-lifecycle-fixes` found seven defects **inside
the previous round's own fixes**, and the repairs were reverted rather than
patched a third time. Two of those failures were not code at all: a test
manufactured the one condition that made a wrong repair look right. The
repairs were redone afterwards, and the same shape appeared twice more —
four instances in one arc, which is why it is written here rather than in a
commit message.

All four have the same silhouette. The fixture is the thing standing between
the assertion and the truth, and it is written by whoever most wants the
repair to work.

- **It supplied a value no caller supplies.** A cross-server test called
  `_add_row` with an explicit `server_id`; the only real caller read that out of
  the apiclient's config, under a key that client never assigns
  (`docs/do-not-fix.md` 1). The repair scoped on that column and was green —
  against a catalog where the column was `NULL` on every row ever written, and
  which has since been dropped outright.
- **It cleared the cache that would have failed the assertion.** The same
  test emptied `_library_ids` between the two writes, so the process-wide
  cache the defect lived in was never exercised.
- **It left behind state a real transition removes.** A test for a login
  that outlives a user switch kept the credential in the manager's live
  list. `_adopt_active_user` repoints that list at the user now active, so
  the real path has no such entry — and the mutation that should have failed
  passed.
- **It gave the wrong thing enough time to finish.** A test that the connect
  no longer waits for a name lookup held the resolver for two seconds and
  gave the connect five to return, so a lookup still *on* the connect path
  completed inside the wait and the test passed. The budgets have to be
  lopsided the other way.

**The check that finds these is a mutation, not a re-reading.** Each was
caught by breaking the code the test claims to pin and watching the test stay
green — not by inspecting the fixture, which reads as reasonable in all four
cases. So: after writing a test for a repair, put the defect back and watch
it fail. If it does not, the fixture is the reason.

The corollary for reviewing someone else's green test: ask *which field of
the real object did this stand-in not model*, and *what does this fixture set
up that the real path would have torn down*.

### Running a round: `tools/mutate_round.py`

A plan under `tools/mutation_plans/` lists `(what breaking this represents,
file, old, new)` and a `SELECT` of `-k` filters. The runner checks every
`old` matches exactly once, runs the baseline, then breaks and restores one
mutation at a time. A survivor is a claim without evidence.

```sh
python3 tools/mutate_round.py <plan> --list        # what is in it
python3 tools/mutate_round.py <plan> --dry-run     # do the patterns still apply?
xvfb-run -a python3 tools/mutate_round.py <plan> -p            # the round
xvfb-run -a python3 tools/mutate_round.py <plan> --only retreat -p
python3 tools/mutate_round.py --restore            # after a killed round
```

**`--only` is the negative control**, and it is the one to reach for the
moment a survivor gets a new test: one mutation instead of thirty, so seconds
instead of half an hour. A round you can only run in twenty-five minute
blocks is one you run once and then work alongside, which is the whole
problem below.

`-p` is a smaller win than it sounds and the measurement is recorded so
nobody re-derives it: 38.7 s per suite run against 47 s serial on this plan,
about 18%. A parallel run costs its slowest module, and `test_sync_manager.py`
is ~40 s on its own. Use it for the *report* rather than the clock — a serial
failure says `FAILED (failures=1)` and a parallel one names the module that
killed the mutation. It also carries a deadline (`-t`, default 300 s): a
mutation can turn a loop infinite, and a hung worker inside a round holds a
mutation in the working tree for as long as it lasts.

**While a round runs, the working tree is not yours.** It rewrites the
plan's files under you and, until 2026-09-13, left no trace of having done
so. Three failures in one session, and the runner now has one guard each:

- **A killed round left a mutation in the tree.** `finally: restore` does not
  survive SIGKILL and nothing recorded which file was broken, so a mutation
  sat in `sync/db.py` looking exactly like code somebody had written; the
  suite found it three modules later. Every write is now journalled to
  `.git/mutate-round/` *before* it happens, and the next invocation puts it
  back — `--restore` does only that.
- **Editing source mid-round reverts it, twenty minutes later.** Backups are
  taken once, up front, so an edit made during mutation 4 is overwritten by
  the restore for mutation 19. The runner now hashes what it wrote and
  **rescues** anything different into `.git/mutate-round/rescued/` with a
  loud line, rather than throwing it away.
- **A live round is invisible.** The journal carries a pid, and a second
  invocation refuses while it is alive.

`.git/mutate-round/` rather than `/tmp`, for the same reason the diagnose
tooling keeps its evidence there: outside the working tree, so no revert
carries it off, and findable after a reboot without remembering a random
directory name.

`-p` runs the SELECTed modules concurrently through `run_tests_parallel.py`'s
own worker, so the argv neutralisation and the `sys.path` insert stay in one
place. It declines when a `-k` pattern does not name a module file, because
`-k` also matches class and method names and a module-level split would then
run a different set than the serial round does. Same trade as §9: it cannot
see cross-module interference.

### Known flakes on this hardware

Re-run before believing any of these; each has been seen once in a full run
and passed in isolation.

- `test_mpvtk_browser.TestRealMousePosPath.test_the_real_pointer_hovers_leaves_and_comes_back`
  (integration, libmpv) — `mouse-pos` comes back `{hover: False, x: -1,
  y: -1}`. It drives a real mpv window with `mouse x y` and reads back after
  a fixed delay, which is the timing this class of test cannot promise under
  Xvfb. Passed on jsonipc in the same run and on re-run.
- `test_offline_sync.PushedUserDataReachesTheCatalogTest.test_not_even_the_one_that_finishes_it`
  (e2e, Windows VM, seen 2026-09-12) — asserts that a completing progress
  report announces nothing, and received a payload of ~100 items with season
  and series roll-ups (`PlayedPercentage: 100`, `UnplayedItemCount: 0`). That
  is a *series-wide mark*, which is not what the test does: it is
  `test_a_series_mark_fans_out_the_way_the_server_does` arriving late, and
  the test's own docstring already names that hazard ("the previous action's
  event arriving after the inbox was cleared"). Passed alone and in a clean
  full re-run. Two logical CPUs make a late websocket delivery much likelier
  here than on the Linux box.
- Leftover playlists from an e2e run killed mid-flight — a killed process
  never runs its cleanup. Section 11.
- `test_playback_advance` under heavy machine load: the episode it advances
  into is ten seconds long and plays out while the earlier waits run.
