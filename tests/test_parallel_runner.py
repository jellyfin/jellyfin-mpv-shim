"""``tools/run_tests_parallel.py`` must run the same tests as discover.

The whole value of the parallel runner is that its result means what
``python3 -m unittest discover tests`` means. The way that quietly stops
being true is not a crash -- it is a module the runner never launches, which
looks exactly like a module whose tests all passed.

So this pins the one invariant a runner cannot check about itself: the set
of modules it would run is the set discover would collect. It does not run
anything (that would be the suite running itself), and it deliberately
compares FILENAMES rather than counts -- a count is the thing that silently
agrees when two modules are swapped for each other.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _runner():
    """``tools/run_tests_parallel.py`` as a module, without installing it.

    It lives in ``tools/`` rather than in a package, so there is nothing to
    import normally -- and adding it to the path permanently would put
    ``tools/`` ahead of the repo root, which is the exact shadowing hazard
    the runner's own docstring is about.
    """
    import importlib.util

    path = os.path.join(ROOT, "tools", "run_tests_parallel.py")
    spec = importlib.util.spec_from_file_location("_jms_par_runner", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class ParallelRunnerCoversTheSuiteTest(unittest.TestCase):
    #: unittest's default. The runner matches on ``test_``; if a module ever
    #: lands that discover would take and the runner would not, that is a
    #: silently skipped module.
    DISCOVER_PATTERN = "test*.py"

    def _discovered(self):
        import fnmatch

        return {n for n in os.listdir(os.path.join(ROOT, "tests"))
                if fnmatch.fnmatch(n, self.DISCOVER_PATTERN)}

    def test_it_would_run_every_module_discover_collects(self):
        missing = self._discovered() - set(_runner()._modules())
        self.assertEqual(
            missing, set(),
            "tools/run_tests_parallel.py would skip these, and a skipped "
            "module reports as a passing one:\n  " + "\n  ".join(sorted(missing)))

    def test_it_would_not_invent_a_module(self):
        """The mirror image: a file the runner launches and discover does
        not is a worker that fails on an empty suite, or worse, runs
        something the real suite never does."""
        extra = set(_runner()._modules()) - self._discovered()
        self.assertEqual(extra, set(),
                         "not part of `discover tests`: %s" % sorted(extra))

    def test_the_module_list_is_not_empty(self):
        """Guard on the guard: both assertions above pass against a runner
        that collects nothing at all."""
        self.assertGreater(len(_runner()._modules()), 100)


if __name__ == "__main__":
    unittest.main()
