"""The integration matrix's own pass/fail accounting.

`run_integration.py` judged a leg purely on its exit status, and every mpvtk
and real-mpv class is decorated with a skipUnless for mpv / ffmpeg / a
display. So a machine missing any of those printed a fully green matrix —
"All legs passed" — having executed zero UI assertions. That is worse than a
red one: it is a confident claim that nothing is wrong, made by a run that
checked nothing.

These cover the accounting, not the tests it runs.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "integration"))

import run_integration as runner  # noqa: E402


class TestCounts(unittest.TestCase):
    def test_it_reads_the_run_and_skip_totals(self):
        out = "test_a ... ok\nRan 12 tests in 3.4s\n\nOK (skipped=5)\n"
        self.assertEqual(runner._counts(out), (12, 5))

    def test_no_skips_reported_is_zero_not_none(self):
        self.assertEqual(runner._counts("Ran 3 tests in 0.1s\n\nOK\n"), (3, 0))

    def test_output_with_no_summary_is_unknown(self):
        """A leg that crashed before unittest printed anything must not be
        mistaken for one that ran nothing — unknown is not hollow."""
        self.assertEqual(runner._counts("Segmentation fault\n"), (None, None))

    def test_a_failed_run_still_reports_its_totals(self):
        out = "Ran 8 tests in 1s\n\nFAILED (failures=1, skipped=2)\n"
        self.assertEqual(runner._counts(out), (8, 2))


class TestLegStatus(unittest.TestCase):
    def test_a_normal_pass(self):
        text, failed, hollow = runner.leg_status(0, 10, 1)
        self.assertEqual((failed, hollow), (0, 0))
        self.assertIn("9 run, 1 skipped", text)

    def test_a_failure_is_a_failure(self):
        _text, failed, hollow = runner.leg_status(1, 10, 0)
        self.assertEqual((failed, hollow), (1, 0))

    def test_a_leg_that_skipped_everything_is_hollow(self):
        """The case that mattered: rc == 0, so the old runner called it
        PASS and printed 'All legs passed'."""
        text, failed, hollow = runner.leg_status(0, 25, 25)
        self.assertEqual(failed, 0)
        self.assertEqual(hollow, 1)
        self.assertIn("nothing ran", text)

    def test_a_hollow_leg_is_not_double_counted_as_a_failure(self):
        _text, failed, _hollow = runner.leg_status(0, 25, 25)
        self.assertEqual(failed, 0, "hollow must not inflate the fail count")

    def test_an_empty_leg_is_not_hollow(self):
        """Zero tests collected is a different problem (a bad module path)
        and shows up as a non-zero rc; do not also call it hollow."""
        _text, _failed, hollow = runner.leg_status(0, 0, 0)
        self.assertEqual(hollow, 0)

    def test_unknown_counts_are_not_hollow(self):
        text, failed, hollow = runner.leg_status(0, None, None)
        self.assertEqual((failed, hollow), (0, 0))
        self.assertNotIn("run,", text)

    def test_one_real_test_among_skips_is_enough_to_not_be_hollow(self):
        _text, _failed, hollow = runner.leg_status(0, 25, 24)
        self.assertEqual(hollow, 0)


class _Recorder:
    """A stdout that remembers the ORDER of writes and flushes."""

    def __init__(self):
        self.events = []

    def write(self, text):
        self.events.append(("write", text))

    def flush(self):
        self.events.append(("flush", None))


class TestOutputStreamsWhileTheLegRuns(unittest.TestCase):
    """A leg's output must reach the log while the leg is still running.

    `_run` tees the child through our own stdout, and that is block-buffered
    whenever it is redirected -- which is every `run_integration.py > log`.
    Flushing after the loop instead of inside it meant a whole-suite leg's
    ~147s of output arrived in a single write when the leg ended, so the log
    sat unchanged throughout it. A log that stops growing is indistinguishable
    from a hung run, and twice it was read as one; the second time a
    stall-watchdog fired during a leg that went on to report `Ran 324 tests
    ... OK`.
    """

    LINES = ["first\n", "second\n", "third\n"]

    def _drive(self):
        import subprocess

        rec = _Recorder()
        captured = {}

        class FakeProc:
            # `pid` is not decoration: `_run` ends by cleaning up the leg's
            # process group, and a fake without one would make that call
            # unreachable while still reporting a pass. Negative so that a
            # real killpg escaping the guards cannot name a live group.
            pid = -999999

            def __init__(self):
                self.stdout = iter(TestOutputStreamsWhileTheLegRuns.LINES)

            def wait(self):
                return 0

        def fake_popen(cmd, **kwargs):
            captured["cmd"] = cmd
            return FakeProc()

        orig_popen, orig_stdout = subprocess.Popen, sys.stdout
        subprocess.Popen, sys.stdout = fake_popen, rec
        try:
            runner._run(["tests.integration.test_example"])
        finally:
            subprocess.Popen, sys.stdout = orig_popen, orig_stdout
        return rec.events, captured["cmd"]

    def test_every_line_is_flushed_as_it_arrives(self):
        events, _cmd = self._drive()
        first = next(i for i, (k, v) in enumerate(events)
                     if k == "write" and v == self.LINES[0])
        last = next(i for i, (k, v) in enumerate(events)
                    if k == "write" and v == self.LINES[-1])
        self.assertTrue(
            any(k == "flush" for k, _ in events[first:last]),
            "nothing was flushed between the first line and the last, so a "
            "running leg would look like a stalled one")

    def test_the_child_is_unbuffered_too(self):
        """Our flush cannot help if the child is holding the lines."""
        _events, cmd = self._drive()
        self.assertIn("-u", cmd)
        self.assertLess(cmd.index("-u"), cmd.index("-m"),
                        "-u must reach python, not unittest")


class TestStrictIsWired(unittest.TestCase):
    def test_the_flag_exists(self):
        """--strict is what CI should use; a typo'd flag name would make
        the whole guard silently absent."""
        import subprocess
        out = subprocess.run(
            [sys.executable, runner.__file__, "--help"],
            capture_output=True, text=True, timeout=60).stdout
        self.assertIn("--strict", out)


if __name__ == "__main__":
    unittest.main()


class TestALegThatLeaksAProcessStillEnds(unittest.TestCase):
    """A leg that exits while something still holds its output pipe.

    This wedged the whole matrix indefinitely, *after the leg had passed*: a
    test mpv outliving its leg is reparented to init still holding the write
    end of the pipe `_run` was reading to EOF, so EOF could never arrive. The
    leg's own `Ran 324 tests ... OK` was already in the log; only the exit was
    missing, and the run sat there until the mpv was killed by hand.

    `tools/run_tests_parallel.py` has had the process-group discipline from
    the start. This runner had not.
    """

    # The grandchild outlives its parent holding stdout -- exactly what a
    # leaked mpv does. If `_run` waits for EOF it waits this long.
    LEAK_SECONDS = 60
    # Generous next to OUTPUT_DRAIN_SECS, tight next to LEAK_SECONDS: the
    # point is only to tell "returned" from "waited for the grandchild".
    PATIENCE = 25

    LEAK_SRC = (
        "import os, sys, time\n"
        "if os.fork() == 0:\n"
        "    time.sleep({leak})\n"        # holds fd 1 open
        "    os._exit(0)\n"
        "sys.stdout.write('Ran 1 test in 0.0s\\n')\n"
        "sys.stdout.write('OK\\n')\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n"
    )

    @unittest.skipUnless(hasattr(os, "fork"), "needs fork")
    def test_it_ends_when_the_leg_ends_not_when_the_pipe_closes(self):
        import subprocess
        import threading

        orig_popen = subprocess.Popen
        leak_cmd = [sys.executable, "-c",
                    self.LEAK_SRC.format(leak=self.LEAK_SECONDS)]

        def fake_popen(cmd, **kwargs):
            # The runner's own kwargs are kept -- start_new_session is the
            # half of the fix this exercises.
            return orig_popen(leak_cmd, **kwargs)

        result = {}

        def drive():
            try:
                result["value"] = runner._run(["tests.integration.whatever"])
            except BaseException as exc:            # pragma: no cover
                result["error"] = exc

        subprocess.Popen = fake_popen
        try:
            t = threading.Thread(target=drive, daemon=True)
            t.start()
            t.join(self.PATIENCE)
            # A regression must FAIL here rather than hang the suite.
            self.assertFalse(
                t.is_alive(),
                "_run did not return within %ds: it is waiting for the "
                "leaked grandchild to close the pipe instead of for the leg "
                "to exit, which is the hang this guards" % self.PATIENCE)
        finally:
            subprocess.Popen = orig_popen

        self.assertNotIn("error", result, repr(result.get("error")))
        _label, rc, (ran, _skipped) = result["value"]
        self.assertEqual(rc, 0)
        # The leg's real output was still captured, not lost to the shortcut.
        self.assertEqual(ran, 1)


class TestTheHarnessMakesAModuleRunnableAlone(unittest.TestCase):
    """`python -m unittest -v tests.integration.test_mpvtk_hud` must work.

    Importing almost anything under `jellyfin_mpv_shim` eventually reaches
    `args.get_args()`, which parses the REAL argv -- so a runner's `-v` and
    the module path leave the app's argparse printing its usage line and
    exiting, which reads as a broken test module and is not. `_harness`
    primes the parser at import for exactly this, and the integration
    modules used to depend on a *sibling* in the same leg having done it:
    each one passed in the matrix and died on its own.

    A subprocess with a runner-shaped argv, because in-process this file
    has already scrubbed sys.argv (see the top) and could not see the bug.
    """

    PROBE = (
        "import sys, os\n"
        "sys.path.insert(0, %r)\n"
        "sys.path.insert(0, os.path.join(%r, 'tests', 'integration'))\n"
        "import _harness\n"
        "from jellyfin_mpv_shim import conffile\n"
        "print('CONFDIR', conffile.confdir('jellyfin-mpv-shim'))\n"
    )

    def test_a_dirty_argv_no_longer_reaches_the_app_parser(self):
        import subprocess
        import tempfile
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        out = subprocess.run(
            [sys.executable, "-c", self.PROBE % (root, root),
             "-v", "tests.integration.test_mpvtk_hud"],
            capture_output=True, text=True, cwd=root, timeout=120)
        self.assertEqual(out.returncode, 0,
                         "importing the harness under a runner argv exits: %s"
                         % out.stderr[-800:])
        line = [ln for ln in out.stdout.splitlines()
                if ln.startswith("CONFDIR ")]
        self.assertTrue(line, "no config dir resolved: %r" % out.stdout)
        confdir = line[0].split(" ", 1)[1]
        # ...and at a throwaway. The prime pins one so a leg cannot read or
        # write the developer's own config; a prime that resolved to the
        # real directory would satisfy the check above and be worse than
        # the crash it replaced.
        self.assertTrue(
            confdir.startswith(tempfile.gettempdir()),
            "the harness primed the arg parser at the real config dir: %r"
            % confdir)



class TestEveryModuleRunAsAScriptGetsThisTree(unittest.TestCase):
    """`python3 tests/integration/test_foo.py` must import THIS repo.

    Every module here ends in a `__main__` block, which invites exactly
    that -- and run that way `sys.path[0]` is `tests/integration` and the
    repo root is on the path nowhere, so `jellyfin_mpv_shim` resolves to
    whatever is pip-installed. Silently, and it *runs*: measured once as a
    `renderer.lua` a fortnight old failing a test about this tree, and a
    stale install can as easily produce a false pass.

    Asserted for **every** module rather than the one that was noticed:
    nineteen of them had the same preamble, and a guard pasted into one is
    the one the next round reports. `run_integration.py` itself is not
    affected -- it spawns `-m unittest` with `cwd` at the root -- so
    nothing in the matrix would ever have gone red over this.
    """

    #: Modules with no `__main__` block are not making the invitation.
    @staticmethod
    def _modules():
        here = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "tests", "integration")
        for name in sorted(os.listdir(here)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            path = os.path.join(here, name)
            with open(path, encoding="utf-8") as fh:
                if 'if __name__ == "__main__"' in fh.read():
                    yield name, path

    def test_there_are_modules_to_check(self):
        """The guard on the guard: a listing that finds nothing would make
        every subTest below vacuous and report a pass."""
        self.assertGreater(len(list(self._modules())), 10)

    def test_the_repo_root_is_on_the_path_before_the_package_is_imported(self):
        """Executed, not grepped for.

        The module's own preamble runs up to the first `jellyfin_mpv_shim`
        import and then reports which copy it would get, from a cwd that
        cannot put the root on the path by accident.
        """
        import subprocess
        import tempfile

        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        expected = os.path.join(root, "jellyfin_mpv_shim")
        for name, path in self._modules():
            with self.subTest(module=name):
                probe = (
                    "import sys, runpy\n"
                    # argv as `python3 path/to/test_foo.py` leaves it, and
                    # sys.path[0] as the interpreter would set it.
                    "sys.argv = [%r]\n"
                    "sys.path.insert(0, %r)\n"
                    "import ast\n"
                    "src = open(%r, encoding='utf-8').read()\n"
                    # Everything above the first class or def -- the imports
                    # and the path preamble -- without running a test. Cut
                    # with ast rather than by splitting on 'class ', because
                    # the first one here is often decorated.
                    "tree = ast.parse(src)\n"
                    "tops = [n for n in tree.body if isinstance(n, "
                    "(ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))]\n"
                    "stop = min([tops[0].lineno] + "
                    "[d.lineno for d in tops[0].decorator_list]) "
                    "if tops else len(src.splitlines()) + 1\n"
                    "head = chr(10).join(src.splitlines()[:stop - 1])\n"
                    "exec(compile(head, %r, 'exec'), "
                    "{'__file__': %r, '__name__': 'not_main'})\n"
                    "import jellyfin_mpv_shim as j\n"
                    "print('PKG', j.__file__)\n"
                    % (path, os.path.dirname(path), path, path, path))
                out = subprocess.run(
                    [sys.executable, "-c", probe],
                    capture_output=True, text=True,
                    cwd=tempfile.gettempdir(), timeout=180)
                line = [ln for ln in out.stdout.splitlines()
                        if ln.startswith("PKG ")]
                self.assertTrue(
                    line, "%s did not resolve the package: %s"
                    % (name, out.stderr[-600:]))
                self.assertTrue(
                    line[0].split(" ", 1)[1].startswith(expected),
                    "%s would run against %s, not this tree"
                    % (name, line[0].split(" ", 1)[1]))
