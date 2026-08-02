#!/usr/bin/env python3
"""Runner for the end-to-end suite (real Jellyfin server + real mpv).

Separate from both `python3 -m unittest discover tests` and the integration
runner: `tests/e2e/` has no `__init__.py`, so the fast suite never recurses
into it and never needs a server.

    ./stdjflib.py serve ~/Desktop/std-jf-lib --live-tv     # in another shell

    JMS_E2E_SERVER=http://127.0.0.1:8096 python3 tests/e2e/run_e2e.py
    JMS_E2E_SERVER=... python3 tests/e2e/run_e2e.py --backend libmpv
    python3 tests/e2e/run_e2e.py --list

Every module runs once per mpv backend, in a fresh interpreter with
`JMS_TEST_BACKEND` set — player.py picks its backend at import time and wires
interdependent module-level singletons, so a subprocess is the only clean way
to get a pristine import per backend. External mpv is the least-tested path in
the app and one of the two largest open-bug clusters, which is exactly why it
is not optional here.

Legs run under `xvfb-run` when it is available, not merely when headless: a
bare run throws real video windows onto the developer's desktop and steals
their clicks. `--no-xvfb` to watch them.

With `JMS_E2E_SERVER` unset the tests skip themselves and this exits 0 — the
suite is not a reason for a machine without a server to fail.
"""

import argparse
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

MODULES = [
    "tests.e2e.test_playback_advance",
    "tests.e2e.test_playback_eof",
    "tests.e2e.test_playback_failure",
    "tests.e2e.test_mpv_reopen",
]

BACKENDS = ("libmpv", "jsonipc")


def run_leg(module, backend, use_xvfb, verbosity):
    env = dict(os.environ)
    env["JMS_TEST_BACKEND"] = backend
    cmd = [sys.executable, "-m", "unittest", module]
    if verbosity > 1:
        cmd.append("-v")
    if use_xvfb:
        cmd = ["xvfb-run", "-a"] + cmd
    print("\n=== %s [%s] ===" % (module, backend), flush=True)
    proc = subprocess.run(cmd, cwd=REPO_ROOT, env=env)
    return proc.returncode == 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", choices=BACKENDS,
                        help="run one backend instead of the matrix")
    parser.add_argument("--module", action="append",
                        help="run only this module (repeatable)")
    parser.add_argument("--no-xvfb", action="store_true",
                        help="show the real mpv windows")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("-v", "--verbose", action="count", default=1)
    args = parser.parse_args()

    if args.list:
        for module in MODULES:
            print(module)
        return 0

    server = os.environ.get("JMS_E2E_SERVER")
    if not server:
        print("JMS_E2E_SERVER is not set — every test will skip.\n"
              "Start one with:  ./stdjflib.py serve ~/Desktop/std-jf-lib "
              "--live-tv\nthen re-run with "
              "JMS_E2E_SERVER=http://127.0.0.1:8096", file=sys.stderr)
    else:
        print("server: %s" % server)

    use_xvfb = not args.no_xvfb and shutil.which("xvfb-run") is not None
    backends = [args.backend] if args.backend else list(BACKENDS)
    modules = args.module or MODULES

    results = []
    for backend in backends:
        for module in modules:
            ok = run_leg(module, backend, use_xvfb, args.verbose)
            results.append((module, backend, ok))

    print("\n" + "=" * 60)
    for module, backend, ok in results:
        print("%-6s %-45s %s" % (backend, module, "PASS" if ok else "FAIL"))
    failed = [r for r in results if not r[2]]
    print("=" * 60)
    print("%d/%d legs passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
