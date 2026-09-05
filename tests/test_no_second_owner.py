"""State with one owner still has one owner.

Runs ``tools/audit_owned_state.py``. The two measured bugs behind it, and the
``owners`` convention for answering a finding, are in that file's docstring.

A finding here is not automatically a bug: it says a new site has started
touching state that was deliberately funnelled through one accessor. Route it
through the accessor, or add the site to ``owners`` with the reason it owns
the state too.
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

import audit_owned_state as audit    # noqa: E402


class OwnedStateTest(unittest.TestCase):
    def test_nothing_reaches_owned_state_from_outside(self):
        findings = audit.audit()
        if findings:
            lines = []
            for entry, strays in findings:
                lines.append("%s: self.%s reached in %s"
                             % (entry.module, entry.attr,
                                ", ".join(sorted(strays))))
                lines.append("    " + entry.why)
            self.fail("\n".join(lines) +
                      "\n\nSee tools/audit_owned_state.py — route it through "
                      "the accessor, or add the site to `owners` with a "
                      "reason.")

    def test_every_entry_still_finds_its_owners(self):
        """The guard on the guard. A renamed attribute finds nothing
        anywhere, reports a clean tree, and checks nothing ever again."""
        for entry in audit.OWNED:
            with self.subTest("%s.%s" % (entry.module, entry.attr)):
                sites = audit._sites(entry)
                self.assertTrue(
                    sites,
                    "self.%s is not touched anywhere in %s any more — it was "
                    "renamed or removed, and this entry has been checking "
                    "nothing" % (entry.attr, entry.module))
                phantom = sorted(entry.owners - set(sites))
                self.assertFalse(
                    phantom,
                    "declared owners of self.%s that do not touch it: %r. An "
                    "owner is a record of a site that exists, not permission "
                    "for one that might: left standing, it pre-authorises "
                    "exactly the second owner this audit is for, and the "
                    "audit stays silent because the name is already listed. "
                    "Real sites: %r"
                    % (entry.attr, phantom, sorted(sites)))


if __name__ == "__main__":
    unittest.main()
