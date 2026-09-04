#!/usr/bin/env python3
"""Find stand-ins that omit the thing they stand in for.

A fake that implements a *subset* of what production code reaches for does
not leave a path untested. It makes the path raise where nobody is looking,
or -- worse and more common -- it makes the path unreachable while reporting
a pass, because the property the test is named after has no field to live in.
Every one of these has shipped here:

* ``FakeQueue`` had no ``has_next``/``has_prev`` at all, so no SyncPlay suite
  could see ``Media.replace_queue`` leaving them frozen across a group queue
  update -- which dropped a client out of its group at the end of a film.
* ``_FakeSyncplay`` collapsed membership and following onto one flag, so the
  halted state that actually reaches an mpv re-creation could not be
  expressed, and a re-create silently zeroed group membership.
* ``FakeThumbs.get_cached`` was ``return None``: it modelled the store's
  callback but not its cache, so nothing could show that every decoded image
  the browser had ever drawn was being kept forever.
* ``FakeManager.enqueue`` recorded the call and wrote no row, so every
  auto-download pass saw a virgin catalog and a five-pass property was
  unobservable.
* ``FakeMPV`` had neither ``eof_reached`` nor ``core_idle`` nor
  ``window_maximized``, and every one of those is read inside a broad
  ``except Exception`` -- so end-of-file was never detected, the trickplay
  arm never armed and the geometry was never re-armed, in a suite that
  passed. It was also missing ``unbind_property_observer`` entirely, which
  made every ``wait_property`` against it raise on the way out; the load
  wait is the *only* thing standing between play() and its timeout path,
  so nothing fake-backed had ever completed a load. And it carried BOTH
  backends' observer APIs at once, which is what the shim discriminates
  on, so the leg named "libmpv" was exercising jsonipc's branch.

The check is the one ``tests/test_syncplay_player_contract.py`` already makes
for the syncplay-to-player surface, generalised: extract from the *source*
what production code reaches for on a collaborator, and assert each stand-in
provides it. Source-level rather than ``dir()`` on an import, because some of
these modules have import side effects (``tests/e2e/_e2e`` repoints
XDG_CONFIG_HOME) and because a constructor-assigned attribute is invisible to
``hasattr`` on a class.

**This is a lead generator, not a prover** -- the same standing as
``tools/audit_stale_captures.py``. It knows what is *reached for*, not what
the fake would have to do to be faithful (``FakeThumbs`` had ``get_cached``;
it was the behaviour that was a lie). A finding is not automatically a bug:
add the name to that pair's ``accepted`` with the reason it does not need
modelling -- and only once something reaches it. A name excused before then
is an excuse the audit will still be honouring on the day the code does reach
it, so `accepted` describes what is, exactly as `owners` does in
``tools/audit_owned_state.py``. What it does catch is the cheap half,
statically, for free.

Usage:  tools/audit_fake_contracts.py [--verbose]
Exit 1 if any stand-in is missing something. tests/test_no_fake_gaps.py runs
this, so a new gap fails the suite rather than waiting to be noticed.
"""

import argparse
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "jellyfin_mpv_shim")


class Pair:
    """One stand-in and the surface it has to cover.

    ``reads`` are the expressions production code reaches the real object
    through (``self.playerManager``, ``self.art.thumbs``, ...). Everything
    read on one of those is the contract. Several spellings are normal: the
    same collaborator is often held under different names by different
    callers.
    """

    def __init__(self, name, fake, cls, reads, accepted=(), notes=""):
        self.name = name
        self.fake = fake            # repo-relative path
        self.cls = cls
        self.reads = tuple(reads)
        self.accepted = set(accepted)
        self.notes = notes


#: Names every object has; reaching one of these says nothing about a fake.
_DUNDER_ISH = {"__class__", "__dict__", "__init__", "__name__"}


PAIRS = [
    Pair(
        "FakeThumbs (ThumbnailStore)",
        "tests/_shell_harness.py", "FakeThumbs",
        reads=("self.art.thumbs", "self.thumbs", "self.shell.thumbs"),
        accepted={
            # Lifecycle the browser owns and no view test drives. `close`,
            # `cache_dir`, `prune` and `_prune_disk` sat here too, excused
            # before anything reached them -- which is the pre-authorisation
            # `tests/test_no_fake_gaps.py` now refuses.
            "shutdown",
        },
        notes="the renderer reads decoded images *through* this cache",
    ),
    Pair(
        "_FakeSyncplay (SyncPlayManager)",
        "tests/integration/_harness.py", "_FakeSyncplay",
        reads=("self.syncplay", "playerManager.syncplay", "pm.syncplay"),
        accepted={
            # Built by the real manager's constructor; nothing in the player
            # reads them off it.
            "discord_join_group",
        },
        notes="survives mpv re-creation, like the menu",
    ),
    Pair(
        "_FakeMenu (OSDMenu)",
        "tests/integration/_harness.py", "_FakeMenu",
        reads=("self.menu", "playerManager.menu", "pm.menu"),
        accepted=set(),
        notes="survives mpv re-creation; gates idle_quit",
    ),
    Pair(
        "FakeMPVLibmpv (python-mpv backend)",
        "tests/integration/_harness.py", "FakeMPVLibmpv",
        reads=("self._player", "pm._player", "playerManager._player",
               "instance", "self.mpv"),
        accepted={
            # jsonipc's spelling of the same three things. Their ABSENCE is
            # what the shim dispatches on -- `mpv_events.observe` and
            # `wait_property` both ask `hasattr(type(x),
            # "bind_property_observer")` -- so providing them here would put
            # this leg back on the other backend's branch, which is the
            # exact bug the split fixed.
            "bind_property_observer", "unbind_property_observer",
            # jsonipc-only, and reached only from mpvtk's *spawn* backend
            # (the standalone demo). The production path is AdoptBackend,
            # which uses event_callback on both.
            "on_event",
        },
        notes="what the shim thinks it is talking to on the libmpv leg",
    ),
    Pair(
        "FakeMPVJsonIPC (python-mpv-jsonipc backend)",
        "tests/integration/_harness.py", "FakeMPVJsonIPC",
        reads=("self._player", "pm._player", "playerManager._player",
               "instance", "self.mpv"),
        accepted={
            # libmpv's spelling; see the pair above. Real jsonipc has none
            # of these either -- `_get_property` least of all, which is why
            # both of its call sites are gated on `is_using_ext_mpv` and
            # reach `command("get_property", ...)` here instead.
            "observe_property", "unobserve_property", "_get_property",
        },
        notes="what the shim thinks it is talking to on the jsonipc leg",
    ),
    Pair(
        "FakeQueue (Media)",
        "tests/_syncplay_network.py", "FakeQueue",
        reads=("video.parent", "self.parent"),
        accepted={
            # Reached on a real Media by the player's own paths (local
            # auto-advance, the queue editor, bitrate selection), none of
            # which this stand-in is ever handed to: it is reached only
            # through SyncPlay's upd_queue.
            "is_local", "get_from_key", "get_next", "get_prev",
            "insert_items",
        },
        notes="the queue a group update rewrites",
    ),
]


_ast_cache = {}


def parse(path):
    if path not in _ast_cache:
        with open(path, encoding="utf-8") as fh:
            _ast_cache[path] = ast.parse(fh.read(), filename=path)
    return _ast_cache[path]


def sources():
    """Every module of the package, so a contract cannot be missed by
    forgetting to list the file that grew it."""
    for base, _dirs, files in os.walk(PKG):
        if "__pycache__" in base or "default_shader_pack" in base:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def dotted(node):
    """Render an attribute chain (``self.art.thumbs``) back to a string, or
    None for anything else -- a call, a subscript, a literal."""
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def contract_for(pair):
    """Every attribute production code reaches on this collaborator."""
    names = set()
    for path in sources():
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.Attribute):
                continue
            if dotted(node.value) in pair.reads:
                names.add(node.attr)
    return names - _DUNDER_ISH


def members_of(path, class_name):
    """Methods, class attributes and ``self.x = ...`` of one class, read from
    the source. Includes what a base class provides, when that base is in the
    same file."""
    tree = parse(os.path.join(ROOT, path))
    classes = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)}
    node = classes.get(class_name)
    if node is None:
        raise SystemExit("no class %r in %s" % (class_name, path))

    found = set()
    pending = [node]
    seen = set()
    while pending:
        cur = pending.pop()
        if cur.name in seen:
            continue
        seen.add(cur.name)
        for base in cur.bases:
            if isinstance(base, ast.Name) and base.id in classes:
                pending.append(classes[base.id])
        for item in cur.body:
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                found.add(item.name)
        for sub in ast.walk(cur):
            if isinstance(sub, ast.Assign):
                for target in sub.targets:
                    if (isinstance(target, ast.Attribute)
                            and isinstance(target.value, ast.Name)
                            and target.value.id == "self"):
                        found.add(target.attr)
                    elif isinstance(target, ast.Name):
                        found.add(target.id)
            elif isinstance(sub, ast.AnnAssign):
                if (isinstance(sub.target, ast.Attribute)
                        and isinstance(sub.target.value, ast.Name)
                        and sub.target.value.id == "self"):
                    found.add(sub.target.attr)
                elif isinstance(sub.target, ast.Name):
                    found.add(sub.target.id)
    return found


def audit(verbose=False):
    findings = []
    for pair in PAIRS:
        contract = contract_for(pair)
        if not contract:
            # A guard on the guard: an extraction that finds nothing would
            # report every stand-in as perfect.
            findings.append((pair, ["<the extraction found nothing — did the "
                                    "code stop reaching this collaborator by "
                                    "the names in `reads`?>"]))
            continue
        provided = members_of(pair.fake, pair.cls)
        missing = sorted(contract - provided - pair.accepted)
        if verbose:
            print("%s: %d reached, %d provided, %d accepted"
                  % (pair.name, len(contract), len(provided),
                     len(pair.accepted)))
        if missing:
            findings.append((pair, missing))
    return findings


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    findings = audit(args.verbose)
    for pair, missing in findings:
        print("\n%s — %s\n  %s" % (pair.name, pair.fake, pair.notes))
        for name in missing:
            print("    missing: %s" % name)
    if findings:
        print("\n%d stand-in(s) do not cover what production code reaches.\n"
              "Model the field, or add it to that pair's `accepted` with the "
              "reason it cannot matter." % len(findings))
        return 1
    print("Every stand-in covers what is reached on it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
