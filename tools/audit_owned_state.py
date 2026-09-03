#!/usr/bin/env python3
"""Find state that has grown a second owner.

The recurring defect in this tree is not a missing guard. It is *a guard
applied to one of several symmetric sites* -- the rule gets written out again
at each place that needs it, the copies drift, and the review that follows
names one site, so the fix goes to that site and leaves the siblings.

Measured, twice, on `SyncManager`:

* "the delete wins" was written out at three places in `_download` with three
  different combinations of check, discard, remove-files, delete-row and
  notify. The one that skipped the re-check deleted a download the user had
  asked for again in the meantime. The commit that fixed the first two named
  this exact class in its own message.
* The catalog restore was written twice, once per emergency, and the second
  copy was missing the staging and the `-wal` handling -- so a full disk made
  the loss permanent and a stale WAL replayed the pages being recovered.

Counting the sites is the check that would have caught both, and it is the
one nobody performs while holding a finding that names a single line. So it
is a test rather than a paragraph: a piece of state is declared here with the
methods allowed to touch it, and anything else that reaches for it fails the
suite.

**This is a scope check, not a correctness check.** It says a new caller has
appeared, not that it is wrong. A finding is answered either by routing
through the accessor that already exists, or -- if the new site genuinely
owns the state too -- by adding it to `owners` with the reason.
"""

import ast
import os


class Owned:
    def __init__(self, module, attr, owners, why):
        self.module = module
        self.attr = attr
        self.owners = frozenset(owners)
        self.why = why


OWNED = [
    Owned(
        module="jellyfin_mpv_shim/sync/manager.py",
        attr="_cancelled",
        owners=("__init__", "_is_cancelled", "_drop_cancelled", "_uncancel",
                "_cancel_if_active"),
        why="`_cancelled` is the record that a user's delete is owed, and "
            "acting on it means removing files and a catalog row. Every "
            "reader goes through `_is_cancelled`; the single actor is "
            "`_drop_cancelled`, which re-checks and deletes the row inside "
            "the same critical section as the check.",
    ),
    Owned(
        module="jellyfin_mpv_shim/sync/manager.py",
        attr="_active_item",
        owners=("__init__", "_download", "_cancel_if_active", "relocate",
                "state", "stop"),
        why="Which item the worker owns. A second writer means a delete "
            "either yanks files out from under an open write or misses the "
            "in-flight item entirely.",
    ),
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _sites(entry):
    """Every function that reaches `self.<attr>`, as {function name: count}."""
    path = os.path.join(ROOT, entry.module)
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = {}
    stack = []

    class Walk(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_Attribute(self, node):
            if (node.attr == entry.attr
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "self"):
                # The nearest enclosing def, so a closure is charged to the
                # method that defines it rather than to nothing.
                where = stack[-1] if stack else "<module>"
                found[where] = found.get(where, 0) + 1
            self.generic_visit(node)

    Walk().visit(tree)
    return found


def audit():
    """[(entry, {function: count})] for functions outside `owners`."""
    findings = []
    for entry in OWNED:
        strays = {name: n for name, n in _sites(entry).items()
                  if name not in entry.owners}
        if strays:
            findings.append((entry, strays))
    return findings


def main():
    findings = audit()
    for entry, strays in findings:
        print("%s: self.%s is reached outside its owners: %s"
              % (entry.module, entry.attr,
                 ", ".join(sorted(strays))))
        print("    %s" % entry.why)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
