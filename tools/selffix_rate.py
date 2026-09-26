#!/usr/bin/env python3
"""How much of a release window was spent repairing that same window.

`docs/POSTMORTEM_3.0.0.md` §1 measured 3.0.0 at roughly 30-45% by four
instruments, and §5.8 turned that into the one process rule with a number
on it:

    A release should not tag while the trailing-3-day self-fix rate is
    above the window's opening rate.

On 2026-09-02 that rate went 20% -> 79% and never came back down. The tag
went out five days later. CLAUDE.md states the trigger in words --
*"when a round's findings land mostly in code that earlier rounds fixed,
stop"* -- and words did not fire it, which is this file's warrant.

## What it counts

SZZ-lite. For each non-merge commit in the window: take its hunks that
DELETE or MODIFY a line of a `.py` or `.lua` file outside `docs/`, blame
those lines at the parent, and call the commit a **self-fix** if any blamed
commit is itself inside the window.

Four instruments, deliberately, because they answer in different units and
the postmortem's standing rule is **never quote one figure as "the" rate**:

    A   any in-range blame, over commits that modify existing code
    A'  the same, over every commit in the window
    B   majority in-range (more than half the blamed lines)
    C   shipping code only (`jellyfin_mpv_shim/**`)

## What it cannot see, and why the number is a floor

`git blame` only sees a fix that deletes or edits the line it repairs. The
normal shape of a correctness fix here is *adding a guard*, and blame is
blind to that -- 22 of 3.0.0's 187 commits deleted nothing at all, and 16 of
those added to a file the window had already touched. `--additions` lists
them so the invisible zone is at least visible.

Prose-only commits are excluded when they can be proved so: a commit whose
`.py` files are AST-identical, docstrings stripped, cannot carry a defect.
`--no-prose-filter` turns that off.

**Reconciling with §1's table, because the figures differ and only one
reason matters.** The numerators are identical -- 56 self-fixes by A, 40 by
B, over `v3.0.0pre14..v3.0.0` -- and so is the daily table, 20% on
2026-09-01 and 78.6% on 2026-09-02. The denominators differ because §1 kept
the prose commits in them and excluded them only from the numerator; this
drops them from both, which is the answer to "what share of the real
opportunities were self-fixes". §1 reads 44.1% / 29.9% where this reads
46.3% / 30.9%, and the count of excluded commits is printed so the two can
always be reconciled.

That count is **6**, not §1's four: `f1127df4` and `775d8d79` are the same
prose-compression batch on the same day and were missed by the hand count.
Which is the argument for the filter being code.

## The 0.0% trap, which is why this is a file and not a shell pipeline

The postmortem's first run reported **0.0%** and looked entirely credible.
`git blame --no-color` is an *ambiguous option* -- git offers
`--no-color-lines` and `--no-color-by-age` and refuses to guess -- so every
blame call errored to empty and the script reported a clean release. This
one treats "no blame output anywhere" as a failure rather than as an answer,
and says so. It is the repo's own "gate on the value, not the marker" rule
applied to the instrument that measures the repo.

Usage:
    tools/selffix_rate.py --since v3.0.0pre14 --until v3.0.0
    tools/selffix_rate.py --since origin/master        # an open branch
    tools/selffix_rate.py --since v3.0.0pre14 --daily  # the gate's table

Exit 1 if the gate is failing (trailing-3-day rate above the opening rate),
or if the measurement could not be taken. Read the daily table before
believing either.
"""

import argparse
import ast
import collections
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CODE = (".py", ".lua")
SHIPPING = "jellyfin_mpv_shim/"

#: `@@ -12,3 +12,4 @@` -- the OLD side is what has lines to blame.
_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+\d+(?:,\d+)? @@")
_FILE = re.compile(r"^\+\+\+ b/(.*)$")
#: `git blame -l` starts each line with the 40-char sha.
_BLAME = re.compile(r"^\^?([0-9a-f]{7,40})\s")


def git(*args):
    out = subprocess.run(("git",) + args, cwd=ROOT, capture_output=True,
                         text=True)
    return out.stdout


def window(since, until):
    """Non-merge, non-Weblate commits in (since, until], oldest first."""
    raw = git("log", "--no-merges", "--reverse",
              "--format=%H\t%ad\t%s", "--date=short",
              "%s..%s" % (since, until))
    out = []
    for line in raw.splitlines():
        sha, date, subject = line.split("\t", 2)
        if subject.startswith("Translated using Weblate"):
            continue
        out.append((sha, date, subject))
    return out


def _touched(sha):
    """{path: [(start, count), ...]} for hunks that delete or modify code."""
    diff = git("diff", "--unified=0", "%s^" % sha, sha, "--", "*.py", "*.lua")
    hunks, path = collections.defaultdict(list), None
    for line in diff.splitlines():
        hit = _FILE.match(line)
        if hit:
            path = hit.group(1)
            continue
        hit = _HUNK.match(line)
        if hit and path and not path.startswith("docs/"):
            start = int(hit.group(1))
            count = int(hit.group(2) or 1)
            if count:          # count 0 is a pure insertion: nothing to blame
                hunks[path].append((start, count))
    return hunks


def _strip_docstrings(tree):
    """Drop every docstring node.

    **A docstring IS an AST node**, so a bare `ast.dump` comparison calls a
    docstring-only commit a behaviour change -- which is what the first
    version of this file did, and it disagreed with the postmortem by
    exactly the four commits the postmortem had proved prose-only by hand.
    Comments never reach the AST and need no handling.
    """
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body.pop(0)
    return tree


def _is_prose_only(sha):
    """True when every changed `.py` is AST-identical bar its docstrings.

    A commit that cannot change behaviour cannot be a defect. The postmortem
    excluded four of them by hand and this finds six. `.lua` has no AST
    here, so a commit touching Lua is never called prose-only -- which is
    conservative in the right direction.
    """
    files = [f for f in git("diff", "--name-only", "%s^" % sha, sha,
                            "--", "*.py", "*.lua").split()]
    if not files or any(f.endswith(".lua") for f in files):
        return False
    for path in files:
        before = git("show", "%s^:%s" % (sha, path))
        after = git("show", "%s:%s" % (sha, path))
        if not before or not after:
            return False
        try:
            a = ast.dump(_strip_docstrings(ast.parse(before)))
            b = ast.dump(_strip_docstrings(ast.parse(after)))
        except SyntaxError:
            return False
        if a != b:
            return False
    return True


def classify(commits, verbose=False):
    """[(sha, date, subject, in_range, total, shipping_only)] per commit."""
    shas = {c[0] for c in commits}
    short = {s[:12] for s in shas}
    rows, blame_lines = [], 0
    for sha, date, subject in commits:
        hunks = _touched(sha)
        hits = total = 0
        ship_hits = 0
        for path, spans in hunks.items():
            for start, count in spans:
                out = git("blame", "-l", "-L", "%d,+%d" % (start, count),
                          "%s^" % sha, "--", path)
                for line in out.splitlines():
                    hit = _BLAME.match(line)
                    if not hit:
                        continue
                    blame_lines += 1
                    total += 1
                    blamed = hit.group(1)
                    if blamed in shas or blamed[:12] in short:
                        hits += 1
                        if path.startswith(SHIPPING):
                            ship_hits += 1
        rows.append((sha, date, subject, hits, total, ship_hits,
                     bool(hunks)))
        if verbose:
            print("  %s %s %-3d/%-3d %s"
                  % (sha[:9], date, hits, total, subject[:56]))
    if blame_lines == 0 and commits:
        raise SystemExit(
            "blame produced no lines at all across %d commits. That is the "
            "0.0%% trap from docs/POSTMORTEM_3.0.0.md section 1, not a clean "
            "release -- check the git invocation before believing any "
            "number here." % len(commits))
    return rows


def rates(rows):
    modifying = [r for r in rows if r[6]]
    a_hits = [r for r in modifying if r[3]]
    b_hits = [r for r in modifying if r[4] and r[3] * 2 > r[4]]
    c_all = [r for r in modifying if r[5] or r[3]]
    c_hits = [r for r in modifying if r[5]]

    def pct(n, d):
        return (100.0 * n / d) if d else 0.0
    return {
        "A": (len(a_hits), len(modifying), pct(len(a_hits), len(modifying))),
        "A'": (len(a_hits), len(rows), pct(len(a_hits), len(rows))),
        "B": (len(b_hits), len(modifying), pct(len(b_hits), len(modifying))),
        "C": (len(c_hits), len(c_all), pct(len(c_hits), len(c_all))),
    }


def daily(rows):
    """[(date, self_fixes, modifying, rate)] oldest first."""
    per = collections.OrderedDict()
    for _sha, date, _subj, hits, _total, _ship, modifies in rows:
        if not modifies:
            continue
        got, seen = per.get(date, (0, 0))
        per[date] = (got + (1 if hits else 0), seen + 1)
    return [(d, n, m, (100.0 * n / m) if m else 0.0)
            for d, (n, m) in per.items()]


def gate(table, window_days=3):
    """(ok, opening, trailing) -- section 5.8's rule.

    `opening` is the first `window_days` days of the release window and
    `trailing` the last. Both are rates over commits, not over days, so a
    quiet day cannot swing it.
    """
    if len(table) < window_days * 2:
        return None, None, None

    def rate(chunk):
        n = sum(c[1] for c in chunk)
        m = sum(c[2] for c in chunk)
        return (100.0 * n / m) if m else 0.0
    opening = rate(table[:window_days])
    trailing = rate(table[-window_days:])
    return trailing <= opening, opening, trailing


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--since", required=True, help="ref the window opens at")
    ap.add_argument("--until", default="HEAD")
    ap.add_argument("--daily", action="store_true", help="per-day table")
    ap.add_argument("--additions", action="store_true",
                    help="list the pure-addition commits blame cannot see")
    ap.add_argument("--no-prose-filter", action="store_true")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    commits = window(args.since, args.until)
    if not commits:
        raise SystemExit("no commits in %s..%s" % (args.since, args.until))
    if not args.no_prose_filter:
        keep = [c for c in commits if not _is_prose_only(c[0])]
        if len(keep) != len(commits):
            print("excluded %d prose-only commit(s) -- AST-identical with "
                  "docstrings stripped.\nThey leave the denominator too, "
                  "which is where these figures part company with\n"
                  "docs/POSTMORTEM_3.0.0.md section 1. Same numerators.\n"
                  % (len(commits) - len(keep)))
        commits = keep

    print("%s..%s — %d commits\n" % (args.since, args.until, len(commits)))
    rows = classify(commits, verbose=args.verbose)
    if args.verbose:
        print()

    for name, (n, d, pc) in sorted(rates(rows).items()):
        print("  %-3s %3d / %-3d  %5.1f%%" % (name, n, d, pc))
    print("\nNever quote one of these as \"the\" rate: they are four "
          "instruments in\nthree units, and the variance is the finding "
          "(docs/POSTMORTEM_3.0.0.md section 1).")

    table = daily(rows)
    if args.daily:
        print("\n  date        self-fix / modifying   rate")
        for date, n, m, pc in table:
            print("  %s  %3d / %-3d            %5.1f%%" % (date, n, m, pc))

    if args.additions:
        print("\nPure additions — blame cannot see a fix that only adds a "
              "guard:")
        for sha, date, subject, _h, _t, _s, modifies in rows:
            if not modifies:
                print("  %s %s %s" % (sha[:9], date, subject[:64]))

    ok, opening, trailing = gate(table)
    print()
    if ok is None:
        print("gate: not enough days to compare (need 6, have %d)."
              % len(table))
        return 0
    print("gate: opening 3 days %.1f%%, trailing 3 days %.1f%% — %s"
          % (opening, trailing, "OK" if ok else "DO NOT TAG"))
    if not ok:
        print("      The window is spending more of its commits repairing "
              "itself than\n      when it opened. docs/POSTMORTEM_3.0.0.md "
              "section 5.8; CLAUDE.md's\n      'Applying Review Findings' is "
              "the same rule without the number.")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
