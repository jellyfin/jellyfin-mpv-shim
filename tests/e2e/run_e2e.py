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

# Contract tier: never imports player.py, so the mpv backend is irrelevant and
# these run ONCE and without a display. Seconds, not minutes.
CONTRACT = [
    "tests.e2e.test_account_policy",
    "tests.e2e.test_source_conformance",
    "tests.e2e.test_live_tv",
    "tests.e2e.test_route_walk",
]

# Playback tier: a real mpv, so once per backend under xvfb.
PER_BACKEND = [
    "tests.e2e.test_playback_advance",
    "tests.e2e.test_playback_eof",
    "tests.e2e.test_playback_failure",
    "tests.e2e.test_mpv_reopen",
]

MODULES = CONTRACT + PER_BACKEND

BACKENDS = ("libmpv", "jsonipc")

SINK_NAME = "jms-e2e-sink"


def make_dummy_sink():
    """One null audio sink for the whole matrix. Returns (device, unload).

    The playback legs decode real media, so mpv opens a real output — audible
    on a developer's box, contending with whatever else is playing, and able
    to fail on a device another process holds. A null sink keeps the entire
    audio path live (device selection, format negotiation, the AudioMixin
    settings) while ending nowhere.

    Made here rather than per-leg so eleven processes share one sink instead
    of loading eleven modules under one requested name, and so a leg that dies
    does not leak its own. The **default sink is never changed** — this one is
    addressed explicitly and nothing about the developer's audio moves.
    """
    if not shutil.which("pactl"):
        return None, None
    try:
        out = subprocess.run(
            ["pactl", "load-module", "module-null-sink",
             "sink_name=" + SINK_NAME,
             "sink_properties=device.description=" + SINK_NAME],
            capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None, None
    module_id = (out.stdout or "").strip()
    if out.returncode != 0 or not module_id.isdigit():
        return None, None

    def unload():
        try:
            subprocess.run(["pactl", "unload-module", module_id],
                           capture_output=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass

    return "pulse/" + SINK_NAME, unload


def run_leg(module, backend, use_xvfb, verbosity):
    env = dict(os.environ)
    if backend:
        env["JMS_TEST_BACKEND"] = backend
    cmd = [sys.executable, "-m", "unittest", module]
    if verbosity > 1:
        cmd.append("-v")
    if use_xvfb:
        cmd = ["xvfb-run", "-a"] + cmd
    print("\n=== %s [%s] ===" % (module, backend or "contract"), flush=True)
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

    device, unload_sink = make_dummy_sink()
    if device:
        os.environ["JMS_E2E_AUDIO_DEVICE"] = device
        print("audio:  %s (null sink; your default output is untouched)"
              % device)
    else:
        print("audio:  no null sink available; mpv's own null device",
              file=sys.stderr)

    use_xvfb = not args.no_xvfb and shutil.which("xvfb-run") is not None
    backends = [args.backend] if args.backend else list(BACKENDS)
    modules = args.module or MODULES

    results = []
    # Contract modules once, with no display: they never import player.py.
    for module in [m for m in modules if m in CONTRACT]:
        ok = run_leg(module, None, False, args.verbose)
        results.append((module, "contract", ok))

    for backend in backends:
        for module in [m for m in modules if m not in CONTRACT]:
            ok = run_leg(module, backend, use_xvfb, args.verbose)
            results.append((module, backend, ok))

    print("\n" + "=" * 60)
    for module, backend, ok in results:
        print("%-8s %-45s %s" % (backend, module, "PASS" if ok else "FAIL"))
    if unload_sink:
        unload_sink()
    failed = [r for r in results if not r[2]]
    print("=" * 60)
    print("%d/%d legs passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
