"""Every audit in `tools/` says how much it looked at, and it is never none.

**The class this closes.** An audit reports "clean" two ways that look
identical from outside: it examined the tree and found nothing wrong, or it
examined nothing. The second is the whole of carried-forward C4 -- a guard
whose no-op path is indistinguishable from a pass -- and these tools are
unusually exposed to it, because what they match on is source text. A renamed
parameter, a decorator that moved, an `_ADD` regex over Lua that stopped
matching: each leaves the tool returning `[]` and every test over it green.

It has happened. `tools/audit_build_player_calls.py` was written in one round
to close this class and shipped carrying it: its wrapper rule skipped any
function without a parameter literally named `test`, so a rename walked
through and the audit went on reporting `0 hand over no case`. Two rounds,
three instances, one of them inside the instrument built for it.

**The defence already existed at one site.** `tests/test_no_orphan_fixtures.py`
asserts the `checked` count its audit returns:

    self.assertTrue(checked, "the audit found no file writing a downloads
                              row, which means it stopped matching rather
                              than that the tree is clean")

That is the right answer, written once and applied nowhere else -- a rule at
one site of nine, which is this repository's most-repeated defect shape. This
file is that assertion for every audit at once, rather than nine copies of it.

**What a population is.** The number of things the audit *considered*, not
the number it complained about. Findings are supposed to be zero on a clean
tree; a population that goes to zero means the matcher stopped working. So
the entry for each audit names its widest accessor -- the sites, the doubles,
the call sites -- and deliberately not its `audit()` result.

**Why a registry rather than a convention.** These nine grew separately and
their entry points disagree (`audit()`, `sites()`, `find()`, `undeclared()`,
`audit(path)`), and normalising them would be a refactor of nine working
tools to satisfy a test. One line each is cheaper and says more. The N-of-N
half is `test_every_audit_is_registered`: a new `tools/audit_*.py` fails here
until somebody says what its population is, which is the one moment anybody
is thinking about it.
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
import glob
import importlib
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)


def _lua_bindings(module):
    """Every `add_*_key_binding` the renderer installs.

    The regex is the whole rule: `audit()` returns the ones it finds *and*
    cannot pair with a release, so a regex that stopped matching Lua reports
    no leaked bindings at all.
    """
    path = os.path.join(REPO, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")
    with open(path, encoding="utf-8") as fh:
        return len(module._ADD.findall(fh.read()))


def _handler_lambdas(module):
    """Every widget handler lambda in the package.

    `scan_file` returns findings only, so this walks with the tool's own
    matcher -- which is the thing that has to be proven still matching.
    """
    found = 0
    for dirpath, _dirs, files in os.walk(module.DEFAULT_ROOT):
        for name in sorted(files):
            if not name.endswith(".py"):
                continue
            try:
                with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef):
                    found += sum(1 for _ in module.handler_lambdas(node))
    return found


#: audit module -> how to count what it examined. The comment on each says
#: what one unit is, because "42" in a failure message is not an answer.
POPULATION = {
    # Gateway `_act` target sites.
    "audit_act_targets": lambda m: len(m.sites()),
    # `build_player` call sites and forwarding wrappers. NOT on this branch
    # yet -- it arrives with `todo-mpv-write-guard` at the merge, and this
    # entry goes live then. `test_every_audit_is_registered` is what would
    # have caught it arriving unregistered.
    "audit_build_player_calls": lambda m: m.audit(
        os.path.join(REPO, "tests"))[1],
    # Test doubles standing in for a real collaborator.
    "audit_fake_contracts": lambda m: len(m.doubles()),
    # Key-name literals that must come from the frozen table.
    "audit_frozen_key_literals": lambda m: len(m.sites()),
    # Lua key bindings installed by the renderer.
    "audit_key_bindings": _lua_bindings,
    # Reads of owned state, across every declared entry.
    "audit_owned_state": lambda m: sum(len(m._sites(e)) for e in m.OWNED),
    # Files writing a downloads row.
    "audit_row_fixtures": lambda m: m.audit(os.path.join(REPO, "tests"))[1],
    # Widget handler lambdas that could capture stale state.
    "audit_stale_captures": _handler_lambdas,
    # Narrowing sites a mutation could be built from.
    "audit_vacuous_narrowing": lambda m: len(m.audit()[0]),
    # Raw truth-tests on a video object.
    "audit_video_predicate": lambda m: len(m.find()),
}


def _audits_on_disk():
    return {os.path.basename(p)[:-3]
            for p in glob.glob(os.path.join(REPO, "tools", "audit_*.py"))}


class EveryAuditExaminesSomethingTest(unittest.TestCase):

    def test_every_audit_is_registered(self):
        """The N-of-N half.

        An audit with no entry here is one whose "clean" nobody has ever
        checked is a real clean -- which is the state all nine were in before
        this file, and the state the one built to close this very class
        shipped in.
        """
        missing = sorted(_audits_on_disk() - set(POPULATION))

        self.assertEqual(
            [], missing,
            "these audits do not say how much they examined, so their "
            "'clean' cannot be told from their having stopped matching: %s. "
            "Add a line to POPULATION naming the widest thing each one "
            "considers -- its sites, not its findings." % ", ".join(missing))

    def test_each_audit_examined_something(self):
        """The assertion itself, once for all of them.

        Run against the real tree, because that is the population that has to
        be non-empty: a synthetic fixture would prove the accessor returns a
        number and nothing about whether it still finds this codebase.
        """
        empty, errors = [], []
        for name in sorted(_audits_on_disk()):
            count_fn = POPULATION.get(name)
            if count_fn is None:
                continue        # test_every_audit_is_registered owns this
            try:
                module = importlib.import_module("tools." + name)
                count = count_fn(module)
            except Exception as error:          # noqa: BLE001
                # An accessor that raises is the same failure wearing a
                # different coat: the audit's coverage is unknown, and
                # unknown must not read as fine.
                errors.append("%s: %s: %s"
                              % (name, type(error).__name__, error))
                continue
            if not count:
                empty.append(name)

        self.assertEqual(
            [], errors,
            "these audits could not be asked what they examined:\n  %s"
            % "\n  ".join(errors))
        self.assertEqual(
            [], empty,
            "these audits examined NOTHING, so whatever they report is "
            "vacuous -- they have stopped matching this tree rather than "
            "found it clean: %s" % ", ".join(empty))

    def test_the_counts_are_what_the_registry_claims(self):
        """The control on the case above, and it is not decorative.

        `assertEqual([], empty)` passes if the loop never ran -- an empty
        `_audits_on_disk()`, a glob that stopped matching, a `tools/` that
        moved. That is this file's own no-op path, and it is the exact shape
        the file exists to close. Leaving it unguarded would be the defect
        one level up, for the third time in three rounds.
        """
        found = _audits_on_disk()

        self.assertGreaterEqual(
            len(found), 9,
            "only %d audit tool(s) found under tools/; the glob has stopped "
            "matching and every assertion in this module is now vacuous"
            % len(found))


if __name__ == "__main__":
    unittest.main()
