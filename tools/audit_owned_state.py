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

**`owners` records the sites that exist, never the ones that may.** A name
listed here that does not touch the attribute pre-authorises the very second
owner this audit exists to catch: the day that method does reach the state,
nothing is reported, because the name is already on the list.
`tests/test_no_second_owner.py` fails on any such name -- `_active_item`
shipped with two.

**A `scope` is a file or a directory, and it has to be as wide as the object
is.** `SyncManager` is one class in one file, so the file is the whole of it.
The browser is not: it is `MpvtkApp` plus a dozen mixin modules sharing one
`self`, so scoping one of its attributes to the file that happens to touch it
today is the same one-of-several-sites mistake this tool exists to find --
the next writer lands in a sibling mixin and nothing reports. Those get
`mpvtk_browser/`. What a directory costs is that the match is by attribute
NAME with no class awareness, so a common name scoped wide collects owners
that are a different object's state. Scope to the file unless the name is
distinctive.

**`obj` is for state that does not hang off `self`.** `mpv.TIMEOUT` is a
module global of the backend library, lowered for teardown and never
restored; it has the same one-writer property and none of the `self.` shape.
"""

import ast
import os


class Owned:
    """One piece of state, and the sites allowed to touch it.

    ``scope`` is a repo-relative path: a ``.py`` file, or a directory whose
    ``.py`` files are all searched. ``obj`` is the name the state hangs off
    -- ``self`` for an attribute, a module alias for a global. Owners are
    spelled ``<path>:<function>``, so a finding names the file rather than
    leaving the reader to guess which module of a mixin stack it came from.
    """

    def __init__(self, scope, attr, owners, why, obj="self"):
        self.scope = scope
        self.attr = attr
        self.obj = obj
        self.owners = frozenset(owners)
        self.why = why

    @property
    def state(self):
        """How to spell the state in a message: ``self._cancelled``."""
        return "%s.%s" % (self.obj, self.attr)


#: The path every owner name is written relative to.
PKG = "jellyfin_mpv_shim"


OWNED = [
    Owned(
        scope="sync/manager.py",
        attr="_cancelled",
        owners=("sync/manager.py:__init__",
                "sync/manager.py:_is_cancelled",
                "sync/manager.py:_drop_cancelled",
                "sync/manager.py:_uncancel",
                "sync/manager.py:_cancel_if_active"),
        why="`_cancelled` is the record that a user's delete is owed, and "
            "acting on it means removing files and a catalog row. Every "
            "reader goes through `_is_cancelled`; the single actor is "
            "`_drop_cancelled`, which re-checks and deletes the row inside "
            "the same critical section as the check.",
    ),
    Owned(
        scope="sync/manager.py",
        attr="_active_item",
        owners=("sync/manager.py:__init__",
                "sync/manager.py:_download",
                "sync/manager.py:_cancel_if_active",
                "sync/manager.py:relocate"),
        why="Which item the worker owns. A second writer means a delete "
            "either yanks files out from under an open write or misses the "
            "in-flight item entirely. A reader wanting "
            "\"what is downloading\" asks the catalog, as `state` does "
            "(`STATUS_DOWNLOADING`), rather than reaching in here.",
    ),
    Owned(
        scope="mpvtk_browser/thumbnails.py",
        attr="_gone",
        owners=("mpvtk_browser/thumbnails.py:__init__",
                "mpvtk_browser/thumbnails.py:is_gone",
                "mpvtk_browser/thumbnails.py:_work"),
        why="The set of artwork keys the server answered for with a "
            "permanent absence, so the browser stops asking. `_work` is the "
            "only writer and `is_gone` the only reader, and that matters "
            "because the set is process-lifetime and keyed by image key "
            "alone -- no server, no user. A second writer, or a reader that "
            "treats it as a cache rather than as a verdict, turns a "
            "transient 401 into artwork that stays blank until restart. "
            "Whether 401/403 belongs in here at all is a live question "
            "(`docs/RISK_MAP_2026-09.md` section 3.5); this entry pins who "
            "may answer it.",
    ),
    Owned(
        scope="mpvtk_browser/",
        attr="_sync_path",
        owners=("mpvtk_browser/app.py:__init__",
                "mpvtk_browser/settings/general.py:_setting_row"),
        why="The download folder the settings form is showing, mirrored off "
            "the widget so the row can read it back. It is never cleared, "
            "and it WINS over the saved setting the visible field was drawn "
            "from -- so a second reader anywhere in the browser is reading a "
            "value the user may have abandoned, and acting on it moves the "
            "download store recursively. Scoped to the whole browser "
            "because it is one `self` across a dozen mixin modules.",
    ),
    Owned(
        scope="mpvtk_browser/",
        attr="_login",
        owners=("mpvtk_browser/app.py:__init__",
                "mpvtk_browser/auth.py:field",
                "mpvtk_browser/auth.py:_use_known_server",
                "mpvtk_browser/auth.py:_quick_connect_to",
                "mpvtk_browser/auth.py:_start_quick_connect",
                "mpvtk_browser/auth.py:_do_login"),
        why="The Add Server form's three fields, `pass` among them, held in "
            "cleartext for as long as the process runs: three sites set it "
            "and none clears it, so the form re-seeds with the last "
            "password typed. Contrast `_pin`, whose one dict IS cleared "
            "(`auth.py` sets `_pin['pin'] = ''`) -- the same rule, written "
            "at one of two sites, which is this repo's recurring shape. "
            "Six owners is a weak scope and it is the honest one: the "
            "check here is that a SEVENTH reader of a cleartext password "
            "has to say so out loud.",
    ),
    Owned(
        scope=".",
        attr="TIMEOUT",
        obj="mpv",
        owners=("player.py:bound_ipc_replies",),
        why="The jsonipc backend's reply wait, a module global of the "
            "library rather than state of ours. `bound_ipc_replies` lowers "
            "it 120s->5s for teardown and nothing restores it -- and its "
            "call sites include the MINIMIZE path, which is not a teardown, "
            "so a long-running session can end up with every IPC reply "
            "bounded at 5s. Scoped to the package: any module can reach a "
            "global, so a second writer is not confined to `player.py`.",
    ),
]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _modules(entry):
    """The repo-relative `.py` files an entry's scope covers, sorted."""
    base = os.path.normpath(os.path.join(ROOT, PKG, entry.scope))
    if base.endswith(".py"):
        return [os.path.relpath(base, os.path.join(ROOT, PKG))]
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(os.path.relpath(os.path.join(dirpath, name),
                                           os.path.join(ROOT, PKG)))
    return sorted(out)


def _sites(entry):
    """Every function reaching the state, as {"path:function": count}."""
    found = {}

    for module in _modules(entry):
        path = os.path.join(ROOT, PKG, module)
        with open(path, encoding="utf-8") as fh:
            tree = ast.parse(fh.read(), filename=path)
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
                        and node.value.id == entry.obj):
                    # The nearest enclosing def, so a closure is charged to
                    # the method that defines it rather than to nothing.
                    where = stack[-1] if stack else "<module>"
                    key = "%s:%s" % (module, where)
                    found[key] = found.get(key, 0) + 1
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
        print("%s (%s) is reached outside its owners: %s"
              % (entry.state, entry.scope, ", ".join(sorted(strays))))
        print("    %s" % entry.why)
    return 1 if findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
