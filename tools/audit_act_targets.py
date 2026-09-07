#!/usr/bin/env python3
"""Find the browser reaching past `PlayerManager` into mpv.

The gateway's whole job is to be the one door between the browser's loop
thread and the player. `_act` runs its lambda through `run_action`, which
**defers whenever the player lock is busy** -- and the lock is held for the
whole of a playback start. So a `_act` body is not simply "player code that
runs later": it runs at an unknown time, with no lock held, against an mpv
whose handle may have been replaced in the meantime.

`PlayerManager`'s own methods are where the rules about all of that live.
A `_act` body that writes `pm._player.<prop>` instead of calling one of
them skips every rule that method carries.

* **R8 -- confirmed end to end.** `set_fullscreen` records
  `fullscreen_disable`, *a user-intent flag*, above its own
  `if not persist: return`. An app-initiated un-fullscreen latches it and
  every film from then on starts windowed, with no setting to explain it
  (`docs/POSTMORTEM_3.0.0.md` C6).
* **R7 -- the same shape, and NOT established.** `reset_picture_view` grew
  a `_video is None and not _loading` guard and `set_picture_view` has
  none. Read the guard before acting on that: it covers only the
  `keepaspect` write, which `set_picture_view` does not make, and the two
  are symmetric on zoom and pan. `docs/do-not-fix.md` F15 says *"construct
  the interleaving before fixing"*, and that is still owed.

Both are rows in `docs/RISK_MAP_2026-09.md` section 2. This file needs
neither to be true: what it checks is that a reach past the manager is a
decision somebody wrote down.
`docs/POSTMORTEM_3.0.0.md` section 5.3 asked for this file by name and
`docs/RISK_MAP_2026-09.md` section 7 puts it first among the lints, because
it is the only one of them covering a Tier-1 and a Tier-2 row.

**Two rules, and neither is a correctness check.**

* **A -- a write.** `setattr(pm._player, "x", ...)` or `pm._player.x = ...`
  inside the gateway. This is the one that matters: it changes mpv without
  the manager's lock, its guards or its bookkeeping.
* **B -- a read.** Any other reach for `._player` from the gateway. Weaker
  on its own -- a read cannot corrupt anything -- but it is how a write
  gets written: the reader is already holding the handle, so the write is
  one line away, and three of the four writes below sit next to a read of
  the same property.

A finding is answered by routing through the `PlayerManager` method, or by
adding the site to `DECLARED` with the reason it is the right shape. As in
`tools/audit_owned_state.py`, `DECLARED` records sites that EXIST: a key
for a site that is gone is a pre-authorisation for the next one, and
`tests/test_no_act_reachthrough.py` fails on it.

Usage:  tools/audit_act_targets.py [--verbose]
Exit 1 if anything is undeclared.
"""

import argparse
import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATEWAY = os.path.join("jellyfin_mpv_shim", "mpvtk_browser", "gateway")

#: The handle every rule here is about.
HANDLE = "_player"


class Site:
    def __init__(self, module, func, prop, rule, line):
        self.module = module      # basename, e.g. "hud.py"
        self.func = func
        self.prop = prop          # the attribute reached off `_player`
        self.rule = rule          # "A" (write) or "B" (read)
        self.line = line

    @property
    def key(self):
        return "%s:%s:%s:%s" % (self.module, self.func, self.rule, self.prop)

    def __repr__(self):
        return "<%s line %d>" % (self.key, self.line)


#: Every reach that exists today, with why it has not been routed through a
#: manager method. Three of these name a method that is already there and
#: simply not called -- they are findings with an owner waiting, recorded
#: rather than patched because changing who takes the player lock is a
#: behaviour change and belongs in a commit of its own.
DECLARED = {
    "hud.py:set_speed:A:speed": (
        "`PlayerManager.set_speed` EXISTS (player.py) and this does not "
        "call it. It also carries no `@synchronous`, so routing through it "
        "would not by itself add the lock this skips -- the repair is both "
        "halves at once, and it is a player change rather than a gateway "
        "one."
    ),
    "hud.py:toggle_mute:A:mute": (
        "`PlayerManager.set_mute` EXISTS (player.py) and this does not call "
        "it. Read-modify-write on the handle, so it also decides the new "
        "value from a read taken at deferred time rather than at click "
        "time."
    ),
    "hud.py:toggle_fullscreen.flip:A:fullscreen": (
        "Writes the handle and THEN queues `pm.set_fullscreen` for the "
        "intent flag, so the window changes by one path and the record of "
        "why by another. `set_fullscreen` is `@synchronous` and this write "
        "is not. Risk-map R8 is the flag half of that same method, which is "
        "why the repair is a parameter rather than a moved line."
    ),
    "hud.py:toggle_fullscreen.flip:B:fullscreen": (
        "The read the write on the next line is derived from; same repair."
    ),
    "hud.py:get_speed:B:speed": (
        "Paired with the `set_speed` write above -- one accessor answers "
        "both."
    ),
    "hud.py:get_aspect:B:video_aspect_override": (
        "The write half went through `PlayerManager.set_aspect`; this did "
        "not follow it, because the two ask different questions. The "
        "manager stores the FORCE, and the gear row ticks whatever mpv is "
        "showing -- which with no force is the file's own aspect, or the "
        "user's mpv.conf. `aspect_forced()` answers the manager's question "
        "and is not what this row draws."
    ),
    "hud.py:chapters:B:chapter_list": (
        "Reads mpv's chapter list to draw the chapter row. Read-only, and "
        "the ACTING half of the same feature already went the other way: "
        "`chapter_seek` calls `pm.chapter_seek` rather than working out its "
        "own target, because that was a second definition of \"previous "
        "chapter\" and it went round SyncPlay."
    ),
    "hud.py:player_stats:B:*": (
        "`getattr(playerManager, \"_player\", None)` -- a presence test "
        "followed by seven independent property reads for the Playback "
        "Data panel, each dropped on failure because no mpv has all of "
        "them at all times. Read-only, and a manager method per counter "
        "would be seven methods for one panel."
    ),
    "diagnostics.py:copy_text:B:*": (
        "Passes the handle to `clipboard.copy_or_save`, which offers mpv "
        "first so a box with no wl-copy/xclip/xsel still copies. The handle "
        "is an argument to a module that owns the clipboard rule, not a "
        "property reach, and `None` is a supported value."
    ),
}


def _writes(node):
    """(prop, ) for a Rule A write in this statement, else None.

    Both spellings: `x._player.p = v` and `setattr(x._player, "p", v)`.
    """
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [
            node.target]
        for tgt in targets:
            if (isinstance(tgt, ast.Attribute)
                    and isinstance(tgt.value, ast.Attribute)
                    and tgt.value.attr == HANDLE):
                return tgt.attr
    if (isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "setattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Attribute)
            and node.args[0].attr == HANDLE):
        name = node.args[1]
        if isinstance(name, ast.Constant) and isinstance(name.value, str):
            return name.value
        # A computed property name is still a write, and a worse one --
        # nothing can tell which property it lands on.
        return "<computed>"
    return None


def _scan(path):
    """Every Rule A and Rule B site in one gateway module."""
    module = os.path.basename(path)
    with open(path, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src, filename=path)

    sites = []
    stack = []
    # Nodes already charged as a write, so the `_player` inside them is not
    # also reported as a read. A write is the stronger claim about the same
    # line, and reporting both would make every fix look like two.
    written = set()

    class Walk(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def generic_visit(self, node):
            prop = _writes(node)
            if prop is not None:
                where = ".".join(stack) if stack else "<module>"
                sites.append(Site(module, where, prop, "A", node.lineno))
                for sub in ast.walk(node):
                    if (isinstance(sub, ast.Attribute)
                            and sub.attr == HANDLE):
                        written.add(id(sub))
            ast.NodeVisitor.generic_visit(self, node)

        def visit_Attribute(self, node):
            if node.attr == HANDLE and id(node) not in written:
                where = ".".join(stack) if stack else "<module>"
                sites.append(Site(module, where, "*", "B", node.lineno))
            self.generic_visit(node)

        def visit_Call(self, node):
            # `getattr(playerManager, "_player", None)` is the same reach
            # with the handle spelled as a string, so no Attribute node
            # carries it and the walk above goes straight past. It was in
            # this package the first time this file was run.
            if (isinstance(node.func, ast.Name)
                    and node.func.id == "getattr"
                    and len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and node.args[1].value == HANDLE):
                where = ".".join(stack) if stack else "<module>"
                sites.append(Site(module, where, "*", "B", node.lineno))
            self.generic_visit(node)

    Walk().visit(tree)

    # A read reported as `*` is really a read of whatever is taken off it;
    # name it where the source says so, so the declaration is about a
    # property rather than about a line.
    src_lines = src.splitlines()
    for site in sites:
        if site.rule != "B":
            continue
        line = src_lines[site.line - 1]
        after = line.split("%s." % HANDLE, 1)
        if len(after) == 2:
            name = ""
            for ch in after[1]:
                if ch.isalnum() or ch == "_":
                    name += ch
                else:
                    break
            if name:
                site.prop = name
    return sites


def modules():
    """Every gateway module, sorted."""
    base = os.path.join(ROOT, GATEWAY)
    return [os.path.join(base, n) for n in sorted(os.listdir(base))
            if n.endswith(".py")]


def sites():
    out = []
    for path in modules():
        out.extend(_scan(path))
    return out


def audit():
    """Sites with no `DECLARED` entry."""
    return [s for s in sites() if s.key not in DECLARED]


def stale():
    """Declared keys that match no site -- pre-authorisations."""
    live = {s.key for s in sites()}
    return sorted(k for k in DECLARED if k not in live)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true",
                    help="list every site, declared or not")
    args = ap.parse_args(argv)

    if args.verbose:
        for site in sites():
            print("%-52s line %-5d %s" % (
                site.key, site.line,
                "declared" if site.key in DECLARED else "UNDECLARED"))
        print()

    bad = 0
    for site in audit():
        bad += 1
        print("%s/%s:%d: rule %s -- %s reaches `_player.%s` past "
              "PlayerManager" % (GATEWAY, site.module, site.line, site.rule,
                                 site.func, site.prop))
    for key in stale():
        bad += 1
        print("DECLARED holds %r and nothing matches it. A key for a site "
              "that is gone pre-authorises the next one." % key)
    if bad:
        print("\n%d finding(s). Route it through the PlayerManager method, "
              "or add the site to DECLARED with the reason it is the right "
              "shape." % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
