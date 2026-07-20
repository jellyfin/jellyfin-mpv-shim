"""The fake-mpv harness must not poison the process for later tests.

``import_player_with_fake_mpv`` installs a fake backend into ``sys.modules``
so player.py's import-time singleton constructs without a real window. That
entry is process-wide and permanent, so leaving it there handed the fake to
every *later* importer too — and ``test_mpvtk_browser`` / ``test_mpvtk_hud``
do ``import mpv as libmpv`` to spawn a real handle.

The symptom was 17 real-mpv tests failing with "renderer never became ready",
15 seconds of timeout each, only when the suite ran as a whole; every module
passed in isolation. That reads exactly like resource contention, and was
recorded as such in MIGRATION.md for a while. It was module poisoning.

These run in subprocesses: checking the contract in-process would be the very
thing the contract forbids.
"""

import os
import subprocess
import sys
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import _harness as h  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))

PROBE = r'''
import sys
sys.path.insert(0, %(here)r)
sys.path.insert(0, %(root)r)
import _harness as h

name = "python_mpv_jsonipc" if h.BACKEND == "jsonipc" else "mpv"
before = sys.modules.get(name)
player = h.import_player_with_fake_mpv()
after = sys.modules.get(name)

print("PLAYER_BOUND_TO_FAKE",
      getattr(getattr(player, "mpv", None), "MPV", None) is h.FakeMPV)
print("SYSMODULES_RESTORED", after is before is not None)
fresh = __import__(name)
print("FRESH_IMPORT_IS_REAL", getattr(fresh, "MPV", None) is not h.FakeMPV)
'''


def _probe(backend):
    env = dict(os.environ, JMS_TEST_BACKEND=backend)
    out = subprocess.run(
        [sys.executable, "-c", PROBE % {"here": HERE, "root": ROOT}],
        capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert out.returncode == 0, out.stderr[-2000:]
    return dict(line.split() for line in out.stdout.split("\n") if line.strip()
                and line.split()[0].isupper())


class TestFakeMpvIsNotLeaked(unittest.TestCase):
    def _check(self, backend):
        got = _probe(backend)
        self.assertEqual(
            got.get("PLAYER_BOUND_TO_FAKE"), "True",
            "player.py must still hold the fake — the state-machine tests "
            "depend on it")
        self.assertEqual(
            got.get("SYSMODULES_RESTORED"), "True",
            "the fake was left in sys.modules; every later `import mpv` gets "
            "it, and the real-mpv tests time out waiting for a renderer")
        self.assertEqual(
            got.get("FRESH_IMPORT_IS_REAL"), "True",
            "a fresh `import mpv` returned the fake")

    @unittest.skipUnless(h.HAVE_MPV_LIB, "libmpv not available")
    def test_libmpv_backend_restores_the_real_module(self):
        self._check("libmpv")

    @unittest.skipUnless(h.HAVE_MPV_JSONIPC, "python-mpv-jsonipc not available")
    def test_jsonipc_backend_restores_the_real_module(self):
        self._check("jsonipc")


if __name__ == "__main__":
    unittest.main()
