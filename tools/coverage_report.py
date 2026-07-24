#!/usr/bin/env python3
"""Line-coverage report for jellyfin_mpv_shim, with no third-party dependency.

The project policy is to be frugal with dependencies, and a coverage tool is
exactly the kind of thing that does not need to be one: CPython has shipped
the machinery since 3.12. This uses ``sys.monitoring`` with LINE events, which
is the same mechanism modern coverage.py uses on 3.12+ and costs a few percent
rather than the 3-5x of ``sys.settrace``.

    tools/coverage_report.py                     # unit suite
    tools/coverage_report.py --integration       # + the agnostic integration
                                                 #   modules (no mpv needed)
    tools/coverage_report.py --sort=missing      # biggest gaps first
    tools/coverage_report.py --show mpvtk_browser/views.py
                                                 # the uncovered line numbers
    tools/coverage_report.py --json out.json     # machine-readable

**What "executable lines" means here.** The denominator is every line that
appears in a compiled code object for the module, walked recursively into
nested functions, comprehensions and classes. That is what CPython can
actually emit a LINE event for, so a line missing from the numerator was
genuinely never run — no heuristics about blank lines, comments or
continuations, because none of those produce code objects.

**Known blind spot.** Only lines are counted, not branches. A ``for`` loop
that never iterates and an ``if`` whose false arm never runs both still show
their header line as covered. Treat the number as "was this code reached at
all", not "was it exercised". For the modules where that distinction matters
the report prints a partial-branch hint (see BRANCHY below).

Modules imported before monitoring starts still measure correctly: the
denominator comes from re-compiling the source, not from the live module, and
LINE events fire on already-imported code.
"""

import argparse
import io
import json
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG_DIR = os.path.join(REPO, "jellyfin_mpv_shim")

# Not ours / not Python we wrote.
SKIP_PARTS = ("/messages/", "/default_shader_pack/", "/__pycache__/")

TOOL_ID = getattr(sys.monitoring, "PROFILER_ID", 2)

# Modules whose correctness is mostly in their branches (error handling,
# backend selection, lock contention), so a high line number overstates how
# well they are tested. Flagged in the report rather than scored differently.
BRANCHY = {
    "player.py",
    "clients.py",
    "sync/manager.py",
    "mpvtk_browser/ui.py",
    "mpvtk/app.py",
}


def sources():
    """Every package source file we hold to a coverage number."""
    out = []
    for root, dirs, files in os.walk(PKG_DIR):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            rel = os.path.relpath(path, PKG_DIR)
            if any(part in "/" + rel.replace(os.sep, "/") + "/"
                   for part in SKIP_PARTS):
                continue
            out.append((rel.replace(os.sep, "/"), path))
    return out


def executable_lines(path):
    """Line numbers CPython can emit a LINE event for in this file.

    Compiles the source and walks every nested code object. co_lines() yields
    (start, end, lineno) triples; lineno is None for bytecode with no source
    line (implicit returns, cleanup), which is exactly what should not count
    against a module.
    """
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    try:
        root = compile(src, path, "exec")
    except SyntaxError:
        return set()
    seen = set()
    stack = [root]
    while stack:
        code = stack.pop()
        for _start, _end, lineno in code.co_lines():
            if lineno:
                seen.add(lineno)
        for const in code.co_consts:
            if hasattr(const, "co_lines"):
                stack.append(const)
    return seen


class Monitor:
    """Collect executed (filename, lineno) pairs for the package only."""

    def __init__(self, watched_paths):
        self.watched = watched_paths          # abspath -> rel
        self.hits = {rel: set() for rel in watched_paths.values()}

    def _on_line(self, code, lineno):
        rel = self.watched.get(code.co_filename)
        if rel is not None:
            self.hits[rel].add(lineno)
        return sys.monitoring.DISABLE if rel is None else None

    def __enter__(self):
        mon = sys.monitoring
        mon.use_tool_id(TOOL_ID, "jms-coverage")
        mon.register_callback(TOOL_ID, mon.events.LINE, self._on_line)
        mon.set_events(TOOL_ID, mon.events.LINE)
        return self

    def __exit__(self, *exc):
        mon = sys.monitoring
        mon.set_events(TOOL_ID, 0)
        mon.register_callback(TOOL_ID, mon.events.LINE, None)
        mon.free_tool_id(TOOL_ID)
        return False


# Integration modules that do not need mpv or a display, so they can be folded
# into a coverage run on any machine. The mpv-dependent legs run in
# subprocesses (see run_integration.py) and cannot be measured in-process.
AGNOSTIC_INTEGRATION = [
    "tests.integration.test_clients_concurrency",
    "tests.integration.test_sync_manager_races",
    "tests.integration.test_syncplay_generation",
]


def build_suite(with_integration, modules=None):
    loader = unittest.TestLoader()
    if modules:
        # Explicit module list: do NOT also discover, or player.py gets
        # imported by the unit suite first and the backend the caller asked
        # for via JMS_TEST_BACKEND is not the one under measurement.
        suite = unittest.TestSuite()
        for name in modules:
            suite.addTests(loader.loadTestsFromName(name))
        return suite
    suite = loader.discover(os.path.join(REPO, "tests"), top_level_dir=REPO)
    if with_integration:
        for name in AGNOSTIC_INTEGRATION:
            suite.addTests(loader.loadTestsFromName(name))
    return suite


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--integration", action="store_true",
                    help="also run the mpv-free integration modules")
    ap.add_argument("--modules", nargs="+", metavar="MOD",
                    help="measure ONLY these test modules instead of "
                         "discovering. Use with JMS_TEST_BACKEND to measure "
                         "the per-backend player legs, which run in "
                         "subprocesses under run_integration.py and are "
                         "therefore invisible to a normal run. E.g.: "
                         "JMS_TEST_BACKEND=libmpv tools/coverage_report.py "
                         "--modules tests.integration.test_player_state_machine "
                         "tests.integration.test_lifecycle")
    ap.add_argument("--sort", choices=("percent", "missing", "name"),
                    default="percent", help="report order (default: percent)")
    ap.add_argument("--show", metavar="REL_PATH",
                    help="print the uncovered line numbers for one file")
    ap.add_argument("--functions", metavar="REL_PATH",
                    help="rank one file's functions by uncovered lines. This "
                         "is the view that matters before a refactor: a file "
                         "at 60%% can still have the exact method you are "
                         "about to move sitting at zero.")
    ap.add_argument("--json", metavar="PATH", help="also write JSON")
    ap.add_argument("--min", type=float, default=None,
                    help="exit non-zero if any file is below this percent")
    ap.add_argument("--merge", nargs="+", metavar="JSON",
                    help="don't run anything; union these JSON reports "
                         "instead. This is how you get an honest total: the "
                         "per-backend legs run in separate processes, so "
                         "no single run sees all of player.py.")
    args = ap.parse_args()

    if args.merge:
        return report(merge_reports(args.merge), args)

    os.chdir(REPO)
    sys.path.insert(0, REPO)
    sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

    files = sources()
    watched = {os.path.abspath(path): rel for rel, path in files}
    totals = {rel: executable_lines(path) for rel, path in files}

    suite = build_suite(args.integration, args.modules)
    stream = io.StringIO()
    with Monitor(watched) as mon:
        result = unittest.TextTestRunner(stream=stream, verbosity=0).run(suite)

    rows = []
    for rel, _path in files:
        total = totals[rel]
        hit = mon.hits[rel] & total
        if not total:
            continue
        rows.append(_row(rel, total, hit))
    print("tests: %d run, %d failures, %d errors"
          % (result.testsRun, len(result.failures), len(result.errors)))
    return report(rows, args)


def _row(rel, total, hit):
    return {
        "file": rel,
        "total": len(total),
        "covered": len(hit),
        "missing": len(total) - len(hit),
        "percent": 100.0 * len(hit) / len(total),
        "missing_lines": sorted(total - hit),
    }


def merge_reports(paths):
    """Union the covered lines across runs.

    Needed because no single process sees everything: the fake-mpv and
    real-mpv legs each import player.py against a different backend, so
    run_integration.py gives each its own interpreter. Unioning the MISSING
    sets (rather than averaging percentages) is the only way to answer "is
    this line tested anywhere".
    """
    totals, missing = {}, {}
    for path in paths:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for row in data["files"]:
            rel = row["file"]
            totals[rel] = row["total"]
            miss = set(row["missing_lines"])
            # A line is missing overall only if EVERY run missed it.
            missing[rel] = miss if rel not in missing else (missing[rel] & miss)
    rows = []
    for rel, total in totals.items():
        miss = missing[rel]
        rows.append({
            "file": rel,
            "total": total,
            "covered": total - len(miss),
            "missing": len(miss),
            "percent": 100.0 * (total - len(miss)) / total if total else 0.0,
            "missing_lines": sorted(miss),
        })
    return rows


def report_functions(rows, rel):
    """Per-function uncovered counts for one file.

    Uses each def's full line span, so a method nested in an uncovered method
    is attributed to both. That is the right bias here: it makes an entirely
    dead region obvious rather than splitting its blame.
    """
    import ast as _ast

    row = next((r for r in rows if r["file"] == rel), None)
    if row is None:
        sys.exit("no such file in the report: %s" % rel)
    path = os.path.join(PKG_DIR, rel)
    with open(path, encoding="utf-8") as fh:
        tree = _ast.parse(fh.read(), filename=path)
    missing = set(row["missing_lines"])
    out = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        span = set(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        miss = len(span & missing)
        if miss:
            out.append((miss, len(span), node.name, node.lineno))
    out.sort(reverse=True)
    print("%s — %d/%d lines uncovered (%.1f%% covered)"
          % (rel, row["missing"], row["total"], row["percent"]))
    print("%6s %6s  %s" % ("MISS", "SPAN", "FUNCTION"))
    for miss, span, name, lineno in out:
        print("%6d %6d  %s  (line %d)" % (miss, span, name, lineno))
    if not out:
        print("  (every function is fully covered)")


def report(rows, args):
    if args.functions:
        return report_functions(rows, args.functions)
    if args.show:
        row = next((r for r in rows if r["file"] == args.show), None)
        if row is None:
            sys.exit("no such file in the report: %s" % args.show)
        print("%s — %d uncovered lines" % (row["file"], row["missing"]))
        print(_ranges(row["missing_lines"]))
        return

    key = {"percent": lambda r: (r["percent"], -r["total"]),
           "missing": lambda r: -r["missing"],
           "name": lambda r: r["file"]}[args.sort]
    rows.sort(key=key)

    grand_total = sum(r["total"] for r in rows)
    grand_cov = sum(r["covered"] for r in rows)
    print("%-46s %6s %6s %6s" % ("FILE", "LINES", "MISS", "COVER"))
    print("-" * 68)
    for r in rows:
        flag = " *" if r["file"] in BRANCHY else ""
        print("%-46s %6d %6d %5.1f%%%s"
              % (r["file"], r["total"], r["missing"], r["percent"], flag))
    print("-" * 68)
    print("%-46s %6d %6d %5.1f%%"
          % ("TOTAL", grand_total, grand_total - grand_cov,
             100.0 * grand_cov / grand_total if grand_total else 0.0))
    print("\n* line coverage overstates these: their risk is in branches "
          "(error paths, backend selection, lock contention).")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump({"files": rows,
                       "total": {"lines": grand_total, "covered": grand_cov}},
                      fh, indent=2)
        print("wrote %s" % args.json)

    if args.min is not None:
        low = [r for r in rows if r["percent"] < args.min]
        if low:
            print("\n%d file(s) below %.0f%%:" % (len(low), args.min))
            for r in low:
                print("  %-44s %5.1f%%" % (r["file"], r["percent"]))
            sys.exit(1)


def _ranges(lines):
    """Compress [1,2,3,7,9,10] to '1-3, 7, 9-10' for readable output."""
    out, start, prev = [], None, None
    for n in lines:
        if start is None:
            start = prev = n
            continue
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = n
    if start is not None:
        out.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ", ".join(out)


if __name__ == "__main__":
    main()
