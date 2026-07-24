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


def _spawn(config_dir, hold=0.0, wedge=False, new_session=False):
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = config_dir
    env["SI_HOLD"] = str(hold)
    env["SI_WEDGE"] = "1" if wedge else "0"
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
            with open(os.path.join("/proc", entry, "stat")) as fh:
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


class SingleInstanceMultiprocTest(unittest.TestCase):
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
