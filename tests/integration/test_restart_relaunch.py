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
    """Ask whatever is running against ``config`` to stop.

    Through the app's own `stop` command rather than by pattern: the
    replacement is not our child -- it is our child's child, deliberately
    detached -- and a `pkill -f` matching the config path would also match
    this test's own command line (see the shell footgun in CLAUDE.md).
    """
    try:
        subprocess.run([sys.executable, os.path.join(REPO, "run.py"),
                        "--config", config, "stop"],
                       timeout=60, capture_output=True)
    except Exception:
        pass


def _run_child(config, delay=START_DELAY):
    env = dict(os.environ, JMS_RESTART_DELAY=str(delay))
    return subprocess.run([sys.executable, CHILD, "--config", config],
                          timeout=TIMEOUT, capture_output=True, text=True,
                          env=env)


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
        cls.marker = os.path.join(cls.config, "relaunched")
        cls.out = _run_child(cls.config)
        cls.relaunched = _await(cls.marker)

    @classmethod
    def tearDownClass(cls):
        _stop_app(cls.config)
        shutil.rmtree(cls.config, ignore_errors=True)

    def test_a_restart_starts_a_replacement(self):
        """The whole feature, end to end."""
        self.assertTrue(
            self.relaunched,
            "no replacement app appeared.\nstdout:\n%s\nstderr:\n%s"
            % (self.out.stdout[-3000:], self.out.stderr[-3000:]))

    def test_main_ends_the_process_rather_than_returning(self):
        """The fact the bug turned on. `main` ends in
        `exit_watchdog.finish()`, which calls `os._exit`; if it ever returns,
        every statement below it becomes reachable and the reasoning that
        put the relaunch on a hook instead needs revisiting."""
        self.assertNotIn("MAIN RETURNED", self.out.stdout)

    def test_the_replacement_keeps_the_configuration_directory(self):
        """`--config` surviving is the difference between a restart and a
        different app: without it the copy comes back against the default
        directory, with different servers and different settings, and
        nothing says anything went wrong."""
        self.assertTrue(self.relaunched, "no replacement app appeared")
        with open(self.marker) as fh:
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
        with open(os.path.join(self.config, "generation")) as fh:
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
        self.addCleanup(shutil.rmtree, config, ignore_errors=True)
        self.addCleanup(_stop_app, config)
        # Generation 1 already used up, so the child records a marker and
        # arms nothing -- exactly an ordinary quit.
        with open(os.path.join(config, "generation"), "w") as fh:
            fh.write("1")
        marker = os.path.join(config, "relaunched")
        _run_child(config, delay=START_DELAY)
        self.assertTrue(os.path.exists(marker),
                        "the app under test never started")
        os.remove(marker)
        # If a restart had been armed, a replacement would rewrite it.
        time.sleep(8)
        self.assertFalse(os.path.exists(marker),
                         "a restart happened without being asked for")


if __name__ == "__main__":
    unittest.main()
