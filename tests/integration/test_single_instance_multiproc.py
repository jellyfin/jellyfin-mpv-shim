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


def _spawn(config_dir, hold=0.0, wedge=False):
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = config_dir
    env["SI_HOLD"] = str(hold)
    env["SI_WEDGE"] = "1" if wedge else "0"
    return subprocess.Popen([sys.executable, _CHILD],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)


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


if __name__ == "__main__":
    unittest.main()
