"""Multi-process election tests for SingleInstance.

The fast suite (tests/test_single_instance.py) covers the in-process logic. This
suite races *real* OS processes on the same config dir, because the guarantee
the design leans on — flock granting exactly one primary — only truly holds
across processes, and that is the property the maintainer cares about (two
catalog writers would corrupt the offline DB).
"""

import os
import subprocess
import sys
import tempfile
import time
import unittest

# The repo root, before the package is imported. Run as a script -- which
# the __main__ block at the bottom invites -- `sys.path[0]` is this
# directory and the root is on the path nowhere, so `jellyfin_mpv_shim`
# resolves to whatever is pip-installed: silently, and it *runs*, against
# the previous release. run_integration.py is unaffected (it spawns
# -m unittest with cwd=root).
sys.path.insert(0, os.path.dirname(os.path.dirname(
    os.path.dirname(os.path.abspath(__file__)))))

from jellyfin_mpv_shim.constants import APP_NAME  # noqa: E402

_HERE = os.path.dirname(os.path.abspath(__file__))
_CHILD = os.path.join(_HERE, "_si_child.py")


def _prep_config_dir(base):
    """Pre-create the per-app config subdir. conffile.get() has a check-then-
    makedirs TOCTOU race that FileExistsErrors when several fresh processes
    create it at once (a real, separate app bug — see the README). Creating it
    up front keeps *this* (single-instance election) test deterministic instead
    of flaky on that unrelated race."""
    os.makedirs(os.path.join(base, APP_NAME), exist_ok=True)
    return base


def _spawn(config_dir, hold=0.0, wedge=False, new_session=False,
           activate_log=None):
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = config_dir
    # conffile.win32 reads APPDATA and knows nothing about XDG_CONFIG_HOME, so
    # on Windows the line above isolates nothing: every child lands in the real
    # %APPDATA% and shares one lock. That reads as "different config dirs did
    # not both win" -- a product failure -- when the two children were never
    # given different config dirs in the first place.
    env["APPDATA"] = config_dir
    env["SI_HOLD"] = str(hold)
    env["SI_WEDGE"] = "1" if wedge else "0"
    if activate_log:
        env["SI_ACTIVATE_LOG"] = activate_log
    else:
        env.pop("SI_ACTIVATE_LOG", None)
    # start_new_session makes the child a session/process-group leader, so its
    # pgid == its pid; any grandchild it leaks inherits that group and can be
    # spotted even after being reparented to init (see OrphanedChildOnExitTest).
    return subprocess.Popen([sys.executable, _CHILD],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env, start_new_session=new_session)


def _live_pgid_members(pgid):
    """PIDs of live processes in process group ``pgid`` (Linux, via /proc).

    Used to detect a leaked child/forkserver after a process exits: an orphan
    reparented to init keeps its process-group id, so a non-empty group after
    the group leader is gone means something was left running."""
    members = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(os.path.join("/proc", entry, "stat"), encoding="utf-8") as fh:
                data = fh.read()
        except OSError:
            continue  # process exited between listing and reading
        # Fields after the (possibly space/paren-containing) comm: state ppid
        # pgrp ... — so pgrp is the 3rd token past the final ')'.
        rparen = data.rfind(")")
        if rparen == -1:
            continue
        fields = data[rparen + 2:].split()
        try:
            pgrp = int(fields[2])
        except (IndexError, ValueError):
            continue
        if pgrp == pgid:
            members.append(int(entry))
    return members


def _first_line(proc, timeout=15):
    # readline blocks until the child prints its verdict; guard with a deadline.
    deadline = time.time() + timeout
    line = proc.stdout.readline().strip()
    if not line and time.time() > deadline:
        raise AssertionError("child produced no verdict")
    return line


class _MultiprocBase:
    """setUp and spawn bookkeeping, shared by the two test classes below.

    A plain mixin rather than a base TestCase: subclassing a TestCase makes
    unittest collect and RUN the parent's tests again under the child's
    name, so the five election tests would be paid for twice.
    """

    def setUp(self):
        self._cfg = _prep_config_dir(tempfile.mkdtemp(prefix="jms-si-"))
        self.addCleanup(self._rmtree, self._cfg)
        self._procs = []
        self.addCleanup(self._reap)

    @staticmethod
    def _rmtree(path):
        import shutil
        shutil.rmtree(path, ignore_errors=True)

    def _reap(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(5)
            except Exception:
                p.kill()

    def _spawn(self, config_dir, **kw):
        p = _spawn(config_dir, **kw)
        self._procs.append(p)
        return p


class SingleInstanceMultiprocTest(_MultiprocBase, unittest.TestCase):
    def test_exactly_one_primary_when_processes_race(self):
        # N processes launched at once against one config dir: flock must grant
        # exactly one primary; everyone else refuses to run.
        procs = [self._spawn(self._cfg, hold=3) for _ in range(6)]
        verdicts = [_first_line(p) for p in procs]
        self.assertEqual(verdicts.count("PRIMARY"), 1,
                         "expected exactly one primary, got %r" % verdicts)
        self.assertEqual(verdicts.count("SECONDARY"), 5)

    def test_second_launch_blocked_while_primary_holds(self):
        primary = self._spawn(self._cfg, hold=5)
        self.assertEqual(_first_line(primary), "PRIMARY")
        second = self._spawn(self._cfg, hold=0)
        self.assertEqual(_first_line(second), "SECONDARY")

    def test_wedged_primary_listener_still_blocks_duplicate(self):
        # A primary whose activation socket has died must still block a second
        # launch — the election is the guard-file lock, not the handoff.
        primary = self._spawn(self._cfg, hold=5, wedge=True)
        self.assertEqual(_first_line(primary), "PRIMARY")
        time.sleep(0.3)  # let the wedge take effect
        second = self._spawn(self._cfg, hold=0)
        self.assertEqual(_first_line(second), "SECONDARY")

    def test_different_config_dirs_both_win(self):
        other = _prep_config_dir(tempfile.mkdtemp(prefix="jms-si-b-"))
        self.addCleanup(self._rmtree, other)
        a = self._spawn(self._cfg, hold=3)
        b = self._spawn(other, hold=3)
        self.assertEqual(_first_line(a), "PRIMARY")
        self.assertEqual(_first_line(b), "PRIMARY")

    def test_lock_released_on_exit_allows_new_primary(self):
        first = self._spawn(self._cfg, hold=0)   # acquires then releases + exits
        self.assertEqual(_first_line(first), "PRIMARY")
        first.wait(10)
        second = self._spawn(self._cfg, hold=0)
        self.assertEqual(_first_line(second), "PRIMARY")


class ActivationHandoffTest(_MultiprocBase, unittest.TestCase):
    """A blocked launch must SURFACE the running copy, not just decline.

    The tests above prove the election: exactly one primary, everyone else
    refuses to run. None of them proves the other half -- that the refusal
    reaches the primary and runs `on_activate`, which is what puts the
    window back in front of somebody who double-clicked the icon because
    they could not see the app (#718). A primary that elects correctly and
    never runs the handler is indistinguishable from the outside, and the
    app it leaves you with is one you cannot get at.

    The handler is deliberately not `ui.activate()` here: what this file can
    say something about is the CROSS-PROCESS delivery. What activate() then
    does with a minimized browser is a different question, asked against a
    real player in test_startup_window.py.
    """

    def _deliveries(self, path, want=1, timeout=15):
        """Lines in the activation log, once there are ``want`` of them.

        Polled rather than read once, and the reason is in the protocol:
        `_serve_one` acknowledges BEFORE calling the handler, so the second
        launch can have exited before the primary has recorded anything.
        A single read here would be a race that passes on a fast machine.
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().split()
            except OSError:
                lines = []
            if len(lines) >= want:
                return lines
            time.sleep(0.05)
        return lines

    def _log_path(self):
        return os.path.join(self._cfg, "activations.log")

    def test_a_blocked_launch_activates_the_primary(self):
        log = self._log_path()
        primary = self._spawn(self._cfg, hold=8, activate_log=log)
        self.assertEqual(_first_line(primary), "PRIMARY")
        second = self._spawn(self._cfg, hold=0)
        self.assertEqual(_first_line(second), "SECONDARY")
        self.assertEqual(self._deliveries(log), ["SHOW"])

    def test_a_lone_primary_is_never_activated(self):
        """So the assertion above is about the handoff and not about the
        handler running at startup."""
        log = self._log_path()
        primary = self._spawn(self._cfg, hold=2, activate_log=log)
        self.assertEqual(_first_line(primary), "PRIMARY")
        primary.wait(15)
        self.assertEqual(self._deliveries(log, want=1, timeout=0.5), [])

    def test_every_repeat_launch_activates_it_again(self):
        """Three, not one. The listener serves each connection on its own
        thread and the socket outlives the handoff; a primary that handed
        off once and then stopped listening -- a closed socket, a thread
        that died on an exception -- would pass a single-shot test and
        leave the user pressing a shortcut that does nothing from the
        second press onwards."""
        log = self._log_path()
        primary = self._spawn(self._cfg, hold=12, activate_log=log)
        self.assertEqual(_first_line(primary), "PRIMARY")
        for n in range(1, 4):
            later = self._spawn(self._cfg, hold=0)
            self.assertEqual(_first_line(later), "SECONDARY")
            self.assertEqual(self._deliveries(log, want=n), ["SHOW"] * n)

    def test_a_wedged_primary_blocks_without_activating(self):
        """The state the election test already covers, from the other side:
        the duplicate is still refused, and nothing is surfaced because
        there is no listener to ask. Blocking is the lock's job and
        surfacing is the socket's, and this is what says so."""
        log = self._log_path()
        primary = self._spawn(self._cfg, hold=6, wedge=True, activate_log=log)
        self.assertEqual(_first_line(primary), "PRIMARY")
        time.sleep(0.3)
        second = self._spawn(self._cfg, hold=0)
        self.assertEqual(_first_line(second), "SECONDARY")
        self.assertEqual(self._deliveries(log, want=1, timeout=1.0), [])


@unittest.skipUnless(sys.platform.startswith("linux"),
                     "reads /proc for process-group membership")
class OrphanedChildOnExitTest(unittest.TestCase):
    """Issue #505: the single-instance guard must not orphan a helper /
    multiprocessing forkserver process on exit. acquire() only starts daemon
    threads and a socket today; this pins that a clean acquire → hold → release
    → exit leaves no surviving process in the child's group."""

    def setUp(self):
        self._cfg = _prep_config_dir(tempfile.mkdtemp(prefix="jms-si-orphan-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(
            self._cfg, ignore_errors=True))
        self._procs = []
        self.addCleanup(self._reap)

    def _reap(self):
        for p in self._procs:
            try:
                p.terminate()
                p.wait(5)
            except Exception:
                p.kill()

    def test_no_child_or_forkserver_survives_after_exit(self):
        proc = _spawn(self._cfg, hold=0.5, new_session=True)
        self._procs.append(proc)
        self.assertEqual(_first_line(proc), "PRIMARY")

        pgid = proc.pid  # it is its own group leader (start_new_session)
        # While the primary holds, the group holds exactly the child — acquire
        # spawned no helper process.
        self.assertEqual(_live_pgid_members(pgid), [proc.pid],
                         "acquire/hold spawned an unexpected process")

        self.assertEqual(proc.wait(10), 0, "child did not exit cleanly")
        # release() + a clean exit must drain the group entirely.
        survivors = _live_pgid_members(pgid)
        self.assertEqual(survivors, [],
                         "process(es) orphaned after teardown: %r" % survivors)


if __name__ == "__main__":
    unittest.main()
