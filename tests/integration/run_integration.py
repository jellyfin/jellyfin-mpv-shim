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
* Real-mpv legs are run under ``xvfb-run`` whenever it is available — not just
  when headless. Two reasons: a bare run throws ~25 real windows onto the
  developer's desktop, and a real window manager is free to ignore the
  requested geometry (a leg once came up 1272x55, which fails as "no overlays
  rendered" rather than as the window-size problem it is). Pass ``--no-xvfb``
  to watch the windows for debugging. They self-skip if mpv/ffmpeg/display are
  unavailable, so a bare machine still exits clean.

Results are reported per leg (and per backend) so an external-mpv-only failure
is unmissable.
"""

import argparse
import os
import re
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
    "tests.integration.test_mpv_lifecycle",
]

# Real mpv / real display legs -> run per backend, wrapped in xvfb when headless.
# The browser UI leg needs a display (Tk) but not a specific mpv backend; it is
# run once under xvfb (see main()).
PER_BACKEND_REAL = [
    "tests.integration.test_realmpv_smoke",
    # mpvtk browser attaches renderer.lua to a real mpv per backend.
    "tests.integration.test_mpvtk_browser",
    # playback-HUD lifecycle (mpvtk-hud) over real video per backend.
    "tests.integration.test_mpvtk_hud",
    # The PIN gate and user switching, driven through the renderer's real
    # focus/keystroke path — the half a unit test calling _do_unlock()
    # cannot cover. Replaces the Tk browser's 12 equivalents.
    "tests.integration.test_mpvtk_auth",
]

# Backend-agnostic, run once. The harness's own contract: the fake mpv
# module must not survive into sys.modules for later importers (it spawns
# its own subprocesses).
DISPLAY_ONCE = [
    "tests.integration.test_harness_isolation",
]

# The whole suite in ONE process, per backend. The legs above deliberately
# isolate the fake-mpv and real-mpv halves, which meant a module that poisoned
# the process for later ones could not be caught by them — and one did, for a
# while, costing 17 real-mpv tests that passed in isolation. This leg is the
# only one that would have failed. Keep it last: it is the slowest, and a
# failure here with every other leg green means cross-module interference.
# (tests/integration has no __init__.py on purpose, so this is a
# plain start-directory discover, not a package path.)
WHOLE_SUITE = ["discover", "tests/integration"]

BACKENDS = ("libmpv", "jsonipc")


def _have_display():
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _run(modules, *, backend=None, use_xvfb=False, extra_env=None,
         label=None):
    env = dict(os.environ)
    if backend:
        env["JMS_TEST_BACKEND"] = backend
    if extra_env:
        env.update(extra_env)
    if modules and modules[0] == "discover":
        # `-m unittest -v discover ...` is rejected: -v before the
        # subcommand selects the plain form, which has no `discover`.
        cmd = [sys.executable, "-m", "unittest", "discover", "-v", *modules[1:]]
    else:
        cmd = [sys.executable, "-m", "unittest", "-v", *modules]
    if use_xvfb:
        xvfb = shutil.which("xvfb-run")
        if xvfb:
            cmd = [xvfb, "-a", *cmd]
    label = "%s%s" % (
        label or "/".join(m.rsplit(".", 1)[-1] for m in modules),
        " [%s]" % backend if backend else "",
    )
    print("\n" + "=" * 72)
    print("RUN: %s" % label)
    print("=" * 72, flush=True)
    # Tee rather than subprocess.call: we want the live output AND the
    # counts, because "rc == 0" alone cannot tell a leg that passed from one
    # that skipped everything (see --strict).
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True)
    captured = []
    for line in proc.stdout:
        sys.stdout.write(line)
        captured.append(line)
    sys.stdout.flush()
    rc = proc.wait()
    return label, rc, _counts("".join(captured))


_RAN_RE = re.compile(r"^Ran (\d+) tests? in ", re.M)
_SKIP_RE = re.compile(r"\bskipped=(\d+)")


def _counts(output):
    """(ran, skipped) as unittest reported them, or (None, None)."""
    ran = _RAN_RE.search(output)
    if ran is None:
        return None, None
    skipped = sum(int(m.group(1)) for m in _SKIP_RE.finditer(output))
    return int(ran.group(1)), skipped


def leg_status(rc, ran, skipped):
    """(text, failed, hollow) for one leg.

    A leg where EVERY test skipped is not a pass in any useful sense: a
    container missing mpv/ffmpeg/a display printed a fully green matrix
    having asserted nothing at all. Split out from main() so it can be
    tested — see tests/test_integration_runner.py."""
    if rc != 0:
        status, failed = "FAIL (rc=%d)" % rc, 1
    else:
        status, failed = "PASS", 0
    hollow = 0
    if ran is not None:
        executed = ran - (skipped or 0)
        status += "  [%d run, %d skipped]" % (executed, skipped or 0)
        if rc == 0 and ran and executed == 0:
            hollow = 1
            status += "  <- nothing ran"
    return status, failed, hollow


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="fail if a leg skipped every one of its tests "
                         "(a container with no mpv/ffmpeg/display otherwise "
                         "prints a green matrix having asserted nothing)")
    ap.add_argument("--backend", choices=BACKENDS,
                    help="only run this backend's legs (default: both)")
    ap.add_argument("--list", action="store_true",
                    help="list the legs that would run and exit")
    ap.add_argument("--no-real", action="store_true",
                    help="skip the real-mpv smoke legs (Tier 1 only)")
    ap.add_argument("--no-xvfb", action="store_true",
                    help="use the real display instead of xvfb, to watch the "
                         "windows (expect ~25 of them, and a window manager "
                         "that may not honour the requested geometry)")
    args = ap.parse_args()

    backends = (args.backend,) if args.backend else BACKENDS

    if args.list:
        print("Agnostic (once):")
        for m in AGNOSTIC:
            print("  ", m)
        print("Display, once (xvfb when headless):")
        for m in DISPLAY_ONCE:
            print("  ", m)
        if not args.no_real:
            print("Whole suite in one process, per backend (last)")
        for b in backends:
            print("Backend %s:" % b)
            for m in PER_BACKEND_FAKE + ([] if args.no_real else PER_BACKEND_REAL):
                print("  ", m)
        return 0

    results = []
    # Prefer xvfb whenever we have it: isolated from the developer's desktop
    # and from a window manager with opinions about geometry.
    xvfb = shutil.which("xvfb-run") is not None and not args.no_xvfb
    if not xvfb and not _have_display():
        xvfb = True              # no display at all: xvfb or bust

    # 1) Backend-agnostic concurrency tests, once.
    results.append(_run(AGNOSTIC))

    # 2) Backend-agnostic Tk browser UI, once (needs a display; xvfb when headless).
    results.append(_run(DISPLAY_ONCE, use_xvfb=xvfb))

    # 3) Per-backend legs.
    for backend in backends:
        # Fake-mpv state machine / keyboard / lifecycle (no display).
        results.append(_run(PER_BACKEND_FAKE, backend=backend))
        # Real-mpv smoke (needs a display; xvfb when headless).
        if not args.no_real:
            results.append(_run(PER_BACKEND_REAL, backend=backend,
                                use_xvfb=xvfb))

    # 4) Everything at once, per backend — catches cross-module interference
    #    that the isolated legs above are blind to by construction.
    if not args.no_real:
        for backend in backends:
            results.append(_run(WHOLE_SUITE, backend=backend,
                                use_xvfb=xvfb, label="whole suite"))

    print("\n" + "=" * 72)
    print("INTEGRATION MATRIX SUMMARY")
    print("=" * 72)
    failed = 0
    hollow = 0
    for label, rc, (ran, skipped) in results:
        status, is_failed, is_hollow = leg_status(rc, ran, skipped)
        failed += is_failed
        hollow += is_hollow
        print("  %-52s %s" % (label, status))
    print("=" * 72)
    if failed:
        print("%d leg(s) FAILED." % failed)
        return 1
    if hollow and args.strict:
        print("%d leg(s) skipped EVERY test (--strict)." % hollow)
        return 1
    if hollow:
        print("%d leg(s) skipped every test; they assert nothing here. "
              "Use --strict to treat that as failure." % hollow)
    print("All legs passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
