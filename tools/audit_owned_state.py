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
`tests/test_no_second_owner.py` fails on any such name -- the download
claim shipped with two.

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
        attr="_active",
        owners=("sync/manager.py:__init__",
                # Takes and gives up one worker's claim. Both require
                # `_active_lock` because they sit inside larger critical
                # sections.
                "sync/manager.py:_claim_active",
                "sync/manager.py:_release_active",
                # The two readers, which take the lock themselves. Nothing
                # else may ask: `relocate` wants "is anything live" and
                # `_open_and_run` wants "which items are live", and both
                # used to read the state and get it wrong in their own way
                # while two workers were running.
                "sync/manager.py:_active_ids",
                "sync/manager.py:_cancel_if_active"),
        why="Which item each live worker owns, keyed by generation. A "
            "second writer means a delete either yanks files out from under "
            "an open write or misses the in-flight item entirely -- and it "
            "was one slot, which could not represent the two live workers "
            "`relocate`'s refusal path creates. A reader wanting "
            "\"what is downloading\" asks the catalog, as `state` does "
            "(`STATUS_DOWNLOADING`), rather than reaching in here.",
    ),
    Owned(
        scope="clients.py",
        attr="_server_on_lan",
        owners=("clients.py:__init__",
                # The only writer, and it runs on a probe thread rather than
                # on the connect that asked, so a reader reaching past
                # `server_is_local` is reading state a background worker is
                # writing.
                "clients.py:_finish_lan_probe",
                # Forgets the answer with the credential, and with the
                # profile. The one place all three per-uuid dicts are dropped,
                # so their lifetimes cannot drift apart again.
                "clients.py:_forget_server_state",
                "clients.py:server_is_local"),
        why="Whether a server is on this machine's own network. Three "
            "answers, not two -- absent means nobody has asked -- so a "
            "reader that treats a missing entry as False tells a LAN user "
            "their server is on the internet. Anyone wanting the answer "
            "asks `server_is_local`; anyone wanting it *resolved* must not "
            "reach for the resolver, which is unbounded (see "
            "`utils.resolved_host_is_private`).",
    ),
    Owned(
        scope="clients.py",
        attr="_lan_probe",
        owners=("clients.py:__init__",
                "clients.py:_start_lan_probe",
                "clients.py:_finish_lan_probe",
                "clients.py:_forget_server_state"),
        why="Which locality lookup is outstanding, as (token, address). It "
            "is doing two jobs at once and both break quietly if a fourth "
            "site writes it: presence is what stops N reconnects against a "
            "stuck resolver leaving N threads, and the token is how a slow "
            "answer from an older connect learns it has been superseded.",
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
                "mpvtk_browser/app.py:_drop_abandoned_sync_path",
                "mpvtk_browser/settings/general.py:_setting_row"),
        why="The download folder the settings form is showing, mirrored off "
            "the widget so the row can read it back, and it WINS over the "
            "saved setting the visible field was drawn from -- so a second "
            "reader anywhere in the browser is reading a value the user may "
            "have abandoned, and acting on it moves the download store. "
            "Scoped to the whole browser because it is one `self` across a "
            "dozen mixin modules. It used never to be cleared at all "
            "(`docs/do-not-fix.md` F42); `_drop_abandoned_sync_path` is now "
            "the single clearer and is listed here so it stays single -- a "
            "second one would have to decide the same thing on a different "
            "cadence, which is the shape the entry was written about.",
    ),
    Owned(
        scope="mpvtk_browser/",
        attr="_login",
        owners=("mpvtk_browser/app.py:__init__",
                "mpvtk_browser/auth.py:field",
                "mpvtk_browser/auth.py:_use_known_server",
                "mpvtk_browser/auth.py:_quick_connect_to",
                "mpvtk_browser/auth.py:_start_quick_connect",
                "mpvtk_browser/auth.py:_do_login",
                "mpvtk_browser/auth.py:show_login"),
        why="The Add Server form's three fields, `pass` among them, held in "
            "cleartext for as long as the process runs: the sites that set "
            "it mostly do not clear it, so the form re-seeds with the last "
            "password typed. Contrast `_pin`, whose one dict IS cleared "
            "(`auth.py` sets `_pin['pin'] = ''`) -- the same rule, written "
            "at one of two sites, which is this repo's recurring shape. "
            "`show_login` is the exception and the pattern to copy: opening "
            "the form to re-authenticate a server seeds the address and the "
            "username and blanks `pass`, so that one entrance cannot show a "
            "password carried over from another server's login. "
            "Seven owners is a weak scope and it is the honest one: the "
            "check here is that an EIGHTH reader of a cleartext password "
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


def _rel(path):
    """Repo-relative, spelled with `/` on every platform.

    OWNED spells its owners `sync/manager.py:_uncancel`, so the scan has to
    answer in the same alphabet. `os.path.relpath` hands back backslashes on
    Windows, every entry then matched nothing, and this audit reports that
    as a phantom owner: measured, all six entries "failed" there and none
    here.
    """
    return os.path.relpath(path, os.path.join(ROOT, PKG)).replace(os.sep, "/")


def _modules(entry):
    """The repo-relative `.py` files an entry's scope covers, sorted."""
    base = os.path.normpath(os.path.join(ROOT, PKG, entry.scope))
    if base.endswith(".py"):
        return [_rel(base)]
    out = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                out.append(_rel(os.path.join(dirpath, name)))
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
