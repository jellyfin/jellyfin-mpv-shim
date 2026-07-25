# A low-risk method for the decomposition

`ARCHITECTURE_TARGET.md` says where to go. This says how to get there without
a long-lived branch, a big-bang merge, or a season of regression reports.

The problem this method exists to solve is specific: **hand-testing a client
this size gives strong signal on the paths you use and almost none on the
rest.** You will exercise "play a movie" a hundred times and never once
exercise "the server returns 500 while a playlist edit is in flight". Those
are exactly the paths a decomposition breaks, because they are the ones held
together by shared state that the refactor is moving.

So the method is built around getting mechanical signal where hands can't
reach, and reserving hand-testing for what only hands can judge.

---

## 0. Where the risk actually is

From `tools/coverage_all.sh` (union of the unit suite, both fake-mpv backends
and the real-mpv legs — 64.4% overall):

> **Step 4 is done.** `ui.py` is now at **67.2%** (`_PlayerController` 80.9%,
> `UserInterface` 79.6%), so the gate below is cleared and steps 5+ are
> unblocked. The table is kept as the *reason* the gate existed; re-measure
> with `tools/coverage_all.sh` rather than trusting these numbers.

| module | cover | why it matters here |
|---|---|---|
| `mpvtk_browser/ui.py` | **41.6%** → 67.2% | the browser↔player boundary the refactor formalises |
| `media.py` | 29.9% | stream/path/transcode decisions |
| `menu.py` | 32.5% | OSD menu |
| `syncplay.py` | 34.4% | timing loop |
| `event_handler.py` | 36.0% | the whole remote-control surface |
| `player.py` | 60.4% | branch-heavy; line coverage flatters it |
| `mpvtk_browser/app.py` | 88.4% | the god object is the *best* covered part |

Read that table again: **the code being refactored is well covered; the seam
it will be refactored across is not.** `_PlayerController` — every method the
browser uses to reach the player — is almost entirely untested, and
`ui.py`'s worst functions are `_connect`, `on_mpv_recreated`, `login_servers`
and `switch_user`: startup, teardown and recovery. Precisely the paths
hand-testing skips.

**Therefore step 4 of the sequencing table is a hard prerequisite, not a
nice-to-have.** Covering `_PlayerController` is cheap (it is ~90 thin
delegating methods) and it is the safety net everything else hangs from.

**How it was actually covered**, because the technique generalises. The class
has one shape — lazily import a singleton, delegate, catch `Exception`,
return a documented fallback — so the valuable assertion is not 99 individual
delegation tests but that the *contract holds uniformly*. One sweep
substitutes a broken collaborator and calls all 40 guarded methods, asserting
none propagates. That is the property step 5's `PlayerGateway` must preserve,
and it covers methods nobody thought to write out.

Three things the sweep needed, all of which generalise to the next one:

* **Exclude what reaches outside the process.** The first run called
  `open_config_folder`, which does `subprocess.Popen(["xdg-open", …])`, and
  opened a file manager window mid-test. `SIDE_EFFECTING` names those, and a
  companion test fails if a name in it stops existing — otherwise the
  exclusion silently becomes a hole.
* **Model the failure realistically.** The first fake raised on *attribute
  access*; twelve methods "failed" as a result. They were right and the fake
  was wrong: `syncManager.db` and `clientManager.clients` are plain data
  attributes that cannot raise, which is exactly why those methods read them
  outside their `try`. Raising a non-`AttributeError` from `__getattr__` also
  defeats `getattr(obj, "db", None)`. The honest model is *traversal
  succeeds, calls fail*.
* **Separate contracts.** `_sync` catches only to log and then re-raises, and
  `add_user`/`rename_user` never catch at all — catching there made the field
  clear and nothing happen. Those are pinned by their own tests, and the
  sweep detects re-raising from the AST rather than being told.

---

## 1. The five kinds of signal

### 1.1 Characterization snapshots — "the pixels did not move"

Already built: `tests/test_scene_snapshots.py` + `tests/_scene_snapshot.py`.

This is the highest-value tool available for *this* codebase, because the UI
is declarative. `layout()` emits the exact node list pushed to the renderer,
so a byte-identical node list means byte-identical pixels. Four screens are
pinned today (426 nodes); the snapshot is deterministic once the strip `src`
and version counter are normalized, which the harness does.

Verified working: changing `theme.TEXT_FG` by one constant fails the settings
snapshot with a per-node diff naming the changed nodes.

```
python3 -m unittest tests.test_scene_snapshots     # check
python3 tests/test_scene_snapshots.py --update     # regenerate, then REVIEW
```

What it catches that behavioural tests do not: a row that moved, a badge that
vanished, a column that reordered, a padding that changed — all the things a
"pure" refactor is supposed to leave alone and a mixin extraction routinely
does not.

The failure mode to guard against is reflexive regeneration. Keep the set
small (a handful of representative screens, not every route) so that a diff is
always worth reading. `test_snapshots_are_not_trivially_small` stops a
baseline being captured before its async load settled — that mistake produces
a snapshot of a spinner, which passes forever and asserts nothing.

**Extend this before each extraction**, not once at the start: add the screens
the step touches, then extract.

### 1.2 AST invariants — "the structure still holds"

Already built: `tests/test_source_invariants.py`, plus the pre-existing
`test_mpvtk_browser_mixins.py` (no name defined twice), `test_no_tkinter.py`,
`test_python_lua_constants.py` and `test_harness_isolation.py`.

These encode rules a reviewer cannot reliably check and no behavioural test
can fail on. The ones already in place caught real drift: harness isolation
caught `build_player` falling behind `PlayerManager.__init__` (five red
integration legs), and the orphaned-docstring rule caught `enter_browse`
losing its `__doc__` to an inserted guard clause.

Write a new one for each extraction, *before* the extraction, expressing the
invariant that step establishes. Examples for the planned steps:

| step | invariant to assert |
|---|---|
| components extraction | no module under `components/` imports `app` or references `self.route` |
| `AsyncRunner` | `_epoch` is referenced in exactly one module |
| `Navigator` | `nav_stack` is assigned in exactly one module |
| `Page` classes | every `PAGES` key has a class, and no page class touches an attribute outside `PageContext` |
| `MpvSession` | `_mpv_errors` is caught in exactly one module |

That last one is worth writing early even though the step is late: it turns
"twenty-six copies of the disconnect policy" from a description into a number
the test suite tracks downward.

### 1.3 Coverage deltas — "I did not silently drop a branch"

`tools/coverage_all.sh --sort=missing` before and after each step. A
refactor that is behaviour-preserving should not *reduce* covered lines. If it
does, either code became unreachable (a bug) or a test stopped reaching it (a
gap opened).

Per-file, per-function drill-down for the module you are about to touch:

```
tools/coverage_report.py --merge <dir>/merged.json --functions mpvtk_browser/ui.py
```

Use it as a pre-flight: **if the method you are about to move is at zero,
write a test for it first.** Moving untested code is how a "pure move" turns
out not to have been one.

### 1.4 mypy — "the types still line up", with a caveat

`tools/mypy_gate.sh` runs mypy against a committed baseline and fails only on
*new* findings. The tree has ~90 pre-existing ones, almost all
`var-annotated` / `assignment` noise in code never written to be
type-checked; fixing them is a separate project and blocking on them would
just get the check ignored. The run takes under a second.

```
tools/mypy_gate.sh            # check
tools/mypy_gate.sh --update   # re-baseline after intentional changes
```

**It does not catch the failure mode this refactor actually produces**, and
that was measured rather than assumed. Renaming a gateway method and leaving
`self._safe(lambda c: c.check_updates())` behind:

| check | result |
|---|---|
| `mypy` | 0 findings |
| `tests/test_late_bound_calls.py` | 4 findings, with file:line |

The reason is that the callback parameters are unannotated, so mypy infers
`Any` for `c` and stops caring. In an isolated file, annotating the seam
(`fn: Callable[[Gateway], Any]`) *does* make mypy catch it — precisely, with
a "maybe you meant" suggestion. Applying the same annotation in-repo did
**not** make it fire, and the reason was not chased down. So: annotating the
seams is a promising direction, not a solved one, and until someone
investigates it, the AST checks are what covers this.

Use mypy for what it demonstrably does catch here — genuine type errors in
new code, wrong argument types to typed APIs, `None` handling — and do not
retire anything in §1.2 on the strength of it.

### 1.5 Fault injection — "the error paths still work"

This is the one that addresses hand-testing's real blind spot, and the one
the repo does not have yet.

The browser's error handling is concentrated and uniform: `run_async`'s
`on_error`, `_route_async`'s `_error` + offline fallback, `_edit_call`'s
rollback + toast, `_safe`'s swallow. That uniformity makes it injectable —
a source wrapper that fails the Nth call reaches every one of them:

```python
class FlakySource:
    """Wraps a LibrarySource and raises on the Nth call to any method.
    Sweeping N from 1..K drives a different error path each run."""
    def __init__(self, inner, fail_at, exc=ConnectionError):
        ...
```

Then, per screen: for N in 1..K, load the route under `FlakySource(N)` and
assert three things that must hold for *every* failure:

1. `build()` does not raise (the UI never goes black);
2. the route ends with either data or `_error` set (never a permanent
   spinner — `run_async`'s docstring records this exact bug: "an unreachable
   server looked like a hang");
3. no paging/loading guard is left set (`_loading` surviving a failure is the
   other recorded bug — it "silently killed infinite scroll for a route").

Those three are properties, not expected values, so one loop covers every
error path in every screen without anyone enumerating them. That is the
coverage hand-testing cannot produce, and it is worth building **before** the
page extraction rather than after, because the whole point of `PageContext` is
that pages stop sharing the error-handling machinery.

A second injector is worth having for `player.py` step 7: a handle that raises
`_mpv_errors` on the Nth touch, sweeping N to prove the disconnect policy is
uniform. `tests/integration/_harness.py`'s `FakeMPV` is most of it already.

---

## 2. The step recipe

Every extraction, same six moves. Do not compress them; the ordering is what
keeps each commit revertible.

1. **Measure.** `--functions` on the target module. Anything at zero that the
   step touches gets a test now, as its own commit.
2. **Pin.** Add the screens/behaviours this step touches to the snapshot set.
   Commit the baselines separately, so the refactor's diff contains no
   snapshot churn.
3. **Assert the invariant.** Write the AST test for the property the step is
   about to establish. It fails. That is correct — it is a TODO the suite
   enforces.
4. **Move, mechanically.** Extract with no behaviour change at all: no
   renames, no signature improvements, no "while I'm here". Delegate from the
   old location so callers are untouched.
5. **Verify.** Unit suite, snapshots, `run_integration.py` (both backends),
   coverage delta. The AST invariant now passes.
6. **Then** tidy: update call sites, delete the delegation, move the
   long-form comments to `docs/` per §4 of the target document.

Steps 4 and 6 in separate commits is the single most valuable discipline here.
A pure move that breaks something is bisectable in seconds; a move-plus-tidy
is not.

---

## 3. What hand-testing is actually for

Reserve it for what the mechanical signal genuinely cannot judge, and do it
against a checklist rather than by exploring. `docs/REGRESSION_CHECKLIST.md`
already exists for this.

Hands are the only source of truth for:

- **Timing and feel** — scroll smoothness, HUD summon/hide, whether a
  placeholder is visible long enough to be noticed.
- **Real mpv behaviour under load** — a snapshot proves the scene is right,
  not that mpv composited it without tearing.
- **Multi-process / multi-device** — casting from a phone, SyncPlay, the tray,
  a second instance.
- **Long-running state** — the leaks and drifts that need an hour of use.

Hands are a *poor* source of truth for error handling, startup/teardown
ordering, and rarely-taken branches — which is what §1.4 exists to replace.
Do not spend hand-testing budget there; you will not reach them, and you will
believe you have.

---

## 4. Sanity limits

### Which branch is "stable"

`master` is **not** the baseline for this work. `master` is what ships to live
packaging (AUR and friends), and the mpvtk UI has not been merged there yet
precisely because it wants this refactor and a hand code-review first.

For everything in this document, the stable branch is **`local-ui-mpvtk`**.
Treat it exactly as you would treat `master`:

- it must stay shippable and hand-testable at every commit;
- refactoring happens on `mpvtk-ui-refactor` (or a per-step branch off it),
  never directly on it;
- a step lands on `local-ui-mpvtk` only once its suite, snapshots and the
  two-backend integration matrix are green.

`master` merges are a separate decision, made after the refactor and the
hand review — not something any step here should assume or block on.

### The rest

- **One extraction per branch.** The god object took a long time to grow; it
  does not need to be gone in one pass.
- **`local-ui-mpvtk` stays shippable.** Every step is independently revertible
  by design, so a step that turns out to be wrong is reverted, not patched.
- **If a step needs a snapshot regenerated for a reason you cannot state in
  one sentence, stop.** That is the signal that the "pure move" moved
  behaviour, and the snapshot is doing exactly its job.
- **Do not refactor `mpvtk/`.** It is already a clean, independent toolkit at
  90-99% coverage on its core. Leave it alone.
