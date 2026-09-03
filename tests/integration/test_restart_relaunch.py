"""The restart, as two real processes.

This is the one that shipped broken, and it shipped broken *past a test*: the
relaunch was written below `exit_watchdog.finish()`, which ends in `os._exit`
and never returns, so the call was dead code. Every unit test still passed,
because they check the pieces -- does the button arm the flag, does the flag
reach Popen, is the ordering in `main` right -- and none of them starts a
process that has to come back.

So this one does. A real app is launched, arms a restart, and takes the
ordinary SIGTERM shutdown; the assertion is that a *second* real app exists
afterwards, started against the same configuration directory.

Deliberately the whole process and not a seam:

* `main`'s exit is where the bug was, and its last statements are exactly the
  part no in-process test can observe -- the interpreter is gone.
* The instance lock has to have been released before the new copy runs, or it
  hands off to the dying process and quits. That is a real race between two
  real processes and cannot be faked.
* The relaunch rebuilds the command from `sys.argv`, so a launch that is not
  a real launch would not exercise the reconstruction at all.

**One app start is shared across the positive assertions** (`setUpClass`),
because each one is a real start of a real player and this module is also
picked up by the whole-suite leg -- a per-test launch would put minutes on
the matrix to re-measure the same event. The negative case needs its own run
and says so.

Run once rather than per backend: nothing here touches mpv's API. It still
needs a display, because the child builds a real player on the way up.
"""

import os
import subprocess
import sys
import shutil
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# ...and the repo root. Run as a script -- which the __main__ block at the
# bottom invites -- `sys.path[0]` is this directory and the root is on the
# path nowhere, so `jellyfin_mpv_shim` resolves to whatever is pip-installed:
# silently, and it *runs*, against the previous release. Measured once as a
# renderer.lua from a fortnight ago failing a test about this tree.
# run_integration.py is unaffected (it spawns -m unittest with cwd=root).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))
import _harness as h  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
CHILD = os.path.join(HERE, "_restart_child.py")

#: How long the child waits before arming and quitting. Long enough that the
#: app is genuinely up -- a real player built, the config written -- so the
#: shutdown under test is a real one rather than a half-built app tearing
#: itself down.
START_DELAY = 8
#: Bound on one app start plus its replacement. Generous on purpose: a tight
#: bound here would buy a flaky test rather than a fast one.
TIMEOUT = 120


def _stop_app(config):
    """Ask whatever is running against ``config`` to stop. Returns its output.

    Through the app's own `stop` command rather than by pattern: the
    replacement is not our child -- it is our child's child, deliberately
    detached -- and a `pkill -f` matching the config path would also match
    this test's own command line (see the shell footgun in CLAUDE.md).
    """
    try:
        out = subprocess.run([sys.executable, os.path.join(REPO, "run.py"),
                              "--config", config, "stop"],
                             timeout=60, capture_output=True, text=True)
        return (out.stdout or "") + (out.stderr or "")
    except Exception:
        return ""


def _cleanup(config):
    """Wait for the app to be gone, then remove its configuration directory.

    Waiting is the point, and remove-then-retry is not enough. `stop` is a
    *request*: it returns as soon as the instance owning this directory has
    been told, and that app then runs its whole shutdown -- which SAVES THE
    CONFIG (window geometry, credentials). So an `rmtree` fired straight
    after succeeds and the directory reappears a second later, written by a
    process that is still exiting. That is why every run of this module was
    leaving one behind.

    The app's own `stop` is the liveness probe as well as the request: it
    reports "is not running" once nothing holds the instance lock, which is
    the same lock the replacement had to take to exist at all. No pattern
    matching, so no chance of matching this test's own command line.
    """
    deadline = time.time() + 60
    while time.time() < deadline:
        if "not running" in _stop_app(config):
            break
        time.sleep(1)
    # One more pass for anything written between the last check and now.
    for _ in range(10):
        shutil.rmtree(config, ignore_errors=True)
        if not os.path.exists(config):
            return
        time.sleep(0.5)


def _run_child(config, delay=START_DELAY, wedge=None):
    """Run one app process to completion and return (stdout+stderr, rc).

    Output goes to a FILE, not to pipes, and stdin is /dev/null. That is not
    tidiness: `close_fds=True` only closes descriptors from 3 up, so the
    detached replacement inherits this run's stdout and stderr. With pipes,
    `subprocess.run`'s timeout path kills the child and then calls an
    *untimed* `communicate()`, which blocks until every holder of the pipe
    exits -- and the remaining holder is a grandchild in its own session
    that this process cannot signal. One replacement that failed to reach
    its own SIGTERM would wedge the whole leg with no diagnostic.
    """
    env = dict(os.environ, JMS_RESTART_DELAY=str(delay))
    if wedge:
        env["JMS_RESTART_WEDGE"] = str(wedge)
    log_path = os.path.join(config, "child-output.log")
    with open(log_path, "ab") as out:
        proc = subprocess.run([sys.executable, CHILD, "--config", config],
                              timeout=TIMEOUT, env=env,
                              stdin=subprocess.DEVNULL,
                              stdout=out, stderr=subprocess.STDOUT)
    with open(log_path, "r", errors="replace", encoding="utf-8") as fh:
        return fh.read(), proc.returncode


def _await(path, timeout=TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if os.path.exists(path):
            return True
        time.sleep(0.25)
    return False


@unittest.skipUnless(h.HAVE_MPV_DISPLAY, "needs mpv and a display")
class RestartRelaunchTest(unittest.TestCase):
    """One armed restart, examined from several angles."""

    @classmethod
    def setUpClass(cls):
        cls.config = tempfile.mkdtemp(prefix="jms-restart-")
        # Registered BEFORE the run that can raise. `tearDownClass` is not
        # called when `setUpClass` raises, and `_run_child` raises
        # `TimeoutExpired` on the very failure this module exists to catch --
        # so the tidy-up has to be armed first or a failing run leaks a temp
        # directory and leaves a real app process behind.
        cls.addClassCleanup(_cleanup, cls.config)
        cls.marker = os.path.join(cls.config, "relaunched")
        cls.output, cls.returncode = _run_child(cls.config)
        cls.relaunched = _await(cls.marker)

    def test_a_restart_starts_a_replacement(self):
        """The whole feature, end to end."""
        self.assertTrue(
            self.relaunched,
            "no replacement app appeared.\noutput:\n%s"
            % self.output[-4000:])

    def test_main_ends_the_process_rather_than_returning(self):
        """The fact the bug turned on. `main` ends in
        `exit_watchdog.finish()`, which calls `os._exit`; if it ever returns,
        every statement below it becomes reachable and the reasoning that
        put the relaunch on a hook instead needs revisiting."""
        # Both halves: the sentinel absent AND the run having got far enough
        # to print anything at all. "MAIN RETURNED is absent" on its own is
        # satisfied by a child that died on an import error.
        self.assertIn("PARENT READY", self.output,
                      "the child never started, so its silence proves "
                      "nothing:\n%s" % self.output[-2000:])
        self.assertNotIn("MAIN RETURNED", self.output)

    def test_the_replacement_keeps_the_configuration_directory(self):
        """`--config` surviving is the difference between a restart and a
        different app: without it the copy comes back against the default
        directory, with different servers and different settings, and
        nothing says anything went wrong."""
        self.assertTrue(self.relaunched, "no replacement app appeared")
        with open(self.marker, encoding="utf-8") as fh:
            argv = fh.read().split("\n")
        self.assertIn("--config", argv)
        self.assertEqual(argv[argv.index("--config") + 1], self.config)

    def test_the_replacement_became_the_primary_instance(self):
        """The instance lock is held for the life of the process, so a
        replacement that started while the old copy still held it would hand
        off to the dying process and quit -- the restart would look like a
        plain quit, and only on a machine where the timing worked out.

        The generation file is the evidence: the replacement increments it
        on the way up, which it only reaches after `acquire()` let it be
        primary.
        """
        self.assertTrue(self.relaunched, "no replacement app appeared")
        with open(os.path.join(self.config, "generation"), encoding="utf-8") as fh:
            self.assertGreaterEqual(int(fh.read().strip()), 2)


@unittest.skipUnless(h.HAVE_MPV_DISPLAY, "needs mpv and a display")
class OrdinaryQuitTest(unittest.TestCase):
    """A quit nobody asked to restart stays a quit.

    Its own app start, and worth one: `relaunch_if_requested` runs at the end
    of **every** exit, so "does nothing unless asked" is the behaviour every
    other shutdown in the app depends on. Verified against a real process
    because the in-process version of this assertion cannot see an exit.
    """

    def test_nothing_is_spawned_when_no_restart_was_asked_for(self):
        config = tempfile.mkdtemp(prefix="jms-noquit-")
        self.addCleanup(_cleanup, config)
        # Generation 1 already used up, so the child records a marker and
        # arms nothing -- exactly an ordinary quit.
        with open(os.path.join(config, "generation"), "w", encoding="utf-8") as fh:
            fh.write("1")
        marker = os.path.join(config, "relaunched")
        output, _rc = _run_child(config, delay=START_DELAY)
        self.assertTrue(os.path.exists(marker),
                        "the app under test never started")
        os.remove(marker)
        # If a restart had been armed, a replacement would rewrite it.
        time.sleep(8)
        self.assertFalse(os.path.exists(marker),
                         "a restart happened without being asked for:\n%s"
                         % output[-2000:])


@unittest.skipUnless(h.HAVE_MPV_DISPLAY, "needs mpv and a display")
class WedgedShutdownRestartTest(unittest.TestCase):
    """A restart still happens when the shutdown never finishes.

    This is the case `exit_watchdog.set_final_action` was added for, and it
    had no runtime test: the orderly path was covered by the class above,
    and the forced path by two assertions that grep `arm`'s source for the
    string `_run_final_action()`. A mutation that leaves the string in place
    but cannot reach it -- guarding it, or hoisting it above the deadline
    wait -- passes both, and a user who presses *Restart Now* into a wedged
    shutdown watches the app vanish. Same shape as the bug that started all
    of this.

    Its own app start, and it is the slowest test here (the deadline has to
    expire), so the deadline is lowered rather than waited out.
    """

    def test_a_wedged_shutdown_still_comes_back(self):
        config = tempfile.mkdtemp(prefix="jms-wedge-")
        self.addCleanup(_cleanup, config)
        marker = os.path.join(config, "relaunched")

        output, rc = _run_child(config, wedge=4)

        self.assertIn("PARENT READY", output,
                      "the child never started:\n%s" % output[-2000:])
        # The watchdog kills a wedged shutdown with status 1, which is how
        # we know this test exercised the forced path rather than the
        # orderly one it is named apart from.
        self.assertEqual(rc, 1,
                         "the shutdown was not forced, so this measured the "
                         "ordinary exit:\n%s" % output[-2000:])
        self.assertIn("did not finish within", output,
                      "the watchdog never reported a wedge")
        self.assertTrue(_await(marker),
                        "no replacement app appeared from the forced exit:"
                        "\n%s" % output[-4000:])


if __name__ == "__main__":
    unittest.main()
