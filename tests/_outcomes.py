"""`python -m unittest`, plus a per-test record of what happened.

Run it exactly as you would ``python -m unittest`` (module names, ``-v``,
``-k``, ``discover`` and all): ``python -m tests._outcomes <args>``. When
``JMS_TEST_OUTCOMES`` names a file, every test's id, outcome and skip reason
is appended to it as one JSON object per line.

The runners need this because ``Ran N tests ... OK (skipped=K)`` cannot say
*which* tests skipped. A leg where one critical test skipped and the rest
passed reads exactly like one where nothing skipped that mattered. The
manifest check (tests/_manifest.py) compares these records against what each
platform is expected to run.
"""

import json
import os
import sys
import unittest

OUTCOMES_ENV = "JMS_TEST_OUTCOMES"


class RecordingResult(unittest.TextTestResult):
    """Writes one line per finished test. Opened in append mode per write,
    so a leg that dies mid-run still leaves the records of what did run."""

    path = None

    def _record(self, test, outcome, reason=""):
        if not self.path:
            return
        # A setUpClass/setUpModule failure arrives as an _ErrorHolder, whose
        # id() names the class or module rather than a test. Record it under
        # that id: a class whose setup failed ran none of its tests, and the
        # manifest check reports those tests missing.
        line = json.dumps({"id": test.id(), "outcome": outcome,
                           "reason": str(reason)})
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def addSuccess(self, test):
        super().addSuccess(test)
        self._record(test, "pass")

    def addFailure(self, test, err):
        super().addFailure(test, err)
        self._record(test, "fail")

    def addError(self, test, err):
        super().addError(test, err)
        self._record(test, "error")

    def addSkip(self, test, reason):
        super().addSkip(test, reason)
        self._record(test, "skip", reason)

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        self._record(test, "xfail")

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        self._record(test, "xpass")

    def addSubTest(self, test, subtest, err):
        # A failing subtest fails its test; the test's own add* call still
        # follows. Record the subtest only when it failed, so a pass stays one
        # line per test.
        super().addSubTest(test, subtest, err)
        if err is not None:
            failed = issubclass(err[0], test.failureException)
            self._record(subtest, "fail" if failed else "error")


class RecordingRunner(unittest.TextTestRunner):
    resultclass = RecordingResult


def main(argv=None):
    RecordingResult.path = os.environ.get(OUTCOMES_ENV) or None
    # A class, not an instance: unittest.main only passes -v/-f/-b through
    # to a runner it constructs itself.
    runner = RecordingRunner
    argv = list(sys.argv if argv is None else argv)
    # "python -m unittest" as the program name, so a usage line reads the
    # way the command it stands in for does.
    argv[0] = "python -m unittest"
    unittest.main(module=None, argv=argv, testRunner=runner)


if __name__ == "__main__":
    main()
