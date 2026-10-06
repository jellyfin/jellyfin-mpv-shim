#!/usr/bin/env python3
"""Find calls to ``StripStore.keep`` whose answer is thrown away.

A caller that parks a bitmap entry across frames (the epub reader, the cast
screen) renders from that entry rather than asking the store again. The
entry's buffer is freed by eviction, a trim, or ``clear()`` when mpv is
re-created -- and the parked dict outlives all three. ``keep`` is how such a
caller learns that: it returns False for a freed entry. Drawing one anyway
hands mpv the address of freed memory, which is a crash on Windows and
garbage pixels on Linux. See docs/browser-shell.md §6, "Held bitmaps".

So a bare ``store.keep(entry)`` statement is the bug's shape. Anything else
exits 1; a call whose result genuinely does not matter goes in ACCEPTED,
keyed ``path::function``, with the reason.

Usage:
    tools/audit_keep_result.py [package_dir ...]   # default: the package
"""

import argparse
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(os.path.dirname(HERE), "jellyfin_mpv_shim")

ACCEPTED = {}


def scan_file(path, rel):
    """(key, detail) per ignored ``.keep(...)`` call in one file."""
    rel = rel.replace(os.sep, "/")
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError as exc:
        print("%s: could not parse (%s)" % (rel, exc), file=sys.stderr)
        return

    def walk(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef)):
                inner = scope + [child.name]
            if (isinstance(child, ast.Expr)
                    and isinstance(child.value, ast.Call)
                    and isinstance(child.value.func, ast.Attribute)
                    and child.value.func.attr == "keep"):
                name = ".".join(scope) or "<module>"
                yield ("%s::%s" % (rel, name),
                       "%s:%d  %s() ignores what keep() answered"
                       % (rel, child.lineno, name))
            yield from walk(child, inner)

    yield from walk(tree, [])


def sites(root=DEFAULT_ROOT):
    """Every ``.keep(...)`` call in the package, used or not -- what the
    audit considers (tests/test_audits_examine_something.py)."""
    found = []
    for dirpath, _dirs, files in os.walk(root):
        for f in sorted(files):
            if f.endswith(".py"):
                path = os.path.join(dirpath, f)
                tree = ast.parse(open(path, encoding="utf-8").read())
                found += ["%s:%d" % (path, n.lineno) for n in ast.walk(tree)
                          if isinstance(n, ast.Call)
                          and isinstance(n.func, ast.Attribute)
                          and n.func.attr == "keep"]
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", default=[DEFAULT_ROOT])
    args = ap.parse_args(argv)
    new = []
    for root in args.roots:
        base = os.path.dirname(root.rstrip("/"))
        for dirpath, _dirs, files in os.walk(root):
            for f in sorted(files):
                if f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    new += [d for k, d in scan_file(
                        path, os.path.relpath(path, base))
                        if k not in ACCEPTED]
    for detail in new:
        print(detail)
    if new:
        print("\n%d keep() call(s) ignore the answer. Drop the entry and "
              "composite again when it is False." % len(new))
        return 1
    print("no ignored keep() calls")
    return 0


if __name__ == "__main__":
    sys.exit(main())
