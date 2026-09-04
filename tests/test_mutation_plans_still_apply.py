"""Every shipped mutation plan still names real code.

A mutation whose pattern no longer matches is not a mutation. It changes
nothing, the suite stays green, and the tool reports it exactly as it would
report a fix with no test behind it -- so a plan left to rot turns into a
page of claims that quietly stopped being checked.

This is the cheap half (patterns resolve, and to one place); running the
plans is the expensive half and is deliberately not done here:

    xvfb-run -a python3 tools/mutate_round.py tools/mutation_plans/<plan>.py

If this fails, the code it points at was refactored. Rewrite the pattern to
whatever now expresses the same broken behaviour, or drop the entry and say
in the commit message why that claim no longer needs evidence. Do not delete
the plan to make it pass.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import glob
import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import mutate_round    # noqa: E402

PLANS = sorted(glob.glob(os.path.join(ROOT, "tools", "mutation_plans", "*.py")))


class MutationPlansTest(unittest.TestCase):
    def test_there_is_at_least_one_plan(self):
        """Otherwise this module passes by having nothing to check."""
        self.assertTrue(PLANS, "tools/mutation_plans/ is empty")

    def test_every_pattern_identifies_exactly_one_place(self):
        for plan in PLANS:
            with self.subTest(os.path.basename(plan)):
                mutations, _select = mutate_round.load_plan(plan)
                self.assertTrue(mutations, "the plan is empty")
                bad = mutate_round.check_patterns(mutations)
                if bad:
                    self.fail("\n".join(
                        "  %s -> %s in %s" % (
                            name,
                            "file missing" if count < 0 else
                            "matches %d times" % count, rel)
                        for name, rel, count in bad)
                        + "\n\nSee this module's docstring.")

    def test_a_mutation_is_a_change(self):
        """`old` equal to `new` runs the suite unmodified and calls the
        result evidence."""
        for plan in PLANS:
            mutations, _select = mutate_round.load_plan(plan)
            for name, _rel, old, new in mutations:
                with self.subTest("%s: %s" % (os.path.basename(plan), name)):
                    self.assertNotEqual(old, new)


class CheckPatternsTest(unittest.TestCase):
    """The tool's own fail-fast, since a survivor and a typo print the same
    thing without it."""

    def test_a_pattern_matching_nothing_is_reported(self):
        bad = mutate_round.check_patterns(
            [("x", "tools/mutate_round.py", "no such text anywhere", "y")])
        self.assertEqual([(n, r, c) for n, r, c in bad][0][2], 0)

    def test_a_pattern_matching_twice_is_reported(self):
        bad = mutate_round.check_patterns(
            [("x", "tools/mutate_round.py", "def ", "y")])
        self.assertEqual(len(bad), 1)
        self.assertGreater(bad[0][2], 1)

    def test_a_missing_file_is_reported(self):
        bad = mutate_round.check_patterns(
            [("x", "no/such/file.py", "anything", "y")])
        self.assertEqual(bad[0][2], -1)

    def test_a_unique_pattern_is_accepted(self):
        self.assertEqual(
            mutate_round.check_patterns(
                [("x", "tools/mutate_round.py",
                  "def check_patterns(mutations):", "y")]),
            [])


if __name__ == "__main__":
    unittest.main()
