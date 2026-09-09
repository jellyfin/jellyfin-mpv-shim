#!/usr/bin/env python3
"""Every literal mpv key name that a setting can move has to be declared.

Seven settings name a key the user may change -- `ui_select_key`,
`hud_wake_key` and the `kb_menu_*` group -- and the code that acts on those
keys writes the *default* spelling as a literal in a good many places. Some
of those are correct and some are the bug, and they look identical:

* **R1** `player.py`'s `_NAV_KEYPRESS` resolves `"ok"` per press *"because
  it is the one the user may remap"* -- and freezes `"back"` as `"ESC"` two
  lines below, while `kb_menu_esc` is equally remappable.
* **R2** `_shell_claimed_keys` claims the literal `"SPACE"` rather than
  deriving from the bound `kb_*` set.
* **R3** `PHUD_SUMMON_KEYS` holds a literal `'ENTER'`, under a setting whose
  own comment says *"the renderer stops force-binding ENTER when this moves,
  which is the point"*.
* **R10** `phud_skip_bind` calls `phud_wake_key()` to decide whether to drop
  the wake binding, then hardcodes `'ENTER'` on the next two lines -- so the
  Skip button binds ENTER even when `ui_select_key` has moved.

All four are in `docs/RISK_MAP_2026-09.md` section 2. **R10 was found by
writing this predicate down, not by reading the file** -- it sits one screen
from R3, inside a function that resolves the very setting it then hardcodes,
and four surveys plus a full session of hand work had walked past it. That
is the argument for the instrument rather than more attention, and it is
this file's whole warrant.

**Running it added three more to the four.** R2 has a second site: the
`SPACE` claim and the `SPACE` dispatch freeze `kb_pause` independently, and
the risk map records only the claim. `phud_bind_summon` compares the loop's
key against a literal `ENTER` to pick its handler, so resolving R3's table
would not by itself fix it. And `phud_bind_wake` compares the RESOLVED wake
key against `ENTER` to decide whether waking the HUD also toggles pause --
which is a product question nobody has answered, not a freeze, and it is
declared as such.

**No correctness judgement, because a checker cannot make one.** A frozen
literal is right wherever the key is mpv's own and not ours to move. What is
required is that the site be *declared*, which puts the question in front of
whoever adds one -- and the declaration is where the next reader finds out a
resolver exists at all.

## What it does not cover, on purpose

**Single-character and punctuation defaults are excluded** -- `kb_stop`
(`q`), `kb_watched` (`w`), `kb_menu` (`c`), `kb_fullscreen` (`f`),
`kb_kill_shader` (`k`), `kb_prev`/`kb_next` (`<`, `>`). A one-letter literal
collides with everything: the first run matched a debug overlay's `'F'`
glyph on `kb_fullscreen`. None of the four measured defects is one of these,
and every one of them is a multi-character name.

**Matching is case-sensitive on the mpv spelling.** `conf.py` writes the
menu keys lowercase (`kb_menu_esc = "esc"`) and every binding site writes
them uppercase, which is also what separates a key name from a layout
direction -- `'left'` in `renderer.lua` is an alignment and `'LEFT'` is a
key. So a frozen key written lowercase in binding code is invisible here.

Usage:  tools/audit_frozen_key_literals.py [--verbose]
Exit 1 if any site is undeclared, or any declaration has no site.
`tests/test_no_frozen_key_literals.py` is the guard.
"""

import argparse
import ast
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: Where the keys are acted on. `conf.py` itself is not here -- it is the
#: source of the defaults, so every literal in it is the declaration.
PY_FILES = (
    "jellyfin_mpv_shim/player.py",
    "jellyfin_mpv_shim/mpvtk_browser/app.py",
)
LUA_FILES = (
    "jellyfin_mpv_shim/mpvtk/renderer.lua",
)

#: Settings whose value IS an mpv key name.
SETTING_PREFIX = "kb_"
SETTING_NAMES = ("ui_select_key", "hud_wake_key")


#: A declaration's first field. `OPEN` means "a finding somebody has looked
#: at and not repaired"; `OK` means "the literal is right here". A field
#: rather than a word in the prose: the guard has to list the open ones, and
#: reading that off how a sentence is phrased is how a repaired row goes on
#: claiming to be a bug.
OPEN = "open"
OK = "ok"


class Site:
    def __init__(self, path, scope, literal, line):
        self.path = path
        self.scope = scope
        self.literal = literal
        self.line = line

    @property
    def key(self):
        return "%s:%s" % (os.path.basename(self.path), self.scope)

    def __repr__(self):
        return "<%s %r line %d>" % (self.key, self.literal, self.line)


#: Declared sites, by `<file>:<scope>`. A scope is the enclosing def (or
#: class, or `<module>` for a file-scope constant), so one entry covers
#: every key literal in it -- the question is about the function, not about
#: each spelling.
#:
#: As in `tools/audit_owned_state.py`, a key here records a site that
#: EXISTS. One that matches nothing pre-authorises the next literal in a
#: scope by that name, and the guard fails on it.
DECLARED = {
    # ---------------------------------------------------------- OPEN BUGS
    # Declared as findings, not exemptions. All are verified open in
    # docs/RISK_MAP_2026-09.md section 2, and all need a config-file edit to
    # reach -- which is why section 7 defers them rather than repairing them
    # under release pressure.
    "player.py:PlayerManager": (
        OPEN,
        "R1 -- OPEN. `_NAV_KEYPRESS` freezes `back` as `ESC` while "
        "`kb_menu_esc` can move it, in a table whose own comment explains "
        "that `ok` is resolved per press precisely because the user may "
        "remap it. The four arrows beside it are `kb_menu_*` and are the "
        "same question, which nobody has asked of them."
    ),
    "app.py:MpvtkBrowser._shell_claimed_keys": (
        OPEN,
        "R2 -- OPEN. Claims the literal `SPACE` alongside the volume keys "
        "instead of deriving from the bound `kb_*` set, so moving "
        "`kb_pause` leaves the shell claiming a key the player no longer "
        "uses."
    ),
    "app.py:MpvtkBrowser._shell_key": (
        OPEN,
        "R2's SECOND SITE, and the risk map records only the first. The "
        "dispatch half compares `key == \"SPACE\"`, so claim and answer "
        "freeze the same setting independently and moving `kb_pause` has "
        "to be got right in both. Found by this predicate."
    ),
    "renderer.lua:PHUD_SUMMON_KEYS": (
        OPEN,
        "R3 -- OPEN. A literal `{ 'UP', 'DOWN', 'LEFT', 'RIGHT', 'ENTER' }`, "
        "under a setting (`ui_select_key`) whose comment says the renderer "
        "stops force-binding ENTER when it moves. Contrast `NAV_KEYS` "
        "below, which is the same list WITH a resolver."
    ),
    "renderer.lua:phud_skip_bind": (
        OPEN,
        "R10 -- OPEN, and the one this predicate found rather than a "
        "reader. The function asks `phud_wake_key()` on one line to decide "
        "whether to drop the wake binding, then removes "
        "`mpvtk_summon_ENTER` and force-binds a literal `ENTER` on the next "
        "two -- so the Skip button takes ENTER even when `ui_select_key` "
        "has moved. Also `docs/do-not-fix.md` F35, which records it as "
        "deliberately left and says what it is waiting on: a decision about "
        "whether the idle Skip offer follows `hud_wake_key` or "
        "`ui_select_key`."
    ),
    "renderer.lua:phud_bind_summon": (
        OPEN,
        "Downstream of R3, and open with it: inside the loop over "
        "`PHUD_SUMMON_KEYS` it compares the loop's key against a literal "
        "`ENTER` to choose the pause-toggling handler. Resolving the table "
        "alone would not fix this line."
    ),
    "renderer.lua:phud_bind_wake": (
        OPEN,
        "OPEN QUESTION rather than a freeze, and it needs a product answer "
        "before it needs a patch. `wk` here is the RESOLVED wake key, and "
        "it is compared against `ENTER` to decide whether waking the HUD "
        "also toggles pause. So moving `hud_wake_key` silently drops the "
        "pause half. Whether that is intended is written down nowhere, and "
        "it is the same unanswered question `docs/do-not-fix.md` F35 parks "
        "one function away."
    ),

    # ------------------------------------- resolvers, and their own default
    "renderer.lua:phud_wake_key": (
        OK,
        "This IS the resolver -- `state.phud.wake_key or 'ENTER'`. The "
        "literal is the fallback for a renderer the Python side has not "
        "told yet, which is the one place a default belongs."
    ),
    "renderer.lua:state": (
        OK,
        "`select_key = 'ENTER'` in the renderer's initial state table: the "
        "value `mpvtk-select-key` overwrites at startup. A default, not a "
        "freeze."
    ),
    "renderer.lua:mpvtk-select-key": (
        OK,
        "The handler that overwrites it. The literal is what an empty "
        "message falls back to."
    ),
    "renderer.lua:mpvtk-hud": (
        OK,
        "`state.phud.wake_key = (opts and opts.key) or 'ENTER'` -- the "
        "fallback when the app engages the HUD without naming a key."
    ),
    "renderer.lua:NAV_KEYS": (
        OK,
        "The same five names as `PHUD_SUMMON_KEYS` and the reason that one "
        "is a finding: the ENTER row carries a third field, and "
        "`keyclaim.nav_key` substitutes `state.select_key` for it. The "
        "literal is the DEFAULT of a resolved lookup."
    ),
    "renderer.lua:keyclaim.nav_key": (
        OK,
        "The substitution itself: `k[3] and (state.select_key or 'ENTER') "
        "or k[1]`. The literal is the fallback inside the resolver."
    ),

    # ------------------------------------------- mpv's own keys, not ours
    "renderer.lua:phud_summon": (
        OK,
        "Force-binds `ESC` to dismiss the summoned bar. mpv's ESC, not "
        "`kb_menu_esc`: that setting is the OSD MENU's escape, and someone "
        "who moved it did not ask for the HUD's to move with it."
    ),
    "renderer.lua:reconcile": (
        OK,
        "Force-binds `ESC` to dismiss a scene menu or modal, and drops it "
        "again when neither is present. Same reading as `phud_summon`: it "
        "is the renderer's own overlay, not the OSD menu `kb_menu_esc` "
        "names."
    ),
    "renderer.lua:mp.set_key_bindings": (
        OK,
        "The `mpvtk_thumb` section: `mbtn_back` synthesizes `keypress ESC` "
        "rather than reimplementing the app's back ladder, and the comment "
        "above it says so -- \"ESC is still ESC\". The literal is the "
        "thing being deferred TO."
    ),

    # ------------------------------- action names that happen to look like
    # keys. These are the text box's vocabulary, on the far side of the
    # key->op map: `bind` translates once and everything below speaks ops.
    "renderer.lua:bind": (
        OK,
        "`bind_text_keys`'s mpv-key -> op table (`['KP_ENTER'] = 'ENTER'`). "
        "The left-hand side is what mpv delivers to a focused text box, "
        "which takes the whole keyboard by design; the right-hand side is "
        "the op name. Neither is a setting's value."
    ),
    "renderer.lua:tb_key": (
        OK,
        "The op dispatch on the other side of that table. `name` here is an "
        "op, and the fact that most ops are spelled like their default key "
        "is what makes this file's grep unable to tell them apart."
    ),
    "renderer.lua:nav_activate": (
        OK,
        "Hands the text box the `ENTER` op when spatial nav activates a "
        "focused box. An op, per `tb_key` above."
    ),
    "renderer.lua:nav_move": (
        OK,
        "Hands the text box `LEFT`/`RIGHT` ops when spatial nav moves "
        "inside one. Ops, per `tb_key` above."
    ),
}


def _key_names():
    """{"ENTER", "ESC", ...} -- multi-character defaults of key settings.

    Read from `conf.py` rather than listed here, so a new `kb_*` setting is
    covered the day it is added and a renamed one stops being checked
    loudly rather than quietly.
    """
    path = os.path.join(ROOT, "jellyfin_mpv_shim", "conf.py")
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    out = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AnnAssign)
                and isinstance(node.target, ast.Name)):
            continue
        name = node.target.id
        if not (name.startswith(SETTING_PREFIX) or name in SETTING_NAMES):
            continue
        value = node.value
        if not (isinstance(value, ast.Constant)
                and isinstance(value.value, str)):
            continue
        if len(value.value) > 1 and value.value.isalpha():
            out.add(value.value.upper())
    return out


def _scan_py(path, keys):
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    sites, stack = [], []

    class Walk(ast.NodeVisitor):
        def visit_FunctionDef(self, node):
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_ClassDef(self, node):
            # Kept in the scope, because a class-body constant is exactly
            # where R1 lives and "<module>" would not say so.
            stack.append(node.name)
            self.generic_visit(node)
            stack.pop()

        def visit_Constant(self, node):
            if isinstance(node.value, str) and node.value in keys:
                sites.append(Site(path, ".".join(stack) or "<module>",
                                  node.value, node.lineno))
            self.generic_visit(node)

    Walk().visit(tree)
    return sites


#: `function name(`, `local function name(`, `name = function(`.
_LUA_DEF = re.compile(
    r"^\s*(?:local\s+)?function\s+([\w.:]+)"
    r"|^\s*(?:local\s+)?([\w.]+)\s*=\s*function")
#: File-scope anchors that are not functions. Without these, everything
#: between two functions collapses into one `<module>` scope -- in this
#: file that is ten unrelated sites under one declaration, which is a
#: blanket exemption wearing a reason.
_LUA_ANCHOR = re.compile(
    r"^(?:local\s+)?([\w.]+)\s*="              # local NAV_KEYS = {
    r"|^mp\.register_script_message\('([\w-]+)'"
    r"|^(mp\.set_key_bindings)\(")
_LUA_STR = re.compile(r"'([^'\n]*)'|\"([^\"\n]*)\"")


def _scan_lua(path, keys):
    """Lua has no AST here, so scope is tracked by the last definition seen.

    A file-scope `end` in column 0 closes the current function; anything
    nested is charged to the outermost one, which is the right grain here
    -- the question is about the function, not about each closure inside
    it. It over-attributes and never misses a literal.
    """
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    sites, scope = [], "<module>"
    for lineno, line in enumerate(lines, 1):
        hit = _LUA_DEF.match(line)
        anchor = None if hit else _LUA_ANCHOR.match(line)
        if hit:
            scope = hit.group(1) or hit.group(2)
        elif anchor:
            scope = (anchor.group(1) or anchor.group(2)
                     or anchor.group(3))
        elif line == "end" or line.startswith("end)"):
            # A file-scope function closes with `end` in column 0. Without
            # this the next file-scope constant is charged to whatever
            # function happened to be above it -- which is how the FIRST
            # run put `PHUD_SUMMON_KEYS` (R3) inside `phud_wake_key` and
            # invited a declaration naming the wrong site.
            scope = "<module>"
        if line.lstrip().startswith("--"):
            continue          # a key name in prose is not a binding
        for match in _LUA_STR.finditer(line):
            value = match.group(1) or match.group(2)
            if value in keys:
                sites.append(Site(path, scope, value, lineno))
    return sites


def sites():
    keys = _key_names()
    out = []
    for rel in PY_FILES:
        out.extend(_scan_py(os.path.join(ROOT, rel), keys))
    for rel in LUA_FILES:
        out.extend(_scan_lua(os.path.join(ROOT, rel), keys))
    return out


def undeclared():
    return [s for s in sites() if s.key not in DECLARED]


def open_findings():
    """Declared sites that are known bugs rather than settled ones."""
    return sorted(k for k, (status, _why) in DECLARED.items()
                  if status == OPEN)


def stale():
    live = {s.key for s in sites()}
    return sorted(k for k in DECLARED if k not in live)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args(argv)

    keys = _key_names()
    if args.verbose:
        print("key names from conf.py: %s\n" % ", ".join(sorted(keys)))
        seen = {}
        for site in sites():
            seen.setdefault(site.key, set()).add(site.literal)
        for key in sorted(seen):
            print("%-46s %-28s %s"
                  % (key, ",".join(sorted(seen[key])),
                     "declared" if key in DECLARED else "UNDECLARED"))
        print()

    bad = 0
    for site in undeclared():
        bad += 1
        print("%s:%d: %r is a frozen key literal in %s"
              % (os.path.relpath(site.path, ROOT), site.line, site.literal,
                 site.scope))
    for key in stale():
        bad += 1
        print("DECLARED holds %r and no site matches it." % key)
    if bad:
        print("\n%d finding(s). Resolve the setting, or declare the site "
              "with the reason the literal is right." % bad)
    for key in open_findings():
        print("note: %s is a declared OPEN finding, not an exemption" % key)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
