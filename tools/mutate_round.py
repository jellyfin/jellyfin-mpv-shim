#!/usr/bin/env python3
"""Run a round's mutations against the finished tree and report survivors.

    xvfb-run -a python3 tools/mutate_round.py tools/mutation_plans/<plan>.py
    xvfb-run -a python3 tools/mutate_round.py <plan> --dry-run
    xvfb-run -a python3 tools/mutate_round.py <plan> -p        # parallel
    xvfb-run -a python3 tools/mutate_round.py <plan> --only retreat
    python3 tools/mutate_round.py <plan> --list
    python3 tools/mutate_round.py --restore

A green suite says nothing about a fix. Breaking the fix on purpose and
watching the suite go red is the evidence, and it has to be produced for
every fix in a round **against the tree those fixes ended up in** -- not one
at a time as each is written.

That ordering is the whole point of this script. Measured here: two repairs
from one round overlapped, and the second made every case of the first's
test unreachable, so the guard it was named for survived being replaced with
`if False:` while the suite stayed green. Nothing checked per fix could see
that; the fix that hid it had not been written yet. A later run found two
more survivors, and both times the defect was in the *test*, not the code.

A plan is a Python file defining:

    SELECT = ["-k", "test_sync_manager", "-k", "test_auth_header_truth_table"]
    MUTATIONS = [
        ("what breaking this represents", "path/to/file.py", old, new),
        ...
    ]

`SELECT` is passed to `unittest discover tests`. Keep it wide enough that a
mutation can be killed by a test nobody thought to point at it, and narrow
enough that the round finishes -- this is one suite run per mutation.

Three things it does that a shell loop gets wrong:

* **Refuses to start unless the baseline is green.** Against a red tree
  every mutation is "killed" and the run means nothing.
* **Refuses to start unless every `old` appears exactly once.** A pattern
  that matches nothing is silently no mutation at all, and a survivor and a
  typo look identical in the output.
* **Restores from a byte copy and never from git.** This work sits
  uncommitted across many files, and `git checkout -- <file>` has destroyed
  a session's worth of it. `PYTHONDONTWRITEBYTECODE=1` throughout, because a
  `.pyc` is revalidated on source mtime **and size** -- a mutation that
  keeps the length, restored inside the same second, leaves Python running
  the mutated bytecode. That direction makes a mutation look like it
  survived, which is a test you then believe in.

## While a round runs, the tree is not yours

Three failures on 2026-09-13, all of them the same misunderstanding, and the
guards below are one per failure. **A round rewrites files under you and the
old version left no trace of having done so.**

* **A round was killed mid-mutation.** `finally: restore` does not run
  through SIGKILL, and nothing recorded which file was currently broken --
  so a mutation stayed in the working tree, looking exactly like code
  somebody wrote. The suite caught it three modules later. Now every write
  is journalled to ``.git/mutate-round/`` first, and the next invocation
  puts it back before doing anything else (``--restore`` does only that).
* **Source was edited while a round was running.** Backups are taken once,
  up front, so the edit was reverted by a restore for an unrelated mutation
  -- silently, twenty minutes later. Now the on-disk bytes are checked
  against what the round itself wrote, and anything else is **rescued** into
  ``.git/mutate-round/rescued/`` with a loud line rather than thrown away.
* **A live round is not obvious.** A second invocation would have raced the
  first over the same files. The journal carries a pid and a second run
  refuses while it is alive.
* **A round was timed out.** Two more on the same day, both a sweep run under
  ``timeout``, which sends SIGTERM first -- so the tree stayed mutated until
  somebody ran ``--restore`` by hand. The journal made that recoverable but
  not automatic, and "recoverable by the next invocation" is not the same
  promise as "the tree is yours again when the command exits". SIGTERM and
  SIGINT now restore before exiting.

Backups and journal live in ``.git/mutate-round/`` rather than ``/tmp``:
outside the working tree, so no revert carries them off, and findable after a
reboot without remembering a random directory name. Same place the diagnose
tooling keeps its evidence.

## Making a round cheap enough to re-run

A round is one suite run per mutation, which was ~25 minutes for thirty --
long enough that the temptation is to do something else meanwhile, which is
what caused two of the three failures above.

**``--only`` is the lever that matters**, and it is the only one that changes
the order of magnitude: run the mutations whose names contain a string and
nothing else. That is the negative control you want the moment a survivor
gets a new test -- one mutation, one suite run, watch the new test fail.

**``-p`` is worth less than it looks and the number is here so nobody
re-measures it.** Measured 2026-09-13 on this plan: 38.7 s per suite run
against 47 s serial, about 18%. It cannot do better, because the wall clock
of a parallel run is its slowest module and `test_sync_manager.py` alone is
~40 s -- it is in every round here and it is the long pole in all of them.
What ``-p`` is actually good for is the report: a serial failure says
`FAILED (failures=1)`, a parallel one says *which module* killed the
mutation and how, which is most of the work of reading a survivor list.

It refuses when a SELECT pattern does not name a module file, because then
`-k` is filtering by test name and a module-level split would run the wrong
set.
"""

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")
PARALLEL = os.path.join(ROOT, "tools", "run_tests_parallel.py")

#: Seconds a `-p` round gives all its workers together. Matches
#: run_tests_parallel's own per-module default; the slowest module in this
#: repo is well under a minute, so anything near this is a hang.
WORKER_TIMEOUT = 300.0

#: Journal, backups and rescues. Inside .git on purpose -- see the module
#: docstring. Never committed, never in `git status`, and it survives the
#: revert that this work exists to make safe.
STATE_DIR = os.path.join(ROOT, ".git", "mutate-round")


def _state_path():
    """The journal, derived from STATE_DIR **at call time**.

    Not a module constant beside it: the two can then disagree, and they did
    -- a test that pointed STATE_DIR at a sandbox went on writing its journal
    into the real `.git/mutate-round/`, which passed silently for as long as
    that directory happened to exist and became a crash the moment it was
    cleaned up. A path derived from one place cannot drift from it.
    """
    return os.path.join(STATE_DIR, "state.json")


def print_(*args):
    """Print and flush. A round is piped often enough (`| tail`) that a
    block-buffered stdout shows nothing at all until it ends, which reads as
    a hang and invites killing it -- the failure this file is now full of
    guards against.

    A closed pipe (`| head`) is not an error worth a traceback, and a
    traceback out of here during a round would skip the restore.
    """
    try:
        print(*args, flush=True)
    except BrokenPipeError:
        pass


def _flat(rel):
    return rel.replace(os.sep, "_").replace("/", "_")


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def load_plan(path):
    """Import a plan file and return (mutations, select)."""
    spec = importlib.util.spec_from_file_location("_mutation_plan", path)
    if spec is None or spec.loader is None:
        raise SystemExit("not a python file: %s" % path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    try:
        return list(module.MUTATIONS), list(module.SELECT)
    except AttributeError as exc:
        raise SystemExit("%s must define MUTATIONS and SELECT (%s)"
                         % (path, exc))


def check_patterns(mutations):
    """[(name, path, count)] for every `old` that does not appear once.

    Separate from the run so a typo is reported before an hour of suite runs
    rather than as a survivor at the end of one.
    """
    bad = []
    for name, rel, old, _new in mutations:
        full = os.path.join(ROOT, rel)
        if not os.path.exists(full):
            bad.append((name, rel, -1))
            continue
        with open(full, encoding="utf-8") as fh:
            count = fh.read().count(old)
        if count != 1:
            bad.append((name, rel, count))
    return bad


# -- the journal ------------------------------------------------------------

def _alive(pid):
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    except Exception:
        return False
    return True


def read_state():
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def write_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    state_path = _state_path()
    tmp = state_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=1)
    os.replace(tmp, state_path)          # a torn journal is worse than none


def rescue_if_unexpected(state, rel, allowed=None):
    """Copy `rel` aside if it is not one of the contents the round put there.

    ``allowed`` defaults to "the content we backed up", which is what the
    mutation loop wants: anything else at that point is somebody's edit.
    `restore_from_state` widens it by the mutation currently applied.

    Returns whether it rescued. Never raises out: losing the warning is
    better than failing a round in its cleanup path.
    """
    if allowed is None:
        allowed = {(state.get("expected") or {}).get(rel)}
    allowed = {h for h in allowed if h}
    full = os.path.join(ROOT, rel)
    if not allowed or not os.path.exists(full):
        return False
    try:
        if _sha(full) in allowed:
            return False
        rescue_dir = os.path.join(STATE_DIR, "rescued")
        os.makedirs(rescue_dir, exist_ok=True)
        rescue = os.path.join(rescue_dir,
                              "%s.%d" % (_flat(rel), int(time.time())))
        shutil.copyfile(full, rescue)
    except OSError:
        print_("  ! %s changed under the round and could not be rescued."
               % rel)
        return False
    print_("  ! %s changed under the round; your version is in %s"
           % (rel, rescue))
    return True


def restore_from_state(state, reason, only=None):
    """Put backed-up files back, rescuing anything that is not what the round
    itself last wrote. Returns the number of files restored.

    ``only`` limits it to one path, which is what the per-mutation restore
    wants. **One implementation for all three restores** -- after a
    mutation, at the end of the round, and after a crash. There were three,
    and only the first checked anything: the end-of-round loop did a bare
    `shutil.copyfile` over every file, so an edit made while the round was
    on a *different* file was reverted with no warning and no rescue. That
    is failure #2 from the module docstring, still live in the code written
    to close it, and it went on to eat a third edit before this was fixed.

    **Two hashes are acceptable for the file a killed round was holding**:
    the untouched original, and the mutation the round had just written. Only
    the first was checked at first, so recovering a crash reported the
    round's own mutation as somebody's edit -- and a rescue warning that
    fires on the ordinary path is one you learn to scroll past, which costs
    the case it exists for.
    """
    backups = state.get("backups") or {}
    expected = state.get("expected") or {}
    applied = state.get("applied") or {}
    restored = 0
    for rel, backup in sorted(backups.items()):
        if only is not None and rel != only:
            continue
        full = os.path.join(ROOT, rel)
        allowed = {expected.get(rel)}
        if applied.get("rel") == rel:
            allowed.add(applied.get("sha"))
        rescue_if_unexpected(state, rel, allowed)
        if os.path.exists(backup):
            shutil.copyfile(backup, full)
            restored += 1
    if restored and only is None:
        print_("restored %d file(s) from %s (%s)"
               % (restored, state.get("backup_dir"), reason))
    return restored


def recover_if_needed():
    """Undo a round that did not finish. Returns False if one is live."""
    state = read_state()
    if state is None:
        return True
    pid = state.get("pid")
    if _alive(pid) and pid != os.getpid():
        print_("A mutation round is already running (pid %s, started %s).\n"
               "It is rewriting %s.\n"
               "Wait for it, or kill it and run --restore; do not edit those "
               "files meanwhile."
               % (pid, state.get("started"),
                  ", ".join(sorted(state.get("backups") or {}))))
        return False
    applied = state.get("applied")
    if applied:
        print_("A previous round stopped with a mutation still applied:\n"
               "  %s\n  in %s" % (applied.get("name"), applied.get("rel")))
    restore_from_state(state, "previous round did not finish")
    try:
        os.remove(_state_path())
    except OSError:
        pass
    return True


# -- running the suite ------------------------------------------------------

def _select_modules(select):
    """The test module filenames a SELECT's `-k` patterns name, or None.

    None means at least one pattern is not a module name, so the round has
    to go through `discover -k`: `-k` also matches class and method names,
    and splitting by module would then run a different set of tests than the
    serial round does.
    """
    available = [os.path.basename(p)
                 for p in glob.glob(os.path.join(TESTS, "test_*.py"))]
    patterns = [select[i + 1] for i, tok in enumerate(select)
                if tok == "-k" and i + 1 < len(select)]
    if not patterns or len(patterns) * 2 != len(select):
        return None
    modules = []
    for pattern in patterns:
        hits = [m for m in available if pattern in m]
        if not hits:
            return None
        for m in hits:
            if m not in modules:
                modules.append(m)
    return sorted(modules)


def _run_suite(select):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "tests"] + list(select),
        cwd=ROOT, capture_output=True, text=True, env=env)
    tail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
    return proc.returncode == 0, tail


def _run_suite_parallel(modules, timeout=WORKER_TIMEOUT):
    """One process per module, through run_tests_parallel's own worker.

    Reused rather than reimplemented: that worker is where the argv
    neutralisation, the sys.path insert and the os._exit are, and every one
    of them is load-bearing (see its docstring). The parent reads the
    printed RESULT line rather than the exit status for the same reason it
    does there -- a worker that has passed still exits hard.

    **A worker that never finishes is a failure, not a wait.** A mutation can
    turn a loop infinite, and this box was carrying three `test_sync_manager`
    workers stuck in `futex_wait_queue` from a run two days earlier, so it is
    not hypothetical either. Without the deadline one of those hangs the
    whole round, holding a mutation in the tree for as long as it lasts --
    the exact state the journal exists to get out of. Own process group per
    worker (`start_new_session`), so the kill takes the mpv it started with
    it rather than orphaning one.
    """
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    procs, out = {}, {}
    for mod in modules:
        proc = subprocess.Popen(
            [sys.executable, PARALLEL, "--worker", mod],
            cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            start_new_session=True, env=env)
        sink = []
        # Drain now, in a thread: a worker that outruns the 64K pipe buffer
        # blocks in write() forever if nobody is reading. Same reason
        # run_tests_parallel does it.
        pump = threading.Thread(target=lambda p=proc, s=sink: s.append(
            p.stdout.read()), daemon=True)
        pump.start()
        procs[mod] = (proc, pump, sink)
    # One deadline for the round, not one per module: they are all running
    # already, so a per-module timeout would grant the last one started the
    # sum of everything before it.
    deadline = time.time() + timeout
    failed = []
    for mod, (proc, pump, sink) in procs.items():
        try:
            proc.wait(timeout=max(1.0, deadline - time.time()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                proc.kill()
            proc.wait()
            failed.append("%s TIMED OUT after %ds" % (mod, timeout))
            continue
        finally:
            pump.join(30)
        out[mod] = "".join(sink)
        line = [ln for ln in out[mod].splitlines()
                if ln.startswith("JMS-RESULT")]
        if not line:
            failed.append("%s crashed" % mod)
            continue
        _tag, _total, fails, errors = line[-1].split()
        if int(fails) or int(errors):
            failed.append("%s %sF %sE" % (mod, fails, errors))
    return not failed, ("OK" if not failed else " ".join(failed))


# -- the round --------------------------------------------------------------

def run(mutations, select, parallel=False,
        timeout=WORKER_TIMEOUT):
    files = sorted({rel for _n, rel, _o, _w in mutations})
    backup_dir = os.path.join(STATE_DIR, "backups")
    os.makedirs(backup_dir, exist_ok=True)

    state = {"pid": os.getpid(),
             "started": time.strftime("%Y-%m-%d %H:%M:%S"),
             "backup_dir": backup_dir,
             "backups": {}, "expected": {}, "applied": None}
    for rel in files:
        backup = os.path.join(backup_dir, _flat(rel))
        shutil.copyfile(os.path.join(ROOT, rel), backup)
        state["backups"][rel] = backup
        state["expected"][rel] = _sha(os.path.join(ROOT, rel))
    write_state(state)

    modules = _select_modules(select) if parallel else None
    if parallel and modules is None:
        print_("--parallel needs every SELECT entry to be a `-k <module>`; "
               "this plan filters by something else. Running serially.")

    def suite():
        return (_run_suite_parallel(modules, timeout) if modules
                else _run_suite(select))

    def restore(rel):
        restore_from_state(state, "after a mutation", only=rel)
        state["applied"] = None
        write_state(state)

    # **Put the tree back on SIGTERM**, rather than leaving it to the next
    # invocation. `timeout` sends SIGTERM before SIGKILL and a sweep is
    # routinely run under one, so this is the ordinary way a long round ends,
    # not an exotic one. Twice on 2026-09-13 a round was timed out and left a
    # mutation in the working tree until somebody ran `--restore` by hand.
    #
    # Restore first, then raise, so the exception unwinds through the suite
    # runner's own `finally` and its worker process groups are killed -- an
    # `os._exit` here would be tidy for the tree and leave orphaned mpv
    # processes behind, which is the trade the wrong way round. The journal
    # stays the backstop for SIGKILL, which cannot be caught.
    dying = []

    def _on_signal(signum, _frame):
        if dying:          # a second signal during cleanup must not re-enter
            return
        dying.append(signum)
        print_("\n%s: putting the tree back before exiting."
               % signal.Signals(signum).name)
        try:
            restore_from_state(state, "on %s" % signal.Signals(signum).name)
        finally:
            state["applied"] = None
            write_state(state)
        raise SystemExit(128 + signum)

    previous = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            previous[sig] = signal.signal(sig, _on_signal)
        except (ValueError, OSError):
            pass           # not the main thread, or no such signal here

    try:
        ok, tail = suite()
        print_("baseline: %s  (%s)%s"
               % ("OK" if ok else "FAILED", tail,
                  "  [parallel: %d modules]" % len(modules) if modules else ""))
        if not ok:
            print_("\nA red baseline makes every mutation look killed. "
                   "Stopping.")
            return None

        survivors = []
        for index, (name, rel, old, new) in enumerate(mutations, 1):
            full = os.path.join(ROOT, rel)
            # **Noticed here, not only at restore time.** An edit made while
            # the round was on a different file is still on disk now, and
            # mutating *that* would fold it into the hash the restore checks
            # against -- so the edit would look like the round's own work and
            # be reverted with no rescue. Which is what happened: the guard
            # written to stop exactly this could not see it.
            rescue_if_unexpected(state, rel)
            # From the backup, never from disk, for the same reason: the
            # round validated that content and every mutation is a delta on
            # it.
            with open(state["backups"][rel], encoding="utf-8") as fh:
                source = fh.read()
            mutated = source.replace(old, new)
            # Journalled BEFORE the write, so a kill between the two leaves a
            # record that points at the right file either way.
            state["applied"] = {"rel": rel, "name": name,
                                "sha": hashlib.sha256(
                                    mutated.encode("utf-8")).hexdigest()}
            write_state(state)
            # newline="" or the bytes on disk are not the bytes we hashed:
            # Windows translates "\n" to "\r\n" on a text-mode write, while
            # `_sha` reads back in binary, so every restore saw its own
            # mutation as somebody's edit and rescued it. A no-op on Linux,
            # which is why it survived to be found by the Windows suite.
            with open(full, "w", encoding="utf-8", newline="") as fh:
                fh.write(mutated)
            try:
                ok, tail = suite()
            finally:
                restore(rel)
            print_("%-8s %2d/%d %-52s %s"
                   % ("SURVIVED" if ok else "killed", index, len(mutations),
                      name[:52], tail))
            if ok:
                survivors.append(name)
    finally:
        # Through the same checked path, not a bare copy: this loop is where
        # an edit made during an *unrelated* mutation used to disappear.
        # Named for the signal case so the second line is not read as a
        # second event: the handler has already restored, and this pass is the
        # `SystemExit` unwinding through here. It is kept rather than skipped
        # because it is idempotent (`restore_from_state` accepts both the
        # original and the applied hash) and it is the only thing that runs if
        # the handler's own restore raised.
        if dying:
            reason = "already restored on %s" % signal.Signals(dying[0]).name
        else:
            reason = "end of round"
        restore_from_state(state, reason)
        state["applied"] = None
        write_state(state)
        # Handlers are global state and `run` is importable; the recovery
        # tests call it in-process.
        for sig, handler in previous.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass

    ok, tail = suite()
    print_("\nrestored: %s  (%s)" % ("OK" if ok else "FAILED", tail))
    if not ok:
        print_("The tree did not come back green. Restore by hand before "
               "trusting anything below.")
    try:
        os.remove(_state_path())
    except OSError:
        pass
    return survivors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", nargs="?",
                        help="a python file defining MUTATIONS/SELECT")
    parser.add_argument("--dry-run", action="store_true",
                        help="check the patterns still apply, run no tests")
    parser.add_argument("--list", action="store_true",
                        help="print the mutations and their numbers")
    parser.add_argument("--only", metavar="TEXT",
                        help="run only the mutations whose name contains "
                             "TEXT -- the negative control for one test")
    parser.add_argument("-p", "--parallel", action="store_true",
                        help="run the SELECTed modules concurrently, one "
                             "process each (needs `-k <module>` throughout)")
    parser.add_argument("-t", "--timeout", type=float,
                        default=WORKER_TIMEOUT,
                        help="seconds one -p suite run may take before its "
                             "workers are killed and it counts as failed "
                             "(default: %d)" % WORKER_TIMEOUT)
    parser.add_argument("--restore", action="store_true",
                        help="undo a round that was killed, and exit")
    args = parser.parse_args(argv)

    if args.restore:
        state = read_state()
        if state is None:
            print_("Nothing to restore: no round is recorded as unfinished.")
            return 0
        if _alive(state.get("pid")):
            print_("A round is still running (pid %s). Kill it first."
                   % state.get("pid"))
            return 2
        restore_from_state(state, "--restore")
        os.remove(_state_path())
        return 0

    if not args.plan:
        parser.error("a plan is required unless --restore is given")

    mutations, select = load_plan(args.plan)
    if args.only:
        wanted = [m for m in mutations if args.only.lower() in m[0].lower()]
        if not wanted:
            print_("--only %r matches none of the %d mutations."
                   % (args.only, len(mutations)))
            return 2
        mutations = wanted

    if args.list:
        for index, (name, rel, _o, _n) in enumerate(mutations, 1):
            print_("%2d  %-58s %s" % (index, name, rel))
        return 0

    # Before check_patterns, which reads the tree: a stale mutation from a
    # killed round makes an `old` pattern fail to match, so a dry-run would
    # report a broken plan and a real run would refuse to start -- both of
    # them describing the wreckage rather than the plan. This used to sit
    # after the --dry-run early return, so the one command you reach for to
    # ask "is anything wrong" was the one that could not fix it.
    if not recover_if_needed():
        return 2

    bad = check_patterns(mutations)
    for name, rel, count in bad:
        print_("pattern %s in %s: %s"
               % ("matches nothing" if count == 0 else
                  "file is missing" if count < 0 else
                  "matches %d times" % count, rel, name))
    if bad:
        print_("\n%d pattern(s) do not identify one place. A mutation that "
               "changes nothing is indistinguishable from one that survived."
               % len(bad))
        return 2
    print_("%d mutation(s), all patterns unique." % len(mutations))
    if args.dry_run:
        return 0

    print_("journal and backups in %s\n" % STATE_DIR)
    survivors = run(mutations, select, parallel=args.parallel,
                    timeout=args.timeout)
    if survivors is None:
        return 2
    print_("survivors: %d of %d" % (len(survivors), len(mutations)))
    for name in survivors:
        print_("  -", name)
    if survivors:
        print_("\nA survivor is a claim without evidence. Usually the test is "
               "weaker than it looks rather than the code being right.")
    return 1 if survivors else 0


if __name__ == "__main__":
    code = main()
    # CPython flushes stdout again during shutdown, after main() has caught
    # everything it can, and a closed pipe (`| head`) makes that print a
    # traceback over an otherwise clean run. Point the fd at the void so the
    # final flush has somewhere to go. NOT `signal(SIGPIPE, SIG_DFL)`, the
    # other usual answer: that terminates the process on a broken pipe, which
    # mid-round would skip the restore this file exists to guarantee.
    try:
        sys.stdout.flush()
    except BrokenPipeError:
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    raise SystemExit(code)
