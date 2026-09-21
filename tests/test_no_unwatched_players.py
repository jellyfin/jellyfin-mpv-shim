"""A built player has a test case behind it, so nothing has to opt in.

Runs `tools/audit_build_player_calls.py`. The reasoning is there; the short
version is that `_harness.build_player` registers the refused-write cleanup on
the case that called it, and the integration suite has no shared base to put
that cleanup in -- so the parameter is the mechanism and this audit is what
keeps it from being a parameter somebody forgets.

This is the half of `mpv_guard` that decides whether the guard is *observed*.
The guard itself works with or without it: nothing shadows either way, and the
warning is logged either way. What a missing case costs is the assertion --
a run against an mpv that lacks a property the shim writes would pass.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tools.audit_build_player_calls import audit  # noqa: E402


class EveryBuiltPlayerIsWatchedTest(unittest.TestCase):
    def test_every_build_player_call_hands_over_its_case(self):
        offenders, checked = audit(
            os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(
            checked, "the audit found no build_player call at all, which "
                     "means it stopped matching rather than that the tree is "
                     "clean")
        self.assertEqual(
            [], offenders,
            "these call sites build a player with no test case behind it, so "
            "nothing asserts what mpv refused during them. Pass `test=self`, "
            "or add the site to ACCEPTED in "
            "tools/audit_build_player_calls.py with the reason:\n  "
            + "\n  ".join("%s: %s" % pair for pair in offenders))


class TheAuditReadsTheCallAndNotTheNameTest(unittest.TestCase):
    """Its own rule, on inputs the tree does not happen to contain.

    The risk in a name-based audit is the opposite of a false alarm: a bare
    `build_player` in this tree is a *different function*
    (`tests/test_syncplay_pause_ignore.py` defines its own, with its own
    stand-in player), so counting those would make the audit fail on
    something it has nothing to say about -- and the escape hatch would then
    be used to silence it, which is how an audit stops meaning anything.
    """

    def _audit(self, source):
        import tempfile

        with tempfile.TemporaryDirectory() as root:
            with open(os.path.join(root, "test_x.py"), "w",
                      encoding="utf-8") as fh:
                fh.write(source)
            return audit(root)

    def test_a_harness_call_without_a_case_is_found(self):
        offenders, checked = self._audit(
            "def t(self):\n    pm = h.build_player(mod)\n")
        self.assertEqual(1, checked)
        self.assertEqual(1, len(offenders))

    def test_a_harness_call_with_a_case_passes(self):
        offenders, checked = self._audit(
            "def t(self):\n    pm = h.build_player(mod, test=self)\n")
        self.assertEqual(1, checked)
        self.assertEqual([], offenders)

    def test_a_local_builder_of_the_same_name_is_not_ours(self):
        offenders, checked = self._audit(
            "def build_player(**kw):\n    return 1\n\n"
            "def t(self):\n    pm = build_player()\n")
        self.assertEqual(0, checked, "a file's own builder was counted")
        self.assertEqual([], offenders)

    def test_a_wrapper_with_a_defaulted_case_is_found(self):
        """The hole this rule was added for: the call inside the wrapper
        hands over a case, so the call-site rule is satisfied, while every
        caller of the wrapper may omit one and get None."""
        offenders, _checked = self._audit(
            "def build(test=None, **kw):\n"
            "    return h.build_player(mod, test=test, **kw)\n")

        self.assertEqual(1, len(offenders),
                         "a defaulted forwarding wrapper was not found")
        self.assertIn("DEFAULTED", offenders[0][1])

    def test_a_wrapper_that_requires_its_case_passes(self):
        """The control. Wrappers are fine -- defaulting the case is not."""
        offenders, _checked = self._audit(
            "def build(test, **kw):\n"
            "    return h.build_player(mod, test=test, **kw)\n")

        self.assertEqual([], offenders)

    def test_a_function_that_defaults_test_but_forwards_nothing_is_ignored(self):
        """`test=None` is an ordinary parameter name. Only forwarding it to
        build_player makes it this audit's business."""
        offenders, _checked = self._audit(
            "def helper(test=None):\n    return test\n")

        self.assertEqual([], offenders)

    def test_a_bare_call_to_the_imported_name_is_ours(self):
        """The drift this leaves room for, closed: import the name instead of
        the module and the call is bare, which a module-alias-only rule would
        never look at."""
        offenders, checked = self._audit(
            "from _harness import build_player\n\n"
            "def t(self):\n    pm = build_player(mod)\n")
        self.assertEqual(1, checked)
        self.assertEqual(1, len(offenders))


if __name__ == "__main__":
    unittest.main()
