#!/usr/bin/env python3
"""Find test fixtures that build a downloads row with no content server.

A downloads row whose `content_server_id` is unset is an **orphan**, and an
orphan is a distinct contract: it plays, it records playstate locally under
`(@none, @none)`, and it never syncs in either direction
(docs/offline-sync.md section 1). So a fixture that omits the
column is not merely incomplete -- it silently moves every filing assertion
built on it onto the orphan path, under a test name that says otherwise.

Measured when this was written: sixteen test files wrote a real downloads
row and **seven** wrote it with no content server, including one file whose
twenty-two userdata writes all landed on the orphan key while the suite
stayed green. Pass/fail could not see it, because a write and its read were
re-keyed together.

**Why this cannot be `tools/audit_fake_contracts.py`.** That audit asks which
field of the real object a fake *omitted*, and the worst offender here omits
nothing: it builds `{c: None for c in COLUMNS}`, naming every column and
populating six. A field that is structurally present and permanently None is
invisible to an absence check, which is the whole reason this file exists.

Deliberately coarse -- per file, not per row. A file that builds rows both
ways passes on the strength of the homed one; the finer check is the physical
key probe in the plan's batch 0, which is a measurement rather than a lint.

A finding is not automatically a bug: a test that means to exercise the
orphan path is legitimate and belongs in ACCEPTED with its reason.
"""
import ast
import os
import sys

#: This file is `tools/`, so the repo root is its grandparent. ACCEPTED's
#: keys are repo-relative and the lookup normalises to that.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Files that build a downloads row with no content server **on purpose**,
#: with the reason. A bare entry is not acceptable -- say which half of C3's
#: table the file pins, so the next reader can tell a decision from a gap.
ACCEPTED = {
    # The only fixture that starts from the **shipped** schema rather than a
    # branch intermediate: `content_server_id` is the column the migration
    # ADDS, so a row carrying one could not exist before it runs and the
    # unhomed row is the input, not an oversight. It pins the upgrade path
    # that deleted every queued offline playstate entry -- the migration's
    # resolver answered None for every login because nothing had loaded them.
    "tests/test_migration_needs_credentials.py":
        "builds a pre-migration row on purpose: content_server_id is what the "
        "migration adds, so the fixture cannot set it",
}


def _column_names_used(tree):
    """Every column name the module actually names in executable code.

    Three spellings, and the third is why this is not a two-liner: a dict
    key, a subscript **assigned to**, and a **keyword argument** to a row
    helper that ends in `row.update(kw)`. Leaving keywords out reported the
    window's only fail-first module as unhomed, which it is not.

    **Only spellings that set a value count.** Every subscript used to,
    including a read -- so a module that built ten orphan rows and then
    asserted `row["content_server_id"] is None` once satisfied an audit
    whose entire subject is whether its rows *carry* the column. A read is
    the most likely thing to appear in exactly the file this is checking,
    which made the hole worst where the audit mattered most.

    AST rather than a text search, so a column named only in a comment or a
    docstring cannot satisfy the check -- which is the failure mode this
    audit is about, and a text search would have the opposite bug.
    """
    found = set()

    def _subscript_key(node):
        if not isinstance(node, ast.Subscript):
            return None
        idx = node.slice
        if isinstance(idx, ast.Constant) and isinstance(idx.value, str):
            return idx.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            for key in node.keys:
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    found.add(key.value)
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = (node.targets if isinstance(node, ast.Assign)
                       else [node.target])
            for target in targets:
                for sub in ast.walk(target):
                    key = _subscript_key(sub)
                    if key:
                        found.add(key)
        elif isinstance(node, ast.Call):
            for kw in node.keywords:
                if kw.arg:
                    found.add(kw.arg)
    return found


def _writes_a_download_row(tree):
    """Whether this module upserts into the downloads table.

    Two shapes: `<something>.upsert(...)`, which is `SyncDB.upsert`'s only
    spelling, and raw SQL for the tests that build a catalog by hand.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "upsert":
                return True
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if "INSERT INTO downloads" in node.value:
                return True
    return False


def audit(root="tests"):
    """(offenders, checked) -- offenders as (path, reason) pairs."""
    offenders, checked = [], 0
    for base, _dirs, names in os.walk(root):
        for name in sorted(names):
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            try:
                with open(path, encoding="utf-8") as fh:
                    tree = ast.parse(fh.read())
            except (OSError, SyntaxError):
                continue
            if not _writes_a_download_row(tree):
                continue
            checked += 1
            # Repo-relative, **whatever root the caller passed.** The test
            # passes an absolute one (tests/test_no_orphan_fixtures.py), so a
            # bare `path` made every key in ACCEPTED unmatchable and the
            # escape hatch this module documents could never fire. It went
            # unnoticed because the dict was empty.
            rel = os.path.relpath(os.path.abspath(path), _REPO).replace(
                os.sep, "/")
            if rel in ACCEPTED:
                continue
            if "content_server_id" not in _column_names_used(tree):
                offenders.append(
                    (rel, "builds a downloads row but never sets "
                          "content_server_id, so every row it writes is an "
                          "orphan"))
    return offenders, checked


if __name__ == "__main__":
    bad, total = audit()
    for path, why in bad:
        print("%s: %s" % (path, why))
    print("checked %d file(s) that write a downloads row; %d unhomed"
          % (total, len(bad)))
    sys.exit(1 if bad else 0)
