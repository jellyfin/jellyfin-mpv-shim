"""The tray child has to die when the app does.

The whole of this file is one property -- *quitting leaves no process
behind* -- asked at the three places it was broken at once.

`mpv_shim._claim_sigterm` installs a SIGTERM handler so `kill` runs the
orderly shutdown. `TrayManager.stop()` stops the tray child with
`terminate()`, which **is** a SIGTERM. Those two are only compatible while
the child does not inherit the handler, i.e. while the start method is
``spawn`` -- and it silently was not: ``run.py`` calls
``multiprocessing.freeze_support()``, whose first act is to *read* the start
method, which resolves the context to the platform default. The later
``set_start_method("spawn")`` then raised "context has already been set" into
an ``except RuntimeError: pass``, so the tray was forked, took a copy of the
handler, ignored every terminate(), and outlived the app.

Each test drives a real child process. They are seconds, not milliseconds,
and that is the point: nothing short of a real fork/terminate can fail the
way this failed.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import multiprocessing
import os
import signal
import subprocess
import sys
import textwrap
import time
import unittest
from unittest import mock

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _run_python(source):
    """Run a script in a fresh interpreter and return its stdout.

    A subprocess because every one of these is about process-global state --
    the resolved multiprocessing context, the main thread's signal
    dispositions -- which cannot be set up or undone inside a test runner
    that has to keep working afterwards.
    """
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(source)],
        cwd=REPO_ROOT, capture_output=True, text=True, timeout=120)
    if result.returncode != 0:
        raise AssertionError(
            "child interpreter failed (%s):\n%s\n%s"
            % (result.returncode, result.stdout, result.stderr))
    return result.stdout


class StartMethodTest(unittest.TestCase):
    """What `run.py` actually ends up with, not what it asks for."""

    def test_freeze_support_does_not_pin_the_start_method(self):
        # The regression, exactly as run.py sequences it: freeze_support()
        # first, then main()'s choice. Before the fix this printed the
        # platform default ("fork" on Linux) and nothing anywhere said so.
        out = _run_python("""
            import multiprocessing
            multiprocessing.freeze_support()      # run.py, before main()
            from jellyfin_mpv_shim.mpv_shim import _use_spawn_start_method
            _use_spawn_start_method()
            print(multiprocessing.get_start_method())
        """)
        self.assertEqual(out.strip(), "spawn")

    def test_it_is_spawn_from_a_clean_interpreter_too(self):
        # The installed console script's path -- no freeze_support() call.
        # It was already right, and force=True must not have broken it.
        out = _run_python("""
            import multiprocessing
            from jellyfin_mpv_shim.mpv_shim import _use_spawn_start_method
            _use_spawn_start_method()
            print(multiprocessing.get_start_method())
        """)
        self.assertEqual(out.strip(), "spawn")


class InheritedSignalTest(unittest.TestCase):
    """A forked child must not answer to the parent's handlers."""

    def test_a_forked_child_inherits_the_sigterm_handler(self):
        # Not a test of our code -- a test of the premise the other two
        # rest on. If this ever stops holding, the reset below is dead
        # weight and should be reconsidered rather than kept on faith.
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("no fork start method on this platform")
        out = _run_python("""
            import multiprocessing, os, signal, threading, time
            ctx = multiprocessing.get_context("fork")
            halt = threading.Event()
            signal.signal(signal.SIGTERM, lambda s, f: halt.set())
            def loop():
                while True:
                    time.sleep(0.05)
            p = ctx.Process(target=loop, daemon=True)
            p.start()
            time.sleep(1)
            p.terminate()
            p.join(3)
            print("alive" if p.is_alive() else "dead")
            p.kill()
        """)
        self.assertEqual(out.strip(), "alive")

    def test_the_tray_child_puts_sigterm_back(self):
        # _reset_inherited_signals is what makes terminate() mean terminate
        # again. Driven in a forked child holding a live handler, because
        # in-process the assertion is just "signal.signal did what it says".
        if "fork" not in multiprocessing.get_all_start_methods():
            self.skipTest("no fork start method on this platform")
        out = _run_python("""
            import multiprocessing, signal, threading, time
            from jellyfin_mpv_shim.tray import _reset_inherited_signals
            ctx = multiprocessing.get_context("fork")
            halt = threading.Event()
            signal.signal(signal.SIGTERM, lambda s, f: halt.set())
            def loop():
                _reset_inherited_signals()
                while True:
                    time.sleep(0.05)
            p = ctx.Process(target=loop, daemon=True)
            p.start()
            time.sleep(1)
            p.terminate()
            p.join(5)
            print("alive" if p.is_alive() else "dead")
            p.kill()
        """)
        self.assertEqual(out.strip(), "dead")


def _deaf_child():
    """A tray child that will not die for a SIGTERM, whatever the reason.

    Module level so it survives pickling to a spawned child.
    """
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.05)


class DeafTrayProcess(multiprocessing.Process):
    # `tmpdir` because the real TrayProcess takes it (the private temp
    # directory the parent removes in stop()). A stand-in that drops it
    # makes start() raise TypeError, which start() catches and reports as
    # "could not start the tray" -- so the escalation below would be
    # testing a child that was never there.
    def __init__(self, queue, tmpdir=None):
        self.queue = queue
        self.tmpdir = tmpdir
        multiprocessing.Process.__init__(self, daemon=True)

    def run(self):
        _deaf_child()


@unittest.skipIf(
    os.name == "nt",
    "the escalation under test is SIGTERM-then-SIGKILL, and a child that "
    "IGNORES the polite signal is the premise. Windows has no such child: "
    "Process.terminate() is TerminateProcess, which cannot be caught or "
    "ignored, so there is nothing to escalate from")
class TrayStopEscalationTest(unittest.TestCase):
    """`stop()` is the last line of defence, so it may not merely ask."""

    def _deaf_manager(self):
        """A TrayManager whose child cannot be stopped by a SIGTERM.

        The cleanup is not tidiness. multiprocessing's own atexit hook
        terminates each daemon child and then **joins** it, so a deaf child
        that survives the test does not merely linger -- it wedges the test
        runner's exit, turning a failing assertion into a hung suite and
        hiding the result these tests exist to report.
        """
        from jellyfin_mpv_shim import tray

        manager = tray.TrayManager({})
        with mock.patch.object(tray, "TrayProcess", DeafTrayProcess):
            self.assertTrue(manager.start())
        self.addCleanup(_hard_kill, manager._process)
        return manager

    def test_it_kills_a_child_that_ignores_sigterm(self):
        from jellyfin_mpv_shim import tray

        manager = self._deaf_manager()
        pid = manager._process.pid
        # Long enough to be sure the child reached its own signal(), so a
        # pass cannot come from having killed it before it was deaf.
        time.sleep(1.5)
        started = time.monotonic()
        manager.stop()
        elapsed = time.monotonic() - started

        self.assertFalse(_alive(pid),
                         "the tray child outlived TrayManager.stop()")
        # Bounded: this runs inside the shutdown sequence, which has its own
        # deadline, and a stop that takes minutes is its own bug.
        self.assertLess(elapsed, 3 * tray.TrayManager.TERMINATE_GRACE)

    def test_it_releases_the_queue(self):
        # os._exit skips multiprocessing's own cleanup, so an unclosed queue
        # is what puts the resource tracker's "leaked semaphore" warning
        # after "Shutdown complete." on every quit.
        manager = self._deaf_manager()
        manager.stop()
        self.assertIsNone(manager._queue)

    def test_stopping_twice_is_harmless(self):
        # The shutdown sequence can reach it more than once (ui.stop, then a
        # caller that stops the UI again), and a second stop that raised
        # would strand every step after it.
        manager = self._deaf_manager()
        manager.stop()
        manager.stop()


def _hard_kill(process):
    """SIGKILL a child and reap it, whatever state it is in."""
    if process is None:
        return
    try:
        if process.is_alive():
            process.kill()
        process.join(5)
    except Exception:
        pass


def _alive(pid):
    """Whether ``pid`` is a live process, as opposed to a reaped one.

    ``os.kill(pid, 0)`` alone answers yes for a zombie, which is exactly the
    state a child left unjoined ends up in -- so it would report the bug as
    fixed while the process was still in the table.
    """
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    try:
        with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return True


if __name__ == "__main__":
    unittest.main()
