"""The integration matrix's own pass/fail accounting.

`run_integration.py` judged a leg purely on its exit status, and every mpvtk
and real-mpv class is decorated with a skipUnless for mpv / ffmpeg / a
display. So a machine missing any of those printed a fully green matrix —
"All legs passed" — having executed zero UI assertions. That is worse than a
red one: it is a confident claim that nothing is wrong, made by a run that
checked nothing.

These cover the accounting, not the tests it runs.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "tests", "integration"))

import run_integration as runner  # noqa: E402


class TestCounts(unittest.TestCase):
    def test_it_reads_the_run_and_skip_totals(self):
        out = "test_a ... ok\nRan 12 tests in 3.4s\n\nOK (skipped=5)\n"
        self.assertEqual(runner._counts(out), (12, 5))

    def test_no_skips_reported_is_zero_not_none(self):
        self.assertEqual(runner._counts("Ran 3 tests in 0.1s\n\nOK\n"), (3, 0))

    def test_output_with_no_summary_is_unknown(self):
        """A leg that crashed before unittest printed anything must not be
        mistaken for one that ran nothing — unknown is not hollow."""
        self.assertEqual(runner._counts("Segmentation fault\n"), (None, None))

    def test_a_failed_run_still_reports_its_totals(self):
        out = "Ran 8 tests in 1s\n\nFAILED (failures=1, skipped=2)\n"
        self.assertEqual(runner._counts(out), (8, 2))


class TestLegStatus(unittest.TestCase):
    def test_a_normal_pass(self):
        text, failed, hollow = runner.leg_status(0, 10, 1)
        self.assertEqual((failed, hollow), (0, 0))
        self.assertIn("9 run, 1 skipped", text)

    def test_a_failure_is_a_failure(self):
        _text, failed, hollow = runner.leg_status(1, 10, 0)
        self.assertEqual((failed, hollow), (1, 0))

    def test_a_leg_that_skipped_everything_is_hollow(self):
        """The case that mattered: rc == 0, so the old runner called it
        PASS and printed 'All legs passed'."""
        text, failed, hollow = runner.leg_status(0, 25, 25)
        self.assertEqual(failed, 0)
        self.assertEqual(hollow, 1)
        self.assertIn("nothing ran", text)

    def test_a_hollow_leg_is_not_double_counted_as_a_failure(self):
        _text, failed, _hollow = runner.leg_status(0, 25, 25)
        self.assertEqual(failed, 0, "hollow must not inflate the fail count")

    def test_an_empty_leg_is_not_hollow(self):
        """Zero tests collected is a different problem (a bad module path)
        and shows up as a non-zero rc; do not also call it hollow."""
        _text, _failed, hollow = runner.leg_status(0, 0, 0)
        self.assertEqual(hollow, 0)

    def test_unknown_counts_are_not_hollow(self):
        text, failed, hollow = runner.leg_status(0, None, None)
        self.assertEqual((failed, hollow), (0, 0))
        self.assertNotIn("run,", text)

    def test_one_real_test_among_skips_is_enough_to_not_be_hollow(self):
        _text, _failed, hollow = runner.leg_status(0, 25, 24)
        self.assertEqual(hollow, 0)


class TestStrictIsWired(unittest.TestCase):
    def test_the_flag_exists(self):
        """--strict is what CI should use; a typo'd flag name would make
        the whole guard silently absent."""
        import subprocess
        out = subprocess.run(
            [sys.executable, runner.__file__, "--help"],
            capture_output=True, text=True, timeout=60).stdout
        self.assertIn("--strict", out)


if __name__ == "__main__":
    unittest.main()
