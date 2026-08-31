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
import signal
import subprocess
import sys
import threading

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
    # The start of playback, which is where the two backends' observer APIs
    # actually differ: the load wait dispatches on which one the handle has.
    "tests.integration.test_playback_start",
    "tests.integration.test_keyboard_controls",
    "tests.integration.test_lifecycle",
    "tests.integration.test_mpv_lifecycle",
]

# Real mpv / real display legs -> run per backend, wrapped in xvfb when headless.
# The browser UI leg needs a display (Tk) but not a specific mpv backend; it is
# run once under xvfb (see main()).
PER_BACKEND_REAL = [
    # Before test_realmpv_smoke, which terminates the shared player in its
    # tearDownClass: these two then get a player nobody has torn down.
    # `_import_real_player` revives it either way (the whole-suite leg runs
    # them in discovery order, which puts one of them after), so this is
    # about running the cheap way round rather than about correctness.
    # The picture settings against a real mpv. Per backend because that is
    # where they diverge: libmpv writes properties through the C API,
    # python-mpv-jsonipc sends `set_property` over a socket and coerces
    # types on the way -- and a refused write is caught and logged at debug,
    # so the setting reads as applied either way.
    "tests.integration.test_realmpv_picture",
    # The Settings screen against the REAL config module and a REAL gateway.
    # The fast suite drives it through a five-setting stand-in that answers
    # everything; the dynamic parts (the audio device list, which the
    # gateway asks mpv for) only exist here.
    "tests.integration.test_settings_screen",
    "tests.integration.test_realmpv_smoke",
    # mpvtk browser attaches renderer.lua to a real mpv per backend.
    "tests.integration.test_mpvtk_browser",
    # playback-HUD lifecycle (mpvtk-hud) over real video per backend.
    "tests.integration.test_mpvtk_hud",
    # The PIN gate and user switching, driven through the renderer's real
    # focus/keystroke path — the half a unit test calling _do_unlock()
    # cannot cover. Replaces the Tk browser's 12 equivalents.
    "tests.integration.test_mpvtk_auth",
    # No mocks below the network: real testsrc media, a real catalog on
    # disk, the real offline source, real keys, and the database checked
    # afterwards.
    "tests.integration.test_e2e_offline",
    # Whether mpv will ANSWER about this window's decorations. Per backend
    # because that is where it breaks: an unreadable property makes the
    # window controls turn themselves off silently and permanently, on
    # whichever backend stopped answering.
    "tests.integration.test_window_decorations",
]

# Backend-agnostic, run once. The harness's own contract: the fake mpv
# module must not survive into sys.modules for later importers (it spawns
# its own subprocesses).
DISPLAY_ONCE = [
    "tests.integration.test_harness_isolation",
    # Two real app processes: one arms a restart and exits, the other has to
    # come back. Backend-agnostic (nothing here touches mpv's API) but it
    # needs a display, because the child builds a real player on the way up.
    # The one leg that would have caught the relaunch being dead code.
    "tests.integration.test_restart_relaunch",
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
    # os.name: Windows has a desktop and no DISPLAY. Kept in step with
    # _harness.HAVE_DISPLAY, which is where the same answer decides whether
    # the real-player legs run at all.
    return bool(
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ) or os.name == "nt"


def _run(modules, *, backend=None, use_xvfb=False, extra_env=None,
         label=None):
    env = dict(os.environ)
    # A build() that raises is swallowed in production so one bad view cannot
    # kill the UI loop -- the renderer keeps the previous frame. Under test
    # that is indistinguishable from a screen that simply did not change, so
    # a frozen browser passes: exactly how a route-key collision shipped with
    # 1886 tests green. Every leg runs strict.
    env["JMS_STRICT_BUILDS"] = "1"
    if backend:
        env["JMS_TEST_BACKEND"] = backend
    if extra_env:
        env.update(extra_env)
    if modules and modules[0] == "discover":
        # `-m unittest -v discover ...` is rejected: -v before the
        # subcommand selects the plain form, which has no `discover`.
        cmd = [sys.executable, "-u", "-m", "unittest", "discover", "-v",
               *modules[1:]]
    else:
        cmd = [sys.executable, "-u", "-m", "unittest", "-v", *modules]
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
    #
    # start_new_session: the leg leads its own process group, so anything it
    # leaks can be found and killed as a unit. Same reasoning as
    # tools/run_tests_parallel.py, which has had this from the start; this
    # runner had not, and the difference is the hang described below.
    proc = subprocess.Popen(cmd, cwd=REPO_ROOT, env=env,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True,
                            start_new_session=True)
    captured = []

    def pump():
        for line in proc.stdout:
            sys.stdout.write(line)
            # Per line, not per leg. Our stdout is block-buffered whenever it
            # is redirected, which is every `run_integration.py > log 2>&1`,
            # so flushing after the loop meant a leg's entire output landed
            # in one write at the end of it -- ~147s of silence for a
            # whole-suite leg. A log that stops growing then looks exactly
            # like a hung run, and twice it got diagnosed as one.
            sys.stdout.flush()
            captured.append(line)

    # On a thread so that the LEG'S EXIT ends the leg, never pipe EOF.
    #
    # Reading `proc.stdout` to EOF on this thread is what wedged the matrix
    # indefinitely *after a leg had already passed*: a test mpv that outlives
    # its leg is reparented to init still holding the write end of this pipe,
    # and EOF can then never arrive. The leg had printed `Ran 324 tests ...
    # OK`; only the exit was missing, and the run sat there until the mpv was
    # killed by hand. The child exiting is the real end-of-leg signal, so
    # that is what we wait for.
    reader = threading.Thread(target=pump, name="leg-output", daemon=True)
    reader.start()
    rc = proc.wait()
    reader.join(OUTPUT_DRAIN_SECS)      # let genuinely buffered output land
    if reader.is_alive():
        # The leg is over and something is still holding the pipe: a leak, by
        # definition. Kill the group to release it and let `pump` see EOF.
        print("\n*** %s: the leg exited but left a process holding its "
              "output pipe; killing the leg's process group." % label,
              flush=True)
        _kill_leg_group(proc)
        reader.join(OUTPUT_DRAIN_SECS)
    else:
        # Nothing held the pipe, but a leak with its output closed or
        # redirected would still be here. Cheap to check, and a leaked mpv
        # that survives the run is how a machine collects a graveyard of them.
        _kill_leg_group(proc, warn=label)
    try:
        proc.stdout.close()
    except Exception:
        pass
    return label, rc, _counts("".join(captured))


#: How long to let a finished leg's buffered output drain before concluding
#: that something is holding the pipe open rather than still writing to it.
OUTPUT_DRAIN_SECS = 5.0


def _kill_leg_group(proc, warn=None):
    """SIGKILL whatever is left of a finished leg's process group.

    ``proc`` led its own group (``start_new_session``), so its pid *is* the
    group id -- which still resolves after the child itself has been reaped,
    while ``os.getpgid(proc.pid)`` no longer does.

    With ``warn``, says so only when something was actually there: an empty
    group raises ProcessLookupError, so the probe costs nothing and a silent
    run means the leg cleaned up after itself.
    """
    if os.name != "posix":
        return
    try:
        pgid = proc.pid
        if warn is not None:
            os.killpg(pgid, 0)          # probe; raises if the group is empty
            print("\n*** %s: leaked at least one process; cleaning up."
                  % warn, flush=True)
        os.killpg(pgid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        pass


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
