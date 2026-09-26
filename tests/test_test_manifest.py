"""The release-run manifest check (tests/_manifest.py).

Every way a run can look green while not having run what it should: a
test that skipped where it must run, a skip nobody approved, a test that
was never reported (uncollected, or its class setup failed), a new test the
manifest has never seen, and a leg that reported nothing at all.
"""

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

import os
import tempfile
import unittest

from tests import _manifest as m

LEG = "whole suite [libmpv]"


def rec(tid, outcome="pass", reason=""):
    return {"id": tid, "outcome": outcome, "reason": reason}


class CheckTest(unittest.TestCase):
    def test_a_run_matching_the_manifest_is_clean(self):
        manifest = {(LEG, "a.T.t1"): ("pass", ""),
                    (LEG, "a.T.t2"): ("skip", "H8: fixture absent")}
        legs = {LEG: [rec("a.T.t1"), rec("a.T.t2", "skip", "no uosc")]}
        self.assertEqual([], m.check(manifest, legs))

    def test_a_skip_where_the_test_must_run_fails(self):
        manifest = {(LEG, "a.T.t1"): ("pass", "")}
        legs = {LEG: [rec("a.T.t1", "skip", "no mpv")]}
        problems = m.check(manifest, legs)
        self.assertEqual(1, len(problems))
        self.assertIn("skipped unexpectedly", problems[0])

    def test_an_unapproved_skip_fails(self):
        manifest = {(LEG, "a.T.t1"): ("skip", "UNAPPROVED: no mpv")}
        legs = {LEG: [rec("a.T.t1", "skip", "no mpv")]}
        self.assertIn("no approved reason", m.check(manifest, legs)[0])

    def test_a_test_that_never_reported_fails(self):
        """Uncollected, or its setUpClass failed: either way it ran nothing."""
        manifest = {(LEG, "a.T.t1"): ("pass", ""),
                    (LEG, "a.T.t2"): ("pass", "")}
        legs = {LEG: [rec("a.T.t1")]}
        self.assertIn("a.T.t2 never reported", m.check(manifest, legs)[0])

    def test_a_test_the_manifest_has_never_seen_fails(self):
        legs = {LEG: [rec("a.T.new")]}
        self.assertIn("not in the manifest", m.check({}, legs)[0])

    def test_a_leg_that_reported_nothing_fails(self):
        manifest = {(LEG, "a.T.t1"): ("pass", "")}
        self.assertIn("no outcomes", m.check(manifest, {LEG: []})[0])

    def test_a_failure_is_named(self):
        manifest = {(LEG, "a.T.t1"): ("pass", "")}
        legs = {LEG: [rec("a.T.t1", "fail")]}
        self.assertIn("FAIL", m.check(manifest, legs)[0])

    def test_legs_the_run_did_not_attempt_are_not_checked(self):
        """--backend libmpv runs no jsonipc legs; that is a choice, not a
        missing test."""
        manifest = {(LEG, "a.T.t1"): ("pass", ""),
                    ("whole suite [jsonipc]", "a.T.t1"): ("pass", "")}
        self.assertEqual([], m.check(manifest, {LEG: [rec("a.T.t1")]}))


class UpdateTest(unittest.TestCase):
    def test_a_new_skip_arrives_unapproved(self):
        new = m.update({}, {LEG: [rec("a.T.t1", "skip", "no  uosc\nhere")]})
        self.assertEqual(("skip", "UNAPPROVED: no uosc here"),
                         new[(LEG, "a.T.t1")])

    def test_a_reviewed_reason_survives_an_update(self):
        old = {(LEG, "a.T.t1"): ("skip", "H8: fixture absent")}
        new = m.update(old, {LEG: [rec("a.T.t1", "skip", "no uosc")]})
        self.assertEqual(("skip", "H8: fixture absent"), new[(LEG, "a.T.t1")])

    def test_a_removed_test_leaves_the_legs_that_ran(self):
        old = {(LEG, "a.T.gone"): ("pass", ""),
               ("other leg", "a.T.kept"): ("pass", "")}
        new = m.update(old, {LEG: [rec("a.T.t1")]})
        self.assertNotIn((LEG, "a.T.gone"), new)
        self.assertIn(("other leg", "a.T.kept"), new)
        self.assertEqual(("pass", ""), new[(LEG, "a.T.t1")])

    def test_it_round_trips_through_the_file(self):
        manifest = {(LEG, "a.T.t1"): ("pass", ""),
                    (LEG, "a.T.t2"): ("skip", "H8: fixture absent")}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.tsv")
            m.write(path, manifest)
            self.assertEqual(manifest, m.load(path))

    def test_a_malformed_line_is_an_error_not_a_skip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "x.tsv")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("leg\ttest\tmaybe\t\n")
            with self.assertRaises(ValueError):
                m.load(path)


if __name__ == "__main__":
    unittest.main()
