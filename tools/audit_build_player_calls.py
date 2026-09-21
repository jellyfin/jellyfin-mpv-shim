#!/usr/bin/env python3
"""Every `build_player` call hands over its test case.

`_harness.build_player` registers the refused-write cleanup on the case that
called it (`watch_refused_writes`), because the integration suite has **no
shared base** -- its cases inherit `unittest.TestCase` directly -- so a base
class added now would cover only the files somebody remembered to re-parent.
`build_player` is the one thing all of them call.

That makes the hook opt-in by construction, and opt-in is the thing "auto-
assert" rules out: a check each test has to remember is absent exactly where
it is needed. So the opt-in is **enforced rather than remembered** -- a call
site that does not pass its case is a failure here, which is what converts a
parameter back into coverage nobody has to think about.

**What counts as a call.** The harness is always reached through its module
alias (`h.build_player(...)`, 47 sites when this was written), so an
attribute-spelled call is the rule. A *bare* `build_player(...)` is checked
only in a file that imports the name from the harness, because a bare one is
otherwise a different function: `tests/test_syncplay_pause_ignore.py` defines
its own builder of that name, with its own stand-in player, and has nothing to
do with this.

**Wrappers are checked too, and this is the hole that made it necessary.**
A local `def build(test=None, **kw)` forwarding to `build_player` satisfies the
call-site rule -- the keyword is right there -- while letting every caller of
the *wrapper* omit its case and get None. That is the opt-in this file exists
to close, reopened one level up where a rule reading only call sites cannot
see it. So a function that forwards one of its own **defaulted parameters** as
`build_player`'s `test=` is the finding -- whatever that parameter is called,
and regardless of what else the function does with a name spelled `test`.

A finding is not automatically a bug -- a call site with no case to hand over
(a module-level fixture built once for a whole file) is legitimate and belongs
in ACCEPTED with the reason.
"""
import ast
import os
import sys

#: This file is `tools/`, so the repo root is its grandparent. ACCEPTED's
#: keys are repo-relative and the lookup normalises to that.
_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: `"<path>:<line>"` -> why that call site has no case to hand over. A bare
#: entry is not acceptable: say what the player is built for, so the next
#: reader can tell a decision from a gap.
ACCEPTED = {}


def _defines_its_own(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "build_player":
            return True
    return False


def _imports_the_name(tree):
    """Whether this file did `from _harness import ... build_player ...`."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if any(alias.name == "build_player" for alias in node.names):
                return True
    return False


def _defaulted_params(fn):
    """The names of ``fn``'s parameters that carry a default."""
    args = fn.args
    positional = list(args.posonlyargs) + list(args.args)
    out = set()
    if args.defaults:
        for name, _default in zip(positional[-len(args.defaults):],
                                  args.defaults):
            out.add(name.arg)
    for name, default in zip(args.kwonlyargs, args.kw_defaults):
        if default is not None:
            out.add(name.arg)
    return out


def _forwarding_wrappers(tree, bare_counts):
    """Line numbers of functions that forward a DEFAULTED parameter as `test`.

    See the wrapper paragraph above: the call inside one of these looks
    correct to `_calls`, so the hole is only visible from the definition.

    The question is about the **value**, not about either spelling. Asking
    whether the wrapper has a parameter literally called `test` misses
    `def build(case=None): ... build_player(mod, test=case)`, which is the
    same hole one rename away; asking only whether the *keyword* is `test`
    flags `build_player(mod, test=self)` inside a method that happens to
    have a `test=None` parameter it does not forward. Both were real: this
    file had one of each, and each is now a case in
    `tests/test_no_unwatched_players.py`.
    """
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        if node.name == "build_player":
            continue
        defaulted = _defaulted_params(node)
        if not defaulted:
            continue
        for inner in ast.walk(node):
            if not isinstance(inner, ast.Call):
                continue
            func = inner.func
            if isinstance(func, ast.Attribute):
                named = func.attr == "build_player"
            elif isinstance(func, ast.Name):
                named = func.id == "build_player" and bare_counts
            else:
                named = False
            if not named:
                continue
            if any(kw.arg == "test"
                   and isinstance(kw.value, ast.Name)
                   and kw.value.id in defaulted
                   for kw in inner.keywords):
                found.append(node.lineno)
                break
    return found


def _calls(tree, bare_counts):
    """Every build_player call in the file, as (lineno, passes_test)."""
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute):
            named = func.attr == "build_player"
        elif isinstance(func, ast.Name):
            named = func.id == "build_player" and bare_counts
        else:
            named = False
        if not named:
            continue
        found.append((node.lineno,
                      any(kw.arg == "test" for kw in node.keywords)))
    return found


def audit(root="tests"):
    """(offenders, checked) -- offenders as (site, reason) pairs."""
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
            # The harness itself defines the function and forwards nothing.
            rel = os.path.relpath(os.path.abspath(path), _REPO).replace(
                os.sep, "/")
            own = _defines_its_own(tree)
            bare = _imports_the_name(tree)
            if not (own and rel.endswith("_harness.py")):
                for lineno in _forwarding_wrappers(tree, bare):
                    checked += 1
                    site = "%s:%d" % (rel, lineno)
                    if site in ACCEPTED:
                        continue
                    offenders.append(
                        (site, "forwards a DEFAULTED `test` to build_player, "
                               "so a caller of this wrapper can omit its case "
                               "and nothing watches what mpv refused"))
            for lineno, passes in _calls(tree, bare):
                if own and rel.endswith("_harness.py"):
                    continue
                checked += 1
                site = "%s:%d" % (rel, lineno)
                if passes or site in ACCEPTED:
                    continue
                offenders.append(
                    (site, "builds a player without handing over its test "
                           "case, so nothing watches what mpv refused"))
    return offenders, checked


if __name__ == "__main__":
    bad, total = audit()
    for site, why in bad:
        print("%s: %s" % (site, why))
    print("checked %d build_player call site(s); %d hand over no case"
          % (total, len(bad)))
    sys.exit(1 if bad else 0)
