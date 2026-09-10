"""The release gate's own arithmetic, and the trap it exists to avoid.

`tools/selffix_rate.py` is run by hand before a tag, not by this suite --
it walks git history and answers with a number to read rather than a
threshold to assert (docs/testing.md section 7). What is testable is its
pure half, and one thing that is not optional: the postmortem's first run
of this measurement reported a clean 0.0% because every `git blame` call
had errored to empty. An instrument that reports "no rot" when it has
measured nothing is worse than no instrument.
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
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import selffix_rate as gate    # noqa: E402


class BlameGuardTest(unittest.TestCase):
    def test_measuring_nothing_is_not_a_clean_release(self):
        """The 0.0% trap, as an assertion.

        `git blame --no-color` is an ambiguous option; git refuses it and
        the postmortem's first pass read every empty result as "no line in
        this hunk was written by this window". 187 commits of that is a
        very convincing zero.
        """
        with self.assertRaises(SystemExit) as caught:
            gate.classify([("0" * 40, "2026-01-01", "a commit")])
        self.assertIn("0.0%", str(caught.exception))

    def test_an_empty_window_is_not_measured_at_all(self):
        """No commits is a different answer from no blame, and it must not
        take the failure path -- a branch with nothing on it is fine."""
        self.assertEqual([], gate.classify([]))


class ProseFilterTest(unittest.TestCase):
    def test_a_docstring_is_an_ast_node(self):
        """Which is the bug the first version of the tool had: a bare
        `ast.dump` comparison calls a docstring-only change a behaviour
        change, and the tool then disagreed with the postmortem by exactly
        the commits the postmortem had proved prose-only."""
        before = 'def f():\n    """One thing."""\n    return 1\n'
        after = 'def f():\n    """Another, longer thing entirely."""\n    return 1\n'
        self.assertNotEqual(ast.dump(ast.parse(before)),
                            ast.dump(ast.parse(after)))
        self.assertEqual(
            ast.dump(gate._strip_docstrings(ast.parse(before))),
            ast.dump(gate._strip_docstrings(ast.parse(after))))

    def test_a_real_change_survives_the_stripping(self):
        """The other direction, or the filter excuses real commits."""
        before = 'def f():\n    """Doc."""\n    return 1\n'
        after = 'def f():\n    """Doc."""\n    return 2\n'
        self.assertNotEqual(
            ast.dump(gate._strip_docstrings(ast.parse(before))),
            ast.dump(gate._strip_docstrings(ast.parse(after))))

    def test_a_module_docstring_is_stripped_too(self):
        before = '"""Module."""\nX = 1\n'
        after = '"""Module, rewritten."""\nX = 1\n'
        self.assertEqual(
            ast.dump(gate._strip_docstrings(ast.parse(before))),
            ast.dump(gate._strip_docstrings(ast.parse(after))))

    def test_a_bare_string_statement_is_not_taken_for_a_docstring(self):
        """Only the FIRST statement of a module, class or def is one. A
        string used as a statement elsewhere is code, however odd, and
        rewriting it is a change the filter must not excuse.

        (The first draft of this asserted that stripping the same source
        twice gave the same answer, which is true of any function at all.)
        """
        before = 'def f():\n    x = 1\n    "a marker"\n    return x\n'
        after = 'def f():\n    x = 1\n    "a different marker"\n    return x\n'
        self.assertNotEqual(
            ast.dump(gate._strip_docstrings(ast.parse(before))),
            ast.dump(gate._strip_docstrings(ast.parse(after))),
            "a string statement in the body was stripped as a docstring")


class GateArithmeticTest(unittest.TestCase):
    def test_the_rule_compares_opening_and_trailing(self):
        rising = [("d%d" % i, n, 10, 0.0) for i, n in
                  enumerate([1, 1, 1, 8, 8, 8])]
        ok, opening, trailing = gate.gate(rising)
        self.assertFalse(ok)
        self.assertAlmostEqual(10.0, opening)
        self.assertAlmostEqual(80.0, trailing)

    def test_a_settling_window_passes(self):
        falling = [("d%d" % i, n, 10, 0.0) for i, n in
                   enumerate([8, 8, 8, 1, 1, 1])]
        ok, _opening, _trailing = gate.gate(falling)
        self.assertTrue(ok)

    def test_it_is_a_rate_over_commits_not_over_days(self):
        """A day with one commit must not weigh the same as a day with
        thirty, or a quiet Sunday swings the release decision.

        The trailing window here is one commit that IS a self-fix, thirty
        that are not, and one more that is not. Averaging the three days'
        rates gives 33.3% and would fail the gate; weighting by commits
        gives 3.1% and passes, which is the honest reading of a window
        where 1 of 32 commits repaired itself.
        """
        table = [("a", 1, 10, 10.0), ("b", 1, 10, 10.0), ("c", 1, 10, 10.0),
                 ("d", 1, 1, 100.0), ("e", 0, 30, 0.0), ("f", 0, 1, 0.0)]
        ok, opening, trailing = gate.gate(table)
        self.assertAlmostEqual(10.0, opening)
        self.assertAlmostEqual(100.0 / 32, trailing)
        self.assertTrue(ok, "a 30-commit clean day was outvoted by a "
                            "one-commit day")

    def test_too_short_a_window_answers_none_rather_than_guessing(self):
        self.assertEqual((None, None, None),
                         gate.gate([("a", 1, 1, 100.0)]))


if __name__ == "__main__":
    unittest.main()
