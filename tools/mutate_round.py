#!/usr/bin/env python3
"""Run a round's mutations against the finished tree and report survivors.

    xvfb-run -a python3 tools/mutate_round.py tools/mutation_plans/<plan>.py
    xvfb-run -a python3 tools/mutate_round.py <plan> --dry-run

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
"""

import argparse
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


def _run_suite(select):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    proc = subprocess.run(
        [sys.executable, "-m", "unittest", "discover", "tests"] + list(select),
        cwd=ROOT, capture_output=True, text=True, env=env)
    tail = (proc.stderr.strip().splitlines() or ["(no output)"])[-1]
    return proc.returncode == 0, tail


def run(mutations, select, backup_dir):
    files = sorted({rel for _n, rel, _o, _w in mutations})
    for rel in files:
        shutil.copyfile(os.path.join(ROOT, rel),
                        os.path.join(backup_dir, rel.replace(os.sep, "_")))

    def restore(rel):
        shutil.copyfile(os.path.join(backup_dir, rel.replace(os.sep, "_")),
                        os.path.join(ROOT, rel))

    ok, tail = _run_suite(select)
    print("baseline: %s  (%s)" % ("OK" if ok else "FAILED", tail))
    if not ok:
        print("\nA red baseline makes every mutation look killed. Stopping.")
        return None

    survivors = []
    for name, rel, old, new in mutations:
        full = os.path.join(ROOT, rel)
        with open(full, encoding="utf-8") as fh:
            source = fh.read()
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(source.replace(old, new))
        try:
            ok, tail = _run_suite(select)
        finally:
            restore(rel)
        print("%-8s %-58s %s" % ("SURVIVED" if ok else "killed", name, tail))
        if ok:
            survivors.append(name)

    for rel in files:
        restore(rel)
    ok, tail = _run_suite(select)
    print("\nrestored: %s  (%s)" % ("OK" if ok else "FAILED", tail))
    if not ok:
        print("The tree did not come back green. Restore by hand before "
              "trusting anything below.")
    return survivors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("plan", help="a python file defining MUTATIONS/SELECT")
    parser.add_argument("--dry-run", action="store_true",
                        help="check the patterns still apply, run no tests")
    args = parser.parse_args(argv)

    mutations, select = load_plan(args.plan)
    bad = check_patterns(mutations)
    for name, rel, count in bad:
        print("pattern %s in %s: %s"
              % ("matches nothing" if count == 0 else
                 "file is missing" if count < 0 else
                 "matches %d times" % count, rel, name))
    if bad:
        print("\n%d pattern(s) do not identify one place. A mutation that "
              "changes nothing is indistinguishable from one that survived."
              % len(bad))
        return 2
    print("%d mutation(s), all patterns unique." % len(mutations))
    if args.dry_run:
        return 0

    backup_dir = tempfile.mkdtemp(prefix="mutate-round-")
    print("backups in %s\n" % backup_dir)
    survivors = run(mutations, select, backup_dir)
    if survivors is None:
        return 2
    print("survivors: %d of %d" % (len(survivors), len(mutations)))
    for name in survivors:
        print("  -", name)
    if survivors:
        print("\nA survivor is a claim without evidence. Usually the test is "
              "weaker than it looks rather than the code being right.")
    return 1 if survivors else 0


if __name__ == "__main__":
    raise SystemExit(main())
