#!/usr/bin/env python3
"""Find every construct whose job is to **narrow**, and ask whether it can.

    python3 tools/audit_vacuous_narrowing.py                  # list the sites
    python3 tools/audit_vacuous_narrowing.py --plan out.py    # emit a plan
    xvfb-run -a python3 tools/mutate_round.py out.py          # then run it

A narrowing that cannot narrow is invisible to every other instrument this
repo has. It is not a path left uncovered -- the path runs, the query is
issued, the guard is evaluated, and the answer is simply the same one you
would get without it. Tests pass, coverage is full, review reads the line and
agrees with it, and the filter has been off since the day it was written.

Three defects of exactly this shape shipped or nearly shipped here:

* a downloaded-playlist badge scoped on `playlists.server_id`, whose only
  writer read the server identity out of the apiclient's config under a key
  that client never assigns (docs/do-not-fix.md 1 has the mechanism). The
  column was NULL on every row ever written, so the scope narrowed nothing.
  **Deleting it outright left 6,271 tests green.**
* `tools/audit_row_fixtures.ACCEPTED`, whose keys could never match, because
  the audit keyed on whatever root the caller passed and its only caller
  passes an absolute one. Unnoticed because the dict was empty.
* `RESERVED_STORE_DIRS` missing `playlist` for the life of the feature, so
  every launch deleted the playlist poster cache.

**The uniform question, and why one mutation answers it for every shape:**
force the narrowing not to happen, and see whether anything notices. For a
query scope that means the unscoped answer; for a membership gate it means
the gate never fires. A survivor is a narrowing no test can distinguish from
its own absence -- which is either a vacuous filter or an untested one, and
you cannot tell which from here. That is the point: both need a human.

**This finds candidates, it does not judge them.** Some narrowings are
genuinely belt-and-braces over a condition another layer guarantees, and a
survivor there is a decision rather than a defect. Write it down where the
next reader will look, not here.

Deliberately narrow in what it recognises -- two shapes, both taken from
defects that actually happened -- because a lint that flags every `if` is a
lint nobody runs. Extending it is cheap; the cost of noise is that the
output stops being read.
"""
import argparse
import ast
import os
import sys

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PACKAGE = "jellyfin_mpv_shim"

#: Sites deliberately left out of the sweep, with the reason. A bare entry is
#: not acceptable: say why the narrowing cannot be vacuous, or why a survivor
#: there would be a decision rather than a defect.
ACCEPTED = {}

#: Fragments that mark a string as a **SQL narrowing** rather than any other
#: string being built. Leading space matters: these are clauses appended to a
#: query already under construction, which is the shape that hid the badge
#: scope. `WHERE` is not here -- a WHERE being built unconditionally is the
#: query itself, not a narrowing of it.
_SQL_NARROWERS = (" AND ", " AND(", "AND (")


def _is_sql_scope(node, params):
    """Does this `if` narrow a query **on a value the caller supplied**?

    Both halves are required, and the second was missing in the first version
    of this file with a consequence worth keeping: `if clauses:` guarding
    `sql += " WHERE " + " AND ".join(clauses)` matched, because its body does
    contain `" AND "`. But that body *assembles* the WHERE rather than adding a
    filter to a finished query, and disabling it drops the clauses while still
    passing their params -- so the mutation produced a binding-count error, not
    an unscoped query. A crash is not a vacuity result, and in a runner without
    per-test timeouts it consumed the whole budget.

    The discriminator is where the test's value comes from. A **scope** is
    something the caller passed (`server_id`); an accumulator (`clauses`) is
    the function's own bookkeeping, and a guard on it is structural. So the
    test must name at least one of this function's parameters.

    This is narrower than "looks like SQL" on purpose. It will miss a scope
    built from a value derived from a parameter rather than the parameter
    itself; that is a false negative, which costs a missed check, where the
    false positive cost a whole run.
    """
    if not (params & _names_in(node.test)):
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            if any(frag in child.value for frag in _SQL_NARROWERS):
                return True
    return False


def _names_in(node):
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _enclosing_params(tree):
    """(lineno range -> parameter names) for every function in the module."""
    spans = []
    for fn in ast.walk(tree):
        if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = fn.args
            names = {x.arg for x in
                     list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs)}
            if a.vararg:
                names.add(a.vararg.arg)
            if a.kwarg:
                names.add(a.kwarg.arg)
            spans.append((fn.lineno, fn.end_lineno or fn.lineno, names))
    # Innermost wins, so sort by span width ascending at lookup time.
    spans.sort(key=lambda s: s[1] - s[0])
    return spans


def _params_at(spans, lineno):
    for start, end, names in spans:
        if start <= lineno <= end:
            return names
    return set()


def _membership_names(node):
    """Names this `if` tests membership *against*, e.g. `x in ACCEPTED`.

    Only upper-case names: a module-level constant collection is the shape
    that goes stale (a value is added to the table and nobody adds it to the
    set). Membership against a local is ordinary control flow.
    """
    names = []
    for child in ast.walk(node.test):
        if isinstance(child, ast.Compare):
            for op, cmp in zip(child.ops, child.comparators):
                if not isinstance(op, (ast.In, ast.NotIn)):
                    continue
                target = cmp
                if isinstance(target, ast.Attribute):
                    target = target.value
                if isinstance(target, ast.Name) and target.id.isupper():
                    names.append(target.id)
    return names


def _test_source(lines, node, text):
    """The exact `if <test>:` text, and the replacement that disables it.

    Rebuilt from the source rather than unparsed, because `mutate_round`
    matches literal text and an unparsed condition would not appear in the
    file. A test spanning lines is taken whole.

    **The body is carried along when the guard alone is not unique**, and it
    usually is not: `if server_id:` appears three times in `sync/db.py`, and
    one of them is the badge scope that this whole audit exists because of. A
    guard is written in the language's most common idiom almost by definition,
    so keying on it alone would silently drop exactly the interesting sites --
    which is the same failure as the defect being hunted. The body is never
    rewritten, only carried, so the mutation still means one thing: the guard
    does not fire.
    """
    start = node.lineno - 1
    end = (node.test.end_lineno or node.test.lineno) - 1
    segment = lines[start:end + 1]
    if not segment:
        return None, None
    tail = segment[-1]
    colon = tail.rfind(":")
    if colon == -1:
        return None, None
    head = "".join(segment[:-1]) + tail[:colon + 1]
    indent = head[:len(head) - len(head.lstrip())]
    keyword = "elif" if head.lstrip().startswith("elif") else "if"
    disabled = "%s%s False:" % (indent, keyword)

    # Grow the pattern a statement at a time until it names one site. Capped:
    # past a few statements the pattern is brittle against any edit, and an
    # honest "unchecked" beats a mutation that silently moves.
    trailer = tail[colon + 1:]
    for stmt in [None] + list(node.body[:3]):
        if stmt is None:
            old, new = head, disabled
        else:
            stop = (stmt.end_lineno or stmt.lineno)
            body = "".join(lines[end + 1:stop])
            old = head + trailer + body
            new = disabled + trailer + body
        if text.count(old) == 1:
            return old, new
    return None, None


def _site_kind(node, params):
    if _is_sql_scope(node, params):
        return "sql-scope"
    if _membership_names(node):
        return "membership-gate"
    return None


def audit(root=None):
    """(sites, skipped) -- each site a dict ready to become a mutation."""
    root = root or os.path.join(_REPO, PACKAGE)
    sites, skipped = [], []
    for base, dirs, names in os.walk(root):
        dirs[:] = [d for d in dirs if d not in ("__pycache__", "messages")]
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, _REPO).replace(os.sep, "/")
            try:
                with open(path, encoding="utf-8") as fh:
                    text = fh.read()
                tree = ast.parse(text)
            except (OSError, SyntaxError):
                continue
            lines = text.splitlines(keepends=True)
            spans = _enclosing_params(tree)
            for node in ast.walk(tree):
                if not isinstance(node, ast.If):
                    continue
                kind = _site_kind(node, _params_at(spans, node.lineno))
                if not kind:
                    continue
                key = "%s:%d" % (rel, node.lineno)
                if key in ACCEPTED:
                    continue
                # `mutate_round` refuses a pattern that is not unique, and it
                # is right to: a mutation that matches the wrong line looks
                # exactly like one that survived. Report rather than guess.
                old, new = _test_source(lines, node, text)
                if not old:
                    skipped.append(
                        (key, "no unique literal names this site, even with "
                              "three statements of its body"))
                    continue
                sites.append({"key": key, "kind": kind, "path": rel,
                              "line": node.lineno, "old": old, "new": new,
                              "against": ",".join(_membership_names(node))})
    return sites, skipped


_PLAN_HEADER = '''"""Vacuity sweep -- generated by tools/audit_vacuous_narrowing.py.

Every mutation below disables one narrowing: a query scope stops scoping, or a
membership gate stops firing. **A survivor is a narrowing no test can tell
apart from its own absence.**

Do not read a survivor as a bug on its own. It is one of two things and this
plan cannot say which: a filter that never filtered (the defect), or a real
filter nobody tests (the gap). Both want a person. What a survivor does rule
out is "the suite covers this", which is what made the original defect of this
shape survive four review rounds.

Regenerate rather than edit -- line numbers move.
"""

SELECT = %r

MUTATIONS = [
'''


def emit_plan(sites, out, select):
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(_PLAN_HEADER % (select,))
        for s in sites:
            label = "%s %s" % (s["kind"], s["key"])
            if s["against"]:
                label += " (against %s)" % s["against"]
            fh.write("    (%r,\n     %r,\n     %r,\n     %r),\n"
                     % (label, s["path"], s["old"], s["new"]))
        fh.write("]\n")


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--plan", metavar="PATH",
                    help="write a tools/mutate_round.py plan here")
    ap.add_argument("--select", default=None,
                    help="comma-separated -k selectors for the plan; the "
                         "default runs the whole suite, which is right for a "
                         "sweep and takes ~30s per mutation")
    args = ap.parse_args()

    sites, skipped = audit()
    by_kind = {}
    for s in sites:
        by_kind.setdefault(s["kind"], []).append(s)

    for kind in sorted(by_kind):
        print("\n%s (%d):" % (kind, len(by_kind[kind])))
        for s in by_kind[kind]:
            extra = " against %s" % s["against"] if s["against"] else ""
            print("  %s%s" % (s["key"], extra))
    if skipped:
        print("\nnot mutatable (%d) -- these are NOT cleared, they are "
              "unchecked:" % len(skipped))
        for key, why in skipped:
            print("  %s: %s" % (key, why))

    print("\n%d narrowing site(s); %d unchecked; %d accepted"
          % (len(sites), len(skipped), len(ACCEPTED)))

    if args.plan:
        select = []
        for part in (args.select or "").split(","):
            if part.strip():
                select += ["-k", part.strip()]
        emit_plan(sites, args.plan, select)
        print("\nplan written to %s\n  xvfb-run -a python3 "
              "tools/mutate_round.py %s --dry-run" % (args.plan, args.plan))
    return 0


if __name__ == "__main__":
    sys.exit(main())
