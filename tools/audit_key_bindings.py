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

**Second pass, the other direction: the keys mpv's own scripts take from
us.** The console and the context menu each force the same keys the
renderer does, and each publishes a `user-data/mpv/<thing>/open` flag while
it is up. `PUBLISHED` declares every one of those properties and what the
renderer owes it. The block above `Published` carries the measurement that
says why ORDER, and not force, decides who gets the key.
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


# --------------------------------------------------------------------
# Second pass: the keyboard mpv's own scripts take, and whether we notice.
#
# The first pass is about bindings WE install and forget. This one is the
# other direction, and it is the same rule at the site nobody wrote it at.
#
# mpv's builtin console and context menu both install their keys with
# `mp.add_forced_key_binding`, exactly as the renderer does, and each
# publishes a `user-data/mpv/<thing>/open` boolean while it is up. The
# renderer observes the console's and hands its own keys back for the
# duration. Nothing watches the context menu's.
#
# **Measured against mpv 182fa6ca49, not reasoned about** (the probes are
# in the commit message; each asserts which handler RECEIVED a synthetic
# `keypress ENTER`, not which one has the larger `priority` number):
#
#   A. nothing open .............. our forced ENTER fires
#   B. the thing opens AFTER us .. ITS binding fires, not ours
#   C. we re-bind while it is up . OUR binding fires, and it stays open
#
# B is why this is not the leak the first pass looks for: a standing
# binding of ours does not break a menu that opens later. C is the whole
# exposure -- the HUD re-installs its nav keys on pointer movement and on
# every lifecycle event, so anything that re-binds while a menu is up
# takes the key back and leaves the menu on screen with no way to activate
# an item. That is what the console handler prevents, at one of the two
# places that need it.
#
# It also corrects the console handler's own comment, which says our
# forced bindings "outrank" the console. They do not; B measures the
# opposite -- and `renderer.lua:5771` had the rule right the whole time,
# 1,200 lines away: "between two forced bindings of one key the LATER one
# wins". One rule, written correctly at one site and wrongly at another,
# which is this repo's signature shape.
#
# What the console handler is really for is C, plus the `any_unicode`
# block claim -- which outranks an exact key installed BEFORE it, and is
# bound first for that reason.


class Published:
    """One `user-data/mpv/*` property, and what the renderer owes it.

    ``state`` is ``observed`` (the renderer watches it and must go on
    doing so), ``not-input`` (it says nothing about who owns the
    keyboard, so watching it would be noise), or ``gap`` (it should be
    watched, it is not, and the reason is recorded rather than fixed
    quietly).
    """

    def __init__(self, state, since, by, why):
        self.state = state
        self.since = since
        self.by = by
        self.why = why


#: Everything mpv's builtin scripts publish under `user-data/mpv/`, read
#: out of mpv's own tree at 182fa6ca49. Not discoverable at lint time --
#: mpv's source is not in this repo -- so it is a declared list, and the
#: version each appeared in is part of the declaration because the shim
#: runs against several.
PUBLISHED = {
    "console/open": Published(
        "observed", "0.40.0 (8669205d92, 2025-01-30)", "console.lua",
        "The console wants the whole keyboard. The renderer hands back "
        "nav, summon, skip, wake and the any_unicode block while it is "
        "up and takes them again on close.",
    ),
    "context-menu/open": Published(
        "gap", "master only (aec426a4d8, 2026-02-19); NOT in v0.41.0",
        "context_menu.lua",
        "The menu forces ENTER, ESC, the arrows and any_unicode, and it "
        "is what MBTN_RIGHT opens on master -- which is the pin both "
        "shipped builds use. Nothing hands our keys back, so any "
        "re-bind while it is up (case C above) leaves the menu drawn and "
        "dead. NOT FIXED HERE ON PURPOSE: the repair lands on the input "
        "arbiter, which docs/do-not-fix.md F37 records as the worst "
        "regression surface in the tree, and #737's own fix is still "
        "ahead of its evidence one branch below. What is measured is the "
        "mpv mechanism; what is not is that the shim reaches case C in a "
        "real session, and that is a real-mpv e2e leg, not a lint.",
    ),
    "ytdl/path": Published(
        "not-input", "0.39.0 (ff47926d6a, 2024-05-09)", "ytdl_hook.lua",
        "Where yt-dlp was found. Says nothing about the keyboard.",
    ),
    "ytdl/json-subprocess-result": Published(
        "not-input", "0.39.0 (ff47926d6a, 2024-05-09)", "ytdl_hook.lua",
        "The hook's subprocess result, for scripts that want the raw "
        "answer. Says nothing about the keyboard.",
    ),
}

_OBSERVE = re.compile(
    r"mp\.observe_property\s*\(\s*'user-data/mpv/([^']+)'")


def observed(path):
    """The `user-data/mpv/*` properties the file observes."""
    with open(path, encoding="utf-8") as fh:
        return set(_OBSERVE.findall(fh.read()))


def audit_user_data(path):
    """[(prop, problem)] where the renderer and PUBLISHED disagree.

    A declared gap is NOT a finding -- it is printed every run and pinned
    by the test, so a new one fails and a recorded one stays visible.
    """
    seen = observed(path)
    findings = []
    for prop, entry in sorted(PUBLISHED.items()):
        if entry.state == "observed" and prop not in seen:
            findings.append((prop, "declared observed, and nothing observes "
                                   "it -- the handler was removed or the "
                                   "property was renamed"))
        elif entry.state != "observed" and prop in seen:
            findings.append((prop, "declared %r, and the renderer observes "
                                   "it anyway -- move the entry to "
                                   "'observed' with what the handler does"
                                   % entry.state))
    for prop in sorted(seen - set(PUBLISHED)):
        findings.append((prop, "observed, and not in PUBLISHED -- say which "
                               "mpv script publishes it and from which "
                               "release, so the next reader can tell a live "
                               "property from a dead one"))
    return findings


def gaps():
    """The properties recorded as watched-by-nothing, on purpose."""
    return sorted(p for p, e in PUBLISHED.items() if e.state == "gap")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("files", nargs="*", help="lua files to audit")
    args = ap.parse_args(argv)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    files = args.files or [
        os.path.join(root, "jellyfin_mpv_shim", "mpvtk", "renderer.lua")]

    bad = 0
    for path in files:
        rel = os.path.relpath(path, root)
        for line, name, kind in audit(path):
            bad += 1
            print("%s:%d: %s binding %r is never released"
                  % (rel, line, kind, name))
        for prop, problem in audit_user_data(path):
            bad += 1
            print("%s: user-data/mpv/%s: %s" % (rel, prop, problem))
    if bad:
        print("\n%d finding(s). Release the binding where its owner goes "
              "away and add it to ACCEPTED with the reason; for a "
              "user-data property, correct its PUBLISHED entry." % bad)
    for prop in gaps():
        print("note: user-data/mpv/%s is watched by nothing, on purpose --"
              " see PUBLISHED" % prop)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
