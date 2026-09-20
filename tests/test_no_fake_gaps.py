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

# Run as a script, this is what puts the repo root on sys.path -- without
# it `jellyfin_mpv_shim` resolves to whatever is pip-installed. A no-op
# under `discover`; tests/test_module_paths.py is the guard.
if __name__ == "__main__":
    import os
    import sys

    sys.path.insert(0, os.path.dirname(
        os.path.dirname(os.path.abspath(__file__))))

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

    def test_nothing_is_accepted_that_is_not_reached(self):
        """`accepted` excuses a name production reaches. A name it does not
        reach is an excuse issued in advance: the day the code does reach it,
        the audit says nothing, because the name is already on the list --
        which is the gap this whole file exists to fail on.

        The same guard `tests/test_no_stale_captures.py` makes over
        `ACCEPTED`, and `tests/test_no_second_owner.py` over `owners`.
        """
        for pair in audit.PAIRS:
            with self.subTest(pair.name):
                contract = audit.contract_for(pair)
                self.assertTrue(contract, "the extraction found nothing")
                dead = sorted(pair.accepted - contract)
                self.assertFalse(
                    dead,
                    "%s accepts names nothing reaches on it: %s. Drop them; "
                    "re-add one only when production actually reaches it, "
                    "which is the moment somebody has to think about it."
                    % (pair.name, ", ".join(dead)))

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


class DoubleSignaturesTest(unittest.TestCase):
    """No stand-in accepts a call the real method would reject.

    The other half of the same subject, and the half the contract check above
    is blind to: those four doubles all *had* the method. What they had was a
    scope argument with a default where the real store requires one, so a
    production caller that forgot the scope passed the suite and would have
    asked the wrong server in the field.
    """

    def test_no_double_is_more_permissive_than_what_it_stands_for(self):
        loose = audit.signature_findings()
        if loose:
            lines = ["%s:%s (for %s): %s" % (rel, cls, real, "; ".join(notes))
                     for rel, cls, real, notes in loose]
            self.fail("\n".join(lines) +
                      "\n\nSee tools/audit_fake_contracts.py — match the "
                      "signature, or accept it there with a reason.")

    def test_nothing_is_accepted_that_no_longer_differs(self):
        """`ACCEPTED_SIGNATURES` excuses a difference that exists. An entry for
        one that does not is an excuse issued in advance, and the day somebody
        adds the default back the audit says nothing.

        The same guard this file already makes over `accepted`, and
        `tests/test_no_second_owner.py` over `owners`.
        """
        reals = audit._real_methods()
        live = set()
        for rel, node, real, shared in audit.doubles():
            fake = audit._methods(node)
            for name in shared:
                if audit.more_permissive(fake[name], reals[real][name]):
                    live.add((rel, node.name, name))
        dead = sorted(set(audit.ACCEPTED_SIGNATURES) - live)
        self.assertFalse(
            dead,
            "ACCEPTED_SIGNATURES excuses differences that are not there: %s. "
            "Drop them; re-add one only when the double really is more "
            "permissive, which is the moment somebody has to think about it."
            % (dead,))

    def test_the_discovery_still_finds_doubles(self):
        """The guard on the guard, as above: a match rule that finds nothing
        reports every double as faithful. Four is well under what is there and
        well over zero."""
        found = audit.doubles()
        self.assertGreater(
            len(found), 4,
            "the double discovery found %d classes — the real classes in "
            "REALS were renamed, or the match rule stopped matching, and this "
            "check has been passing on an empty set" % len(found))


if __name__ == "__main__":
    unittest.main()
