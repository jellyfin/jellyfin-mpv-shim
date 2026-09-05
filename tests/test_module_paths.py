"""`python3 tests/test_foo.py` must import THIS repo.

Every module in `tests/` ends in a `__main__` block, which invites exactly
that — and run that way `sys.path[0]` is `tests/` and the repo root is on
the path nowhere, so `jellyfin_mpv_shim` resolves to whatever is
**pip-installed**. Silently, and it runs: on the machine this was found on,
the installed copy was old enough to have no `"symbol"`, `"hebrew"` or
`"emoji"` in `pilfont._CANDIDATES`, so a test module about faces reported
twenty-four failures that were entirely about the other package. A stale
install can as easily produce a false *pass*.

`discover` is unaffected — unittest puts the top-level directory on the
path itself — so **no suite run would ever have gone red over this**, which
is why it survived. It is the same defect as F34, which was fixed for the
twenty-four modules in `tests/integration/` and named the class in its own
commit message while repairing one directory of it.

The repair is a `__main__`-guarded `sys.path.insert` above the imports.
Guarded, so that under `discover` it does not execute at all and the suite
sees no change whatsoever.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import ast
import os
import subprocess
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Asked of a module's own preamble, in a fresh interpreter, from a cwd
#: that cannot put the root on the path by accident. The finder answers
#: *which copy would be imported* without importing it — which matters
#: here and not in `tests/integration`: eight of these modules import
#: `player.py`, and importing that opens a real mpv window.
PROBE = r"""
import ast, importlib.machinery, sys

class Watch:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] == "jellyfin_mpv_shim":
            spec = importlib.machinery.PathFinder.find_spec(name, sys.path)
            print("PKG", spec.origin if spec else None)
            raise SystemExit(0)
        return None

sys.meta_path.insert(0, Watch())
sys.argv = [%(path)r]
sys.path.insert(0, %(dirname)r)
src = open(%(path)r, encoding="utf-8").read()
tree = ast.parse(src)
tops = [n for n in tree.body
        if isinstance(n, (ast.ClassDef, ast.FunctionDef,
                          ast.AsyncFunctionDef))]
# Everything above the first class or def -- the imports and the path
# preamble -- without running a test. Cut with ast rather than by
# splitting on "class ", because the first one is often decorated.
stop = (min([tops[0].lineno] + [d.lineno for d in tops[0].decorator_list])
        if tops else len(src.splitlines()) + 1)
head = chr(10).join(src.splitlines()[:stop - 1])
exec(compile(head, %(path)r, "exec"),
     {"__file__": %(path)r, "__name__": "__main__"})
spec = importlib.machinery.PathFinder.find_spec("jellyfin_mpv_shim", sys.path)
print("PKG", spec.origin if spec else None)
"""


def _modules():
    """Modules whose `__main__` block makes the invitation."""
    here = os.path.join(ROOT, "tests")
    for name in sorted(os.listdir(here)):
        if not (name.startswith("test_") and name.endswith(".py")):
            continue
        path = os.path.join(here, name)
        with open(path, encoding="utf-8") as fh:
            if 'if __name__ == "__main__"' in fh.read():
                yield name, path


class TestEveryModuleRunAsAScriptGetsThisTree(unittest.TestCase):
    def test_there_are_modules_to_check(self):
        """The guard on the guard: a listing that finds nothing would make
        the check below vacuous and report a pass."""
        self.assertGreater(len(list(_modules())), 150)

    def test_the_repo_root_is_on_the_path_before_the_package_is_imported(self):
        """Executed, not grepped for.

        Asserted for **every** module rather than the one that was
        noticed. A guard pasted into the module that showed the symptom is
        the one the next round reports: that is precisely what happened
        between F34 and this, with `tests/integration` repaired and the
        other hundred and eighty left.
        """
        expected = os.path.join(ROOT, "jellyfin_mpv_shim")
        modules = list(_modules())

        def check(item):
            name, path = item
            probe = PROBE % {"path": path, "dirname": os.path.dirname(path)}
            out = subprocess.run(
                [sys.executable, "-c", probe], capture_output=True,
                text=True, cwd=tempfile.gettempdir(), timeout=180)
            got = [ln for ln in out.stdout.splitlines()
                   if ln.startswith("PKG ")]
            return name, (got[0][4:] if got else None), out.stderr[-400:]

        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(check, modules))
        for name, origin, err in results:
            with self.subTest(module=name):
                self.assertIsNotNone(
                    origin, "%s did not resolve the package: %s" % (name, err))
                self.assertTrue(
                    origin.startswith(expected),
                    "%s would run against %s, not this tree" % (name, origin))


if __name__ == "__main__":
    unittest.main()
