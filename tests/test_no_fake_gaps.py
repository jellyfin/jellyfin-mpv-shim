"""Every stand-in covers what production code reaches on it.

Runs ``tools/audit_fake_contracts.py``. The reasoning, the four bugs that
motivated it and the ``accepted`` convention live in that file's docstring;
this is the hook that makes a new gap fail the suite instead of waiting to be
noticed.

A finding here is not automatically a bug. It means production code reaches
for something a stand-in does not have, which is either a path that raises
where no test is looking, or a property that has nowhere to live. Model the
field, or add it to that pair's ``accepted`` with the reason it cannot
matter.
"""

import os
import sys
import unittest

sys.argv = [sys.argv[0]]      # importing the shim reaches args.get_args()

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import audit_fake_contracts as audit    # noqa: E402


class FakeContractsTest(unittest.TestCase):
    def test_no_stand_in_is_missing_what_is_reached_on_it(self):
        findings = audit.audit()
        if findings:
            lines = []
            for pair, missing in findings:
                lines.append("%s (%s) is missing: %s"
                             % (pair.name, pair.fake, ", ".join(missing)))
            self.fail("\n".join(lines) +
                      "\n\nSee tools/audit_fake_contracts.py — model the "
                      "field, or accept it there with a reason.")

    def test_every_pair_still_extracts_a_contract(self):
        """The guard on the guard. An extraction that finds nothing reports
        every stand-in as perfect, which is how a check like this dies."""
        for pair in audit.PAIRS:
            with self.subTest(pair.name):
                self.assertGreater(
                    len(audit.contract_for(pair)), 2,
                    "nothing is reached through %r any more — the collaborator "
                    "was renamed or is now held under a different name, and "
                    "this pair has been checking nothing" % (pair.reads,))


if __name__ == "__main__":
    unittest.main()
