#!/usr/bin/env python3
"""Run the unit suite across processes, one process per test module.

    xvfb-run -a python3 tools/run_tests_parallel.py

Same tests as ``python3 -m unittest discover tests``, same import semantics,
in a fraction of the wall clock. Stdlib only, like the suite it runs.

**One xvfb for the whole run, not one per worker.** Put `xvfb-run -a` in
front of THIS script rather than around the workers: `-a` picks a free
display by probing, and thirty-two of them probing at once race for the same
number. The workers inherit `DISPLAY` and share one server. (They still need
one — importing `player.py` opens a real mpv window; see
`docs/testing.md`.)

Why a process per module rather than threads: the suite is full of
module-level singletons, `sys.modules` eviction and a real mpv per process.
Threads would share all of it.

Two things this has to get right, and both have cost a session before:

* **`sys.argv` is neutralised before anything under `jellyfin_mpv_shim` is
  imported.** Importing almost anything there reaches `args.get_args()` at
  import time, which parses the real argv and exits with the app's usage
  line. `discover tests` gets away with it because unittest rewrites argv
  first; a worker taking arguments of its own does not.
* **The repo root goes on `sys.path` explicitly.** A script run from
  `tools/` has `tools/` as `sys.path[0]`, so `jellyfin_mpv_shim` resolves to
  whatever is pip-installed in `~/.venv` — silently, and it *runs*, just
  against the previous release. That is the trap in CLAUDE.md's "run tests
  from the repo root" rule, reached from a direction the rule does not
  cover.

Selection is by `TestLoader.discover(start_dir, pattern=...)` rather than by
module name, so each worker imports the module exactly as `discover tests`
would -- as a top-level `test_foo`, with `tests/` on the path. Loading it as
`tests.test_foo` instead would give it a different identity in `sys.modules`
than the serial run gives it.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS = os.path.join(ROOT, "tests")


#: What a worker prints once its tests are done. The parent reads THIS, not
#: the exit status -- see _worker.
RESULT = "JMS-RESULT"


def _utf8(stream):
    """Make *stream* carry any test output at all.

    A redirected stdout on Windows is cp1252, and a test whose name or
    docstring holds a character outside it (this suite uses arrows and box
    drawing) makes `print` raise UnicodeEncodeError. In a worker that loses
    the module; in the parent it lost the entire run -- 18 modules in, at the
    first failure detail, with no summary and no traceback, because the
    traceback could not be printed either.
    """
    try:
        stream.reconfigure(encoding="utf-8", errors="backslashreplace")
    except (AttributeError, ValueError):
        pass                        # already wrapped, or not a text stream


def _worker(pattern):
    """Run one module's tests and report on stdout. Never returns."""
    _utf8(sys.stdout)
    _utf8(sys.stderr)
    # Before importing anything under jellyfin_mpv_shim -- see the module
    # docstring. Both lines are load-bearing.
    sys.argv = [sys.argv[0]]
    sys.path.insert(0, ROOT)

    import unittest

    suite = unittest.TestLoader().discover(TESTS, pattern=pattern)
    total = suite.countTestCases()
    result = unittest.TextTestRunner(verbosity=1).run(suite)
    print("%s %d %d %d" % (RESULT, total, len(result.failures),
                           len(result.errors)))
    sys.stdout.flush()

    # os._exit, deliberately: skip interpreter teardown entirely.
    #
    # A module that imported player.py has a real libmpv in this process,
    # and tearing several dozen of those down at once -- which is what
    # running the suite in parallel does -- aborts with "pure virtual method
    # called" *after* the tests have passed. Both of the modules it hit are
    # green alone and green in the serial suite; the crash is in the exit
    # path, which is not what any of these tests are about.
    #
    # This is the reason the parent trusts the line above rather than the
    # exit status. A worker that dies BEFORE printing it is still a hard
    # failure -- that is a real crash mid-test, and it is reported as one.
    # Before the hard exit, because atexit will not run after it: the
    # tests' self-cleaning temp directories (tests/_tmpdirs) are registered
    # with atexit for a direct `unittest` run, and this path skips it. Left
    # out, each worker leaked its temp directories on every run -- which is
    # how /tmp reached five thousand entries.
    try:
        from tests import _tmpdirs

        _tmpdirs.cleanup_all()
    except Exception:
        pass

    # Same reason, different owner: the mpvtk scratch cache is product code
    # and cleans up via its own atexit hook, which this exit path skips too.
    # Only if the module was actually imported -- most workers never build a
    # browser and there is nothing to remove. Left out, a run leaked one
    # cache dir per worker that imported it, and on Windows nothing reclaims
    # those (rawimage._process_alive cannot tell a dead pid from a live one).
    rawimage = sys.modules.get("jellyfin_mpv_shim.mpvtk.rawimage")
    if rawimage is not None:
        try:
            rawimage.cleanup_this_process()
        except Exception:
            pass

    sys.stderr.flush()
    os._exit(0 if result.wasSuccessful() else 1)


def _default_jobs():
    """Half the CPUs, not all of them, and that is measured rather than
    cautious.

    Every worker that imports `player.py` creates a real mpv window, and
    they all share one Xvfb, which is single-threaded. On this 32-CPU box
    `-j32` finished in 50s on an idle machine and **starved** on a busy one:
    four modules that take 12-17s were still unfinished at 90s. `-j16` is
    64s and has not wobbled. The extra 14s buys a result you can trust
    while something else is running, which is most of the time.

    Raise it with `-j` on an otherwise idle machine; the ceiling is the
    slowest single module (~43s here), because that is one process and
    nothing splits it.
    """
    return max(2, (os.cpu_count() or 4) // 2)


def _modules():
    """Every module `discover tests` would collect, biggest file first.

    Size is a rough proxy for cost, and it only has to be rough: workers
    pull from a queue, so a bad guess costs one slot for one module rather
    than unbalancing a fixed split. Starting the big ones first is what
    keeps the tail short.

    `tests/integration` and `tests/e2e` are NOT included, for the same
    reason `discover tests` does not reach them -- neither is a package, and
    they have their own runners.
    """
    names = [n for n in os.listdir(TESTS)
             if n.startswith("test_") and n.endswith(".py")]
    return sorted(names, key=lambda n: -os.path.getsize(os.path.join(TESTS, n)))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-j", "--jobs", type=int, default=0,
                    help="worker processes (default: half the CPUs -- see "
                         "DEFAULT_JOBS)")
    ap.add_argument("-q", "--quiet", action="store_true",
                    help="only print the summary and any failures")
    ap.add_argument("-t", "--timeout", type=float, default=300.0,
                    help="seconds a single module may take before it is "
                         "killed and reported as failed (default: 300)")
    ap.add_argument("--worker", metavar="PATTERN",
                    help=argparse.SUPPRESS)   # internal
    args = ap.parse_args()

    if args.worker:
        return _worker(args.worker)

    _utf8(sys.stdout)
    _utf8(sys.stderr)
    modules = _modules()
    jobs = args.jobs or _default_jobs()
    # os.name: Windows has a desktop and no DISPLAY, so the bare check told
    # every Windows run to go and find xvfb. Kept in step with
    # run_integration._have_display and _harness.HAVE_DISPLAY, which answer
    # the same question for the other two suites.
    if (os.name != "nt" and not os.environ.get("DISPLAY")
            and not os.environ.get("WAYLAND_DISPLAY")):
        print("warning: no DISPLAY. Importing player.py opens a real mpv "
              "window, so run this under `xvfb-run -a`.", file=sys.stderr)

    pending = list(modules)
    running = {}          # Popen -> (module, started)
    done = []             # (module, rc, seconds, output, count)
    started = time.time()

    def launch():
        while pending and len(running) < jobs:
            mod = pending.pop(0)
            proc = subprocess.Popen(
                [sys.executable, os.path.abspath(__file__), "--worker", mod],
                cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                # Explicit, not text=True: that decodes with the locale
                # encoding, and cp1252 has undefined bytes -- so the parent
                # could fail to READ output the worker managed to write.
                text=True, encoding="utf-8", errors="replace",
                # Its own process group, so killing a worker takes the mpv
                # it started with it. A killed run that leaves mpv and Xvfb
                # children behind is how a machine accumulates a graveyard
                # of them across sessions -- one on this box outlived its
                # run by five days -- and the waste is what made a *serial*
                # suite look like it needed OOM headroom.
                start_new_session=True)
            # Drain the pipe NOW, in its own thread, rather than after the
            # worker exits. Nothing was reading it until then, so a worker
            # that outran the pipe buffer blocked in write() and never
            # exited -- and the parent, waiting for it to exit before
            # reading, waited out the full --timeout. Latent on Linux,
            # where the buffer is 64K and a passing module prints almost
            # nothing; reached on Windows, where one module's failure
            # tracebacks are enough. run_integration.py's pump() has always
            # done this.
            sink = []
            pump = threading.Thread(target=_drain, args=(proc, sink),
                                    daemon=True)
            pump.start()
            running[proc] = (mod, time.time(), sink, pump)

    def _drain(proc, sink):
        """Copy one worker's output into ``sink`` as it is produced."""
        try:
            for line in proc.stdout:
                sink.append(line)
        except (ValueError, OSError):
            pass                     # killed worker: the pipe went away
        finally:
            try:
                proc.stdout.close()
            except (ValueError, OSError):
                pass

    def reap(proc):
        """Kill a worker and everything it started."""
        if os.name != "posix":
            # No process groups to kill, and os.getpgid does not exist -- an
            # AttributeError here is not caught below and takes the runner
            # down instead of the worker it was asked to time out.
            proc.kill()
            return
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()

    launch()
    while running:
        for proc in list(running):
            if proc.poll() is None:
                if time.time() - running[proc][1] > args.timeout:
                    # A module that hangs must not hang the run. This is a
                    # real hazard here rather than a theoretical one:
                    # importing player.py starts a real mpv, and enough of
                    # them racing for one X server can block in creation.
                    reap(proc)
                continue
            mod, t0, sink, pump = running.pop(proc)
            # The writer is gone, so the reader is at EOF or a breath away.
            pump.join(timeout=30)
            out = "".join(sink)
            count, bad, reported = 0, 0, False
            for line in out.splitlines():
                if line.startswith(RESULT + " "):
                    _tag, n, fails, errs = line.split()
                    count, bad, reported = int(n), int(fails) + int(errs), True
            # No result line means the worker died before it could report:
            # a crash during the tests, an import that exited, a kill. That
            # is a failure however the process happened to exit.
            rc = 1 if (not reported or bad) else 0
            if not reported and time.time() - t0 >= args.timeout:
                out += ("\n*** killed after %.0fs (--timeout). It passes "
                        "alone; suspect contention for the X server or for "
                        "mpv, or a real hang inside the module.\n"
                        % args.timeout)
            done.append((mod, rc, time.time() - t0, out, count))
            if not args.quiet:
                print("%-4s %5.1fs %4d  %s"
                      % ("FAIL" if rc else "ok",
                         time.time() - t0, count, mod), flush=True)
            launch()
        if running:
            time.sleep(0.02)

    elapsed = time.time() - started
    failed = [d for d in done if d[1]]
    total = sum(d[4] for d in done)
    slowest = sorted(done, key=lambda d: -d[2])[:5]

    for mod, _rc, secs, out, _n in failed:
        print("\n" + "=" * 70 + "\nFAILED: %s (%.1fs)\n" % (mod, secs) + "=" * 70)
        print(out.rstrip())

    print("\n%d tests in %d modules, %d workers, %.1fs wall clock"
          % (total, len(done), jobs, elapsed))
    print("slowest: " + ", ".join("%s %.1fs" % (m, s) for m, _r, s, _o, _n
                                  in slowest))
    if failed:
        print("FAILED modules: " + " ".join(sorted(m for m, *_ in failed)))
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
