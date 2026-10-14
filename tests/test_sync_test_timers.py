"""JMS_TEST_SYNC_TIMERS, the e2e suite's way to run the sweep settle and the
reap hold short. Unset must leave the shipped values: the hook lives in
production code, and a default it disturbed would change every install."""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import subprocess
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROBE = ("import sys; sys.argv = ['x']\n"
         "from jellyfin_mpv_shim.sync import manager as m\n"
         "print(m.USERDATA_SWEEP_SETTLE, m.USERDATA_SWEEP_FLOOR,\n"
         "      m.REAP_SWEEP_HOLD, m.PLAYSTATE_INTERVAL)\n")


def _timers(spec):
    env = dict(os.environ)
    env.pop("JMS_TEST_SYNC_TIMERS", None)
    if spec is not None:
        env["JMS_TEST_SYNC_TIMERS"] = spec
    out = subprocess.run([sys.executable, "-c", PROBE], cwd=REPO, env=env,
                         capture_output=True, text=True, timeout=120)
    if out.returncode:
        raise AssertionError(out.stderr)
    return tuple(int(v) for v in out.stdout.split()[-4:])


class SyncTestTimersTest(unittest.TestCase):

    def test_unset_is_the_shipped_values(self):
        self.assertEqual((60, 300, 900, 30), _timers(None))

    def test_each_is_overridden_at_import(self):
        self.assertEqual((5, 30, 90, 5),
                         _timers("settle=5,floor=30,hold=90,replay=5"))

    def test_a_bad_entry_is_ignored_and_the_rest_still_apply(self):
        self.assertEqual((60, 300, 30, 30),
                         _timers("settle=soon,nope=1,hold=30"))


if __name__ == "__main__":
    unittest.main()
