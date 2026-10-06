#!/usr/bin/env python3
"""Where an e2e run's time went, from `run_e2e.py --durations FILE`.

    python3 tools/e2e_durations.py FILE [--top N] [--backend B] [--module M]

Prints, per leg, the slowest tests and the time between tests (class and
module setup, which no test owns), then the real-app harness's step tally
summed over the run (`tests/e2e/_app.py` `step`; times are inclusive, so
nested steps overlap).
"""

import argparse
import collections
import json
import sys


def load(path):
    with open(path, encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("file")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--backend")
    ap.add_argument("--module")
    args = ap.parse_args()

    recs = [r for r in load(args.file)
            if (not args.backend or r.get("backend") == args.backend)
            and (not args.module or args.module in r.get("module", ""))]
    legs = collections.OrderedDict()
    for r in recs:
        legs.setdefault((r["module"], r["backend"]), []).append(r)

    tests, per_leg = [], []
    for (module, backend), rs in legs.items():
        rs = [r for r in rs if r.get("t0") and r.get("t1")]
        if not rs:
            continue
        rs.sort(key=lambda r: r["t0"])
        busy = sum(r["t1"] - r["t0"] for r in rs)
        span = rs[-1]["t1"] - rs[0]["t0"]
        per_leg.append((span, busy, module, backend, len(rs)))
        for r in rs:
            tests.append((r["t1"] - r["t0"], backend, r["id"], r["outcome"]))

    print("== legs (first test start to last test end; between = setup) ==")
    for span, busy, module, backend, n in sorted(per_leg, reverse=True):
        print("%7.0fs  between %6.0fs  %-8s %-42s %3d tests"
              % (span, span - busy, backend, module.split(".")[-1], n))
    print("   total %.0fs" % sum(p[0] for p in per_leg))

    print("\n== slowest %d tests ==" % args.top)
    for secs, backend, tid, outcome in sorted(tests, reverse=True)[:args.top]:
        print("%7.1fs  %-8s %s%s" % (secs, backend, tid.replace("tests.e2e.", ""),
                                     "" if outcome == "pass" else "  [%s]" % outcome))

    steps = collections.defaultdict(lambda: [0, 0.0, 0])
    for r in recs:
        for name, (calls, secs, keys) in (r.get("steps") or {}).items():
            s = steps[name]
            s[0] += calls
            s[1] += secs
            s[2] += keys
    if steps:
        print("\n== harness steps, whole run (inclusive) ==")
        print("%-28s %7s %9s %7s" % ("step", "calls", "seconds", "keys"))
        for name, (calls, secs, keys) in sorted(steps.items(),
                                                key=lambda kv: -kv[1][1]):
            print("%-28s %7d %9.0f %7d" % (name, calls, secs, keys))
    return 0


if __name__ == "__main__":
    sys.exit(main())
