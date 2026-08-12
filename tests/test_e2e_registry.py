"""Every e2e module is registered with the runner.

`tests/e2e/` has no `__init__.py` — deliberately, so the fast suite never
recurses into it and never needs a server. The price is that
`run_e2e.py`'s MODULES list is hand-maintained, and a module that is not
on it does not run, does not fail, and does not report itself missing.

That has now happened twice. `run_e2e.py` documents the first
(`test_auto_download`, "landed after the runner's list was last touched
and never added to it, so it ran only when somebody named it by hand"),
and the lesson was written down instead of enforced — so
`test_batch4_contracts` (380 lines, 20 tests, the server-truth behind a
whole batch) repeated it, and was *also* listed in `tests/e2e/README.md`,
which made it look registered.

A skipped test says "skipped". An unregistered one says nothing at all.
"""

import os
import re
import unittest

E2E = os.path.join(os.path.dirname(os.path.abspath(__file__)), "e2e")

#: Modules deliberately kept out of the runner, each with the reason.
#: An entry here is a decision; anything else is an oversight.
UNREGISTERED = {
    # Needs mpv builds from ~/Desktop/mpv-matrix that no CI has, and
    # cannot join the discovered suite anyway: discovery imports modules
    # that import `player`, putting a live libmpv in the process, and
    # spawning an mpv binary out of that segfaults at teardown often
    # enough to be flaky. Its own docstring says so.
    "tests.e2e.test_mpv_matrix",
}


def _runner_modules():
    path = os.path.join(E2E, "run_e2e.py")
    with open(path, encoding="utf-8") as fh:
        return set(re.findall(r'"(tests\.e2e\.test_[a-z0-9_]+)"', fh.read()))


def _modules_on_disk():
    return {"tests.e2e." + f[:-3] for f in os.listdir(E2E)
            if f.startswith("test_") and f.endswith(".py")}


class E2ERegistryTest(unittest.TestCase):
    def test_every_module_is_registered_or_excused(self):
        missing = sorted(_modules_on_disk() - _runner_modules() - UNREGISTERED)
        self.assertEqual(
            missing, [],
            "e2e modules that exist and never run. Add them to MODULES in "
            "tests/e2e/run_e2e.py, or to UNREGISTERED here with the reason.")

    def test_the_runner_names_nothing_that_is_gone(self):
        """The other direction: a renamed or deleted module left on the
        list fails the whole run with an import error, which at least is
        loud -- but naming it here says which."""
        stale = sorted(_runner_modules() - _modules_on_disk())
        self.assertEqual(stale, [])

    def test_the_excuse_list_does_not_outlive_its_modules(self):
        """An entry for a module that no longer exists would silently
        excuse a future module that happens to take its name."""
        gone = sorted(UNREGISTERED - _modules_on_disk())
        self.assertEqual(gone, [])
