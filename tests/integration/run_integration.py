#!/usr/bin/env python3
"""Runner for the heavy integration / concurrency suite.

This is intentionally *separate* from ``python3 -m unittest discover tests`` so
the fast suite stays fast and mpv-free (tests/integration has no ``__init__.py``,
so discovery skips it). Run this explicitly:

    python3 tests/integration/run_integration.py            # full matrix
    python3 tests/integration/run_integration.py --backend libmpv
    python3 tests/integration/run_integration.py --list

What it does:

* Runs the backend-agnostic concurrency modules once (they never import
  player.py, so the mpv backend is irrelevant to them).
* Runs the mpv-dependent modules once *per backend* (libmpv, jsonipc). Each leg
  is a fresh subprocess with ``JMS_TEST_BACKEND`` set, because player.py selects
  its backend at import time and wires interdependent module-level singletons —
  a subprocess is the clean way to get a pristine import per backend (reloading
  is fragile). The fake-mpv state-machine tests and the real-mpv smoke run in
  *separate* processes even within one backend, since one imports player against
  a fake and the other against the real backend.
* Real-mpv legs are run under ``xvfb-run`` when no display is present. They
  self-skip if mpv/ffmpeg/display are unavailable, so a bare machine still
  exits clean.

Results are reported per leg (and per backend) so an external-mpv-only failure
is unmissable.
"""

import argparse
import os
import shutil
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

# Modules that never import player.py -> backend-agnostic, run once.
AGNOSTIC = [
    "tests.integration.test_clients_concurrency",
    "tests.integration.test_sync_manager_races",
    "tests.integration.test_syncplay_generation",
    "tests.integration.test_single_instance_multiproc",
]

# Fake-mpv legs -> import player.py, so run per backend (a fresh interpreter with
# the matching fake backend). Keyboard + lifecycle are backend-agnostic in intent
# but import player.py (bindings live on the real singleton; action_thread /
# timeline import playerManager), so they belong here rather than in AGNOSTIC;
# running them under both backends is a free extra check that passes identically.
PER_BACKEND_FAKE = [
    "tests.integration.test_player_state_machine",
    "tests.integration.test_keyboard_controls",
    "tests.integration.test_lifecycle",
]

# Real mpv / real display legs -> run per backend, wrapped in xvfb when headless.
# The browser UI leg needs a display (Tk) but not a specific mpv backend; it is
# run once under xvfb (see main()).
PER_BACKEND_REAL = [
    "tests.integration.test_realmpv_smoke",
]

# Tk browser UI under a display -> backend-agnostic (never imports player.py),
# run once, wrapped in xvfb when headless.
DISPLAY_ONCE = [
    "tests.integration.test_browser_ui",
]

BACKENDS = ("libmpv", "jsonipc")


def _have_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run(modules, *, backend=None, use_xvfb=False, extra_env=None):
    env = dict(os.environ)
    if backend:
        env["JMS_TEST_BACKEND"] = backend
    if extra_env:
        env.update(extra_env)
    cmd = [sys.executable, "-m", "unittest", "-v", *modules]
    if use_xvfb:
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            cmd = [xvfb, "-a", *cmd]
    label = "%s%s" % (
        "/".join(m.rsplit(".", 1)[-1] for m in modules),
        " [%s]" % backend if backend else "",
    )
    print("\n" + "=" * 72)
    print("RUN: %s" % label)
    print("=" * 72, flush=True)
    rc = subprocess.call(cmd, cwd=REPO_ROOT, env=env)
    return label, rc


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--backend", choices=BACKENDS,
                    help="only run this backend's legs (default: both)")
    ap.add_argument("--list", action="store_true",
                    help="list the legs that would run and exit")
    ap.add_argument("--no-real", action="store_true",
                    help="skip the real-mpv smoke legs (Tier 1 only)")
    args = ap.parse_args()

    backends = (args.backend,) if args.backend else BACKENDS

    if args.list:
        print("Agnostic (once):")
        for m in AGNOSTIC:
            print("  ", m)
        print("Display, once (xvfb when headless):")
        for m in DISPLAY_ONCE:
            print("  ", m)
        for b in backends:
            print("Backend %s:" % b)
            for m in PER_BACKEND_FAKE + ([] if args.no_real else PER_BACKEND_REAL):
                print("  ", m)
        return 0

    results = []
    headless = not _have_display()

    # 1) Backend-agnostic concurrency tests, once.
    results.append(_run(AGNOSTIC))

    # 2) Backend-agnostic Tk browser UI, once (needs a display; xvfb when headless).
    results.append(_run(DISPLAY_ONCE, use_xvfb=headless))

    # 3) Per-backend legs.
    for backend in backends:
        # Fake-mpv state machine / keyboard / lifecycle (no display).
        results.append(_run(PER_BACKEND_FAKE, backend=backend))
        # Real-mpv smoke (needs a display; xvfb when headless).
        if not args.no_real:
            results.append(_run(PER_BACKEND_REAL, backend=backend,
                                use_xvfb=headless))

    print("\n" + "=" * 72)
    print("INTEGRATION MATRIX SUMMARY")
    print("=" * 72)
    failed = 0
    for label, rc in results:
        status = "PASS" if rc == 0 else "FAIL (rc=%d)" % rc
        if rc != 0:
            failed += 1
        print("  %-52s %s" % (label, status))
    print("=" * 72)
    if failed:
        print("%d leg(s) FAILED." % failed)
        return 1
    print("All legs passed (skips count as pass).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
