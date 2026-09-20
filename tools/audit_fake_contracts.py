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

There is a **second check** in here, over the same subject from the other
side: whether a stand-in's *signature* is more permissive than the real
method's. It exists because the first check cannot see -- four doubles that
*had* the method and answered the rule it replaced, six of them spelling a
scope the real store requires as ``server_uuid=None``. A double that defaults
what production must pass is the one shape that lets a caller which **forgot
the scope** pass the suite and fail in production, which is the failure the
whole scope convention exists to prevent.

That check discovers its own pairs rather than being handed them, because item
H's doubles were classes defined *inside test methods* -- nobody would have
registered them, and the ones nobody registers are the ones that drift. See
``REALS`` and ``signature_findings`` for the rule and its bounds.

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


#: The real classes a test double is worth comparing signatures against.
#:
#: Deliberately short. These are the classes whose methods carry the **content
#: scope** -- the argument the 2026-09-19 convention made explicit and
#: defaultless in the places it matters -- so a double that defaults one is the
#: specific defect that convention can still suffer. Adding a class here costs
#: nothing but a pass over `tests/`; adding one whose methods have no required
#: keyword arguments buys nothing.
REALS = (
    ("jellyfin_mpv_shim/sync/db.py", "SyncDB"),
    ("jellyfin_mpv_shim/sync/manager.py", "SyncManager"),
    ("jellyfin_mpv_shim/sync/auto.py", "AutoDownloader"),
    ("jellyfin_mpv_shim/clients.py", "ClientManager"),
)

#: Method names too common to identify anything. A test class sharing only
#: these with a real one is a coincidence -- `FakeClient.start`/`stop` matched
#: `SyncManager` before this existed -- so a candidate needs at least one
#: overlapping name from outside this set.
_GENERIC = {"start", "stop", "close", "run", "reset", "update", "get",
            "clear", "tick", "delete", "save", "load"}

#: (test file, class name, method) -> why this double may be more permissive
#: than the real method. The key is a name, so where one file holds two classes
#: of the same name an entry covers both. Same standing as `accepted` above and as `owners` in
#: `tools/audit_owned_state.py`: it describes what IS, so an entry that no
#: longer differs is a pre-authorisation and `tests/test_no_fake_gaps.py`
#: fails on it.
ACCEPTED_SIGNATURES = {}


def _signature(fn):
    """What a call to `fn` may leave out.

    The instance parameter is dropped **by position, not by name**: a double
    written `def f(self_inner, pid, *, server_id)` is correct, and a rule that
    looked for the name `self` reported it as dropping an argument.
    """
    a = fn.args
    positional = [x.arg for x in a.posonlyargs + a.args]
    static = any(isinstance(d, ast.Name) and d.id == "staticmethod"
                 for d in fn.decorator_list)
    if positional and not static:
        positional = positional[1:]
    required = max(0, len(positional) - len(a.defaults))
    keyword = {x.arg for x in a.kwonlyargs}
    keyword_required = {x.arg for x, d in zip(a.kwonlyargs, a.kw_defaults)
                        if d is None}
    return {"positional": len(positional), "required": required,
            "positional_names": set(positional),
            "keyword": keyword, "keyword_required": keyword_required,
            "kwargs": a.kwarg is not None, "star": a.vararg is not None}


def more_permissive(fake, real):
    """Ways a call that the real method would reject is accepted by the double.

    **Only that direction.** A double that is *stricter* raises `TypeError`,
    which is loud and immediate; and a renamed positional (`pid` for
    `playlist_id`) is invisible to a positional caller, so comparing those
    names reports correct doubles. What is left is the quiet direction: an
    argument production must pass that the double will supply for it.
    """
    out = []
    sf, sr = _signature(fake), _signature(real)
    if sf["required"] < sr["required"] and not sf["star"]:
        out.append("takes %d required positional argument(s) where the real "
                   "one requires %d" % (sf["required"], sr["required"]))
    for name in sorted(sr["keyword_required"]):
        if name in sf["keyword"] and name not in sf["keyword_required"]:
            out.append("defaults %r, which the real method requires" % name)
        elif name not in sf["keyword"] and sf["kwargs"]:
            out.append("absorbs the required %r into **kwargs" % name)
    # A keyword-only argument by a name the real method does not have at all.
    # Not permissiveness in the arity sense -- production passing it would get
    # a TypeError, which is loud -- but it is the *signature* half of item H's
    # six `server_uuid=None` doubles: a keyword-only name is the interface, so
    # one that no longer exists on the real method means this double was
    # written against a superseded one, and what it does with the argument
    # nobody passes is answer some older rule.
    for name in sorted(sf["keyword"] - sr["keyword"] - sr["positional_names"]):
        out.append("takes a keyword-only %r, which the real method has no "
                   "parameter of that name for" % name)
    return out


def _methods(node):
    return {item.name: item for item in node.body
            if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))}


def _is_a_test_class(node):
    """A `TestCase`, not a stand-in. They are told apart by what only a test
    has, rather than by resolving base classes across files: a test class with
    a private helper called `_delete` or `_spawn` otherwise reads as a double
    for whichever real class happens to have that name."""
    for item in node.body:
        if (isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                and (item.name.startswith("test")
                     or item.name in ("setUp", "tearDown",
                                      "setUpClass", "tearDownClass"))):
            return True
    return False


def _real_methods():
    out = {}
    for path, name in REALS:
        tree = parse(os.path.join(ROOT, path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == name:
                out[name] = _methods(node)
    missing = [n for _p, n in REALS if n not in out]
    if missing:
        raise SystemExit("no class %s -- REALS is stale" % ", ".join(missing))
    return out


def test_files():
    for base, _dirs, files in os.walk(os.path.join(ROOT, "tests")):
        if "__pycache__" in base:
            continue
        for name in sorted(files):
            if name.endswith(".py"):
                yield os.path.join(base, name)


def doubles():
    """Every test class that looks like a stand-in for one of `REALS`, as
    ``(relative path, the class node, real name, shared method names)``.

    The **node**, not its name: one file can hold two classes called `FakeDB`
    (`tests/test_shell_downloads.py` does), and re-finding one by name gets
    whichever comes first, which then reports the other one's methods.

    The match is by **public** method name overlap, best match wins, with at
    least one name outside `_GENERIC`. `TestCase` classes are skipped, private
    names never count, and neither does `__init__` -- a double's constructor
    belongs to the test that builds it.

    One shared name is enough, deliberately: item H's doubles had one or two
    methods each, so a threshold of two excluded exactly the shape this was
    built for.

    A heuristic, and the reason it is allowed to be one is that this whole
    file is a lead generator with an allowlist. What it must not do is *miss*
    the doubles a person would forget to register, which is why it walks.
    """
    reals = _real_methods()
    found = []
    for path in test_files():
        rel = os.path.relpath(path, ROOT)
        for node in ast.walk(parse(path)):
            if not isinstance(node, ast.ClassDef):
                continue
            if _is_a_test_class(node):
                continue
            # The PUBLIC surface: a double stands in for what production calls,
            # and a private helper sharing a name with one is the coincidence
            # that produced every false positive this rule had.
            mine = {n for n in _methods(node) if not n.startswith("_")}
            if not mine:
                continue
            best, shared = None, set()
            for name, real in reals.items():
                common = mine & {n for n in real if not n.startswith("_")}
                if len(common) > len(shared):
                    best, shared = name, common
            if best is None or not (shared - _GENERIC):
                continue
            found.append((rel, node, best, sorted(shared)))
    return found


def signature_findings():
    """``[(path, class, real, [complaint, ...])]`` for doubles that would
    accept a call the real method rejects."""
    reals = _real_methods()
    out = []
    for rel, node, real, shared in doubles():
        fake = _methods(node)
        notes = []
        for name in shared:
            if (rel, node.name, name) in ACCEPTED_SIGNATURES:
                continue
            for note in more_permissive(fake[name], reals[real][name]):
                notes.append("%s() %s" % (name, note))
        if notes:
            out.append((rel, node.name, real, notes))
    return out


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

    loose = signature_findings()
    if args.verbose:
        print("\n%d double(s) discovered for %d real class(es)"
              % (len(doubles()), len(REALS)))
    for rel, cls, real, notes in loose:
        print("\n%s:%s — stands in for %s" % (rel, cls, real))
        for note in notes:
            print("    %s" % note)
    if loose:
        print("\n%d double(s) accept a call the real method rejects, so a "
              "caller that omitted the argument would pass here and fail in "
              "production.\nMatch the signature, or add (file, class, method) "
              "to ACCEPTED_SIGNATURES with the reason." % len(loose))

    if findings or loose:
        return 1
    print("Every stand-in covers what is reached on it, and none is more "
          "permissive than what it stands for.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
