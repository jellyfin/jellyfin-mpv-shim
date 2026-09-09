#!/usr/bin/env python3
"""Find forced key bindings that nothing ever releases.

`mp.add_forced_key_binding` installs a binding that outranks the user's own
input.conf and mpv's defaults, and it stays until something names it to
`mp.remove_key_binding`. A binding whose owner goes away without releasing
it does not fail, log, or degrade -- it silently keeps eating that key for
the rest of the session, and the symptom is "the mouse stopped working"
rather than anything pointing at the binding.

That is #737. `phud_skip_bind` took `mbtn_left` for the Skip button -- but
only in right-click-to-pause mode, since with click-to-pause on
`mpvtk_phud_click` already owns the button -- and `phud_skip_unbind`
released only the ENTER half. Every skip segment left another
`mpvtk_skip_click` behind, and once the button was down its else-branch ran
`begin-vo-dragging`, so every click dragged the window instead of reaching
the UI. The reporter's account is the exact shape: "the UI becomes
unresponsive after a few videos ... keyboard shortcuts still work ...
switching back to left click to pause this behavior never occurs".

**Why a lint and not a test.** The binding registry is real, so a Lua test
*can* assert one name is gone -- and one such test now does. What it cannot
do is notice the next binding somebody adds. The leak lived through a
release with a suite of 400 renderer assertions around it, because nothing
enumerated the pairs. This does.

A finding is not automatically a bug. Either release the binding where its
owner goes away, or add the name to `ACCEPTED` with the reason it does not
need releasing -- which is documentation either way.
"""

import argparse
import os
import re
import sys

#: Bindings that are released by something this cannot see, with the
#: mechanism. Keep the reason concrete: it is the only evidence a reader
#: gets that the name is not the next #737.
ACCEPTED = {
    "mpvtk_text": (
        "released through the `text_key_names` registry -- `bind_text_keys` "
        "appends every name it installs and `unbind_text_keys` iterates the "
        "list calling remove_key_binding, so the release is by variable and "
        "not by literal."
    ),
}

#: `mp.add_forced_key_binding(<key>, <name>, ...)` and its non-forced
#: sibling. The name is the second argument.
_ADD = re.compile(
    r"mp\.add_(?:forced_)?key_binding\s*\(\s*[^,]+,\s*(.+?)\s*,", re.S)
_REMOVE = re.compile(r"mp\.remove_key_binding\s*\(\s*(.+?)\s*\)", re.S)

#: A name built by concatenation -- `'mpvtk_nav_' .. name`. Only the
#: literal prefix is knowable statically, and that is enough: the add and
#: the remove have to agree on it.
_CONCAT = re.compile(r"^'([^']*)'\s*\.\.")
_LITERAL = re.compile(r"^'([^']*)'$")


def _name_of(expr):
    """(kind, value) for a binding-name expression, or None if it is a bare
    variable -- those are released by variable too and carry no literal to
    match on."""
    expr = expr.strip()
    hit = _LITERAL.match(expr)
    if hit:
        return ("literal", hit.group(1))
    hit = _CONCAT.match(expr)
    if hit:
        return ("prefix", hit.group(1))
    return None


def audit(path):
    """[(line, name, kind)] for every added binding with no matching
    release in the same file."""
    with open(path, encoding="utf-8") as fh:
        src = fh.read()

    released_literals, released_prefixes = set(), set()
    for expr in _REMOVE.findall(src):
        got = _name_of(expr)
        if got is None:
            continue
        kind, value = got
        (released_literals if kind == "literal" else
         released_prefixes).add(value)

    findings = []
    for hit in _ADD.finditer(src):
        got = _name_of(hit.group(1))
        if got is None:
            continue
        kind, value = got
        if value in ACCEPTED:
            continue
        if kind == "literal":
            ok = value in released_literals or any(
                value.startswith(p) for p in released_prefixes)
        else:
            ok = value in released_prefixes or any(
                lit.startswith(value) for lit in released_literals)
        if not ok:
            line = src.count("\n", 0, hit.start()) + 1
            findings.append((line, value, kind))
    return findings


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", help="lua files to audit")
    args = ap.parse_args(argv)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = args.files or [
        os.path.join(root, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")]

    bad = 0
    for path in files:
        for line, name, kind in audit(path):
            bad += 1
            print("%s:%d: %s binding %r is never released"
                  % (os.path.relpath(path, root), line, kind, name))
    if bad:
        print("\n%d unreleased binding(s). Release it where its owner goes "
              "away, or add it to ACCEPTED with the reason." % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
