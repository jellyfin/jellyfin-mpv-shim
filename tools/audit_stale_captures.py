#!/usr/bin/env python3
"""Find handlers that fire with state read at draw time rather than now.

A browser screen is rebuilt from scratch on every repaint, so its widget
tree is a *snapshot*. A handler built like this::

    aid, sid = self._effective_tracks(item)          # read while drawing
    Button(_("Play"), on_click=lambda: play(item, aid=aid, sid=sid))

fires with the tracks as of that draw. The track pickers write straight to
the route and force no repaint -- nothing drawn depends on them -- so the
button goes on playing the *previous* selection until some unrelated
repaint (a thumbnail landing, a websocket item update) happens to rebuild
the closure. That is the shape this looks for, and it is why the symptom
reads as "sometimes it works": how long you sat on the page decided it.

**No test can see this**, which is why a static check earns its keep.
`build_scene` renders when asked, so it draws a correct tree whether or not
the app would ever have redrawn -- and a `build_scene` between a click and
its assertion silently refreshes every closure on screen, which is exactly
how the detail page's version of this passed its own test for months.

What is reported: a local read from mutable state (the route, a dialog's
state dict, a `self._foo` scratch dict, or a method that reads one) that a
handler lambda then closes over. Reading the container *inside* the handler
is the safe form and is ignored -- so is a lambda passed to something that
is not building a widget, since that call is already running at handler
time.

Findings that have been read and are fine live in ACCEPTED below, with the
reason. Anything else exits 1. Adding an entry is a claim that the value
cannot change between the draw and the press -- for route identity it
cannot, because a different id is a different route.

**A lead generator, not a proof.** It follows a call one frame deep and no
further, so state laundered through two helpers is invisible to it; and it
says nothing about the mirror-image bug, a handler that writes state
without asking for a repaint (see CLAUDE.md -- a Checkbox cannot redraw
itself).

Usage:
    tools/audit_stale_captures.py [package_dir ...]   # default: the package
    tools/audit_stale_captures.py --all               # ignore ACCEPTED
"""

import argparse
import ast
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_ROOT = os.path.join(os.path.dirname(HERE), "jellyfin_mpv_shim")

#: Keyword arguments whose value is invoked later, by the renderer.
HANDLER_KW = {
    "on_click", "on_select", "on_change", "on_submit", "on_commit",
    "on_toggle", "on_dismiss", "on_scroll", "on_context", "on_dbl",
    "on_cancel", "on_hover", "on_enter", "on_activate", "on_pick",
}

#: Lowercase callables that build a widget and take their handler
#: positionally. A capitalized callee is assumed to be a widget class.
#: Anything else called with a lambda is doing work *at* handler time, so
#: whatever it closes over was read at handler time too.
WIDGET_HELPERS = {
    "action_btn", "nav_button", "icon_btn", "tile_row", "menu_item",
    "picker_list", "_picker_list", "chip", "toggle_btn", "text_btn",
}

#: Containers a handler can write to without a repaint following.
MUTABLE_ROOTS = {"route", "state", "self.route"}

#: Findings that have been read and are sound: "file::scope::local".
#: Every one of these is an *identity* -- which server, which channel, which
#: playlist -- and identity is what a route is made of. Changing it means
#: navigating, and a navigation builds a new tree; there is no gesture that
#: leaves you on this page with a different one. Contrast a track pick,
#: which is a choice ABOUT the thing the route names, and can therefore
#: move under a button that is already drawn.
ACCEPTED = {
    "jellyfin_mpv_shim/mpvtk_browser/pages/livetv.py::"
    "ProgramPage._buttons::server":
        "_srv() is route['server'], never reassigned in place",
    "jellyfin_mpv_shim/mpvtk_browser/pages/livetv.py::"
    "ProgramPage._buttons::channel":
        "the programme's own ChannelId, off the loaded item",
    "jellyfin_mpv_shim/mpvtk_browser/pages/livetv.py::"
    "ChannelPage._buttons::server":
        "_srv() is route['server']",
    "jellyfin_mpv_shim/mpvtk_browser/pages/livetv.py::"
    "ChannelPage._buttons::channel":
        "route['_channel'] -- the channel this page IS. Live TV routes do "
        "re-read themselves in place, but a refresh returns the same "
        "channel; a different one is a different route",
    "jellyfin_mpv_shim/mpvtk_browser/pages/livetv.py::"
    "ChannelPage._buttons::channel_id":
        "that channel's Id, or route['item_id']",
    "jellyfin_mpv_shim/mpvtk_browser/pages/playlist.py::"
    "PlaylistPage.render::server":
        "route['server']",
}


def qualname(stack):
    return ".".join(stack)


def container_name(node):
    """`route` / `state` / `self._foo` at the base of a read, or None."""
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
            and node.func.attr in ("get", "setdefault"):
        node = node.func.value
    if isinstance(node, ast.Subscript):
        node = node.value
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
            and node.value.id == "self":
        return "self." + node.attr
    return None


def reads_mutable(expr, stateful):
    """Mutable containers this expression reads from, directly or one call
    down. The indirection matters: the detail page's bug was
    ``self._effective_tracks(item)``, whose read is a frame away."""
    found = set()
    for n in ast.walk(expr):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and isinstance(n.func.value, ast.Name) \
                and n.func.value.id == "self" and n.func.attr in stateful:
            found.add("self.%s()" % n.func.attr)
        if isinstance(n, (ast.Subscript, ast.Call)):
            name = container_name(n)
            if name and (name in MUTABLE_ROOTS or name.startswith("self._")):
                found.add(name)
    return found


def widget_call(node):
    """Whether this Call builds a widget, so a lambda in it is a handler."""
    func = node.func
    name = func.attr if isinstance(func, ast.Attribute) else (
        func.id if isinstance(func, ast.Name) else "")
    return bool(name) and (name[:1].isupper() or name in WIDGET_HELPERS)


def handler_lambdas(fn):
    for n in ast.walk(fn):
        if not isinstance(n, ast.Call):
            continue
        for kw in n.keywords:
            if kw.arg in HANDLER_KW and isinstance(kw.value, ast.Lambda):
                yield kw.arg, kw.value
        if widget_call(n):
            for a in n.args:
                if isinstance(a, ast.Lambda):
                    yield "positional", a


def free_names(lam):
    """Names the lambda loads that it did not bind itself. A default
    argument (``lambda i, opts=options: ...``) binds its name, and is the
    idiom for capturing on purpose."""
    args = lam.args
    bound = {a.arg for a in args.args + args.kwonlyargs + args.posonlyargs}
    if args.vararg:
        bound.add(args.vararg.arg)
    if args.kwarg:
        bound.add(args.kwarg.arg)
    return {n.id for n in ast.walk(lam)
            if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)} - bound


def audit_function(fn, stateful):
    """(local, read_line, containers, lambda_line, kw) per stale capture."""
    tainted = {}
    for stmt in ast.walk(fn):
        if not isinstance(stmt, (ast.Assign, ast.AnnAssign)) or not stmt.value:
            continue
        containers = reads_mutable(stmt.value, stateful)
        if not containers:
            continue
        targets = stmt.targets if isinstance(stmt, ast.Assign) \
            else [stmt.target]
        for t in targets:
            for el in (t.elts if isinstance(t, (ast.Tuple, ast.List))
                       else [t]):
                if isinstance(el, ast.Name):
                    tainted[el.id] = (stmt.lineno, sorted(containers))
    seen = set()
    for kw, lam in handler_lambdas(fn):
        if reads_mutable(lam, stateful):
            continue          # re-reads the container itself: the safe form
        for name in sorted(free_names(lam) & set(tainted)):
            if name in seen:
                continue      # one local, however many buttons carry it
            seen.add(name)
            line, containers = tainted[name]
            yield name, line, containers, lam.lineno, kw


def scan_file(path, rel):
    """Findings in one file, as (key, detail) pairs."""
    try:
        tree = ast.parse(open(path, encoding="utf-8").read())
    except SyntaxError as exc:
        print("%s: could not parse (%s)" % (rel, exc), file=sys.stderr)
        return

    stateful = {fn.name for fn in ast.walk(tree)
                if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and any(reads_mutable(s, ()) for s in fn.body)}

    def walk(node, stack):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                yield from walk(child, stack + [child.name])
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                scope = qualname(stack + [child.name])
                for name, line, cont, lam_line, kw in \
                        audit_function(child, stateful):
                    yield ("%s::%s::%s" % (rel, scope, name),
                           "%s:%d  %s() captures %r, read at :%d from %s "
                           "(%s handler)" % (rel, lam_line, scope, name,
                                             line, ", ".join(cont), kw))
                # Nested defs are builders too (dialog `build()`), but their
                # own locals are what they close over -- recurse for classes
                # defined inside, not for the function body again.
            else:
                yield from walk(child, stack)

    yield from walk(tree, [])


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("roots", nargs="*", default=[DEFAULT_ROOT])
    ap.add_argument("--all", action="store_true",
                    help="report accepted findings too")
    args = ap.parse_args(argv)

    new, accepted = [], []
    for root in args.roots:
        base = os.path.dirname(root.rstrip("/"))
        for dirpath, _dirs, files in os.walk(root):
            for f in sorted(files):
                if not f.endswith(".py"):
                    continue
                path = os.path.join(dirpath, f)
                rel = os.path.relpath(path, base)
                for key, detail in scan_file(path, rel):
                    if key in ACCEPTED and not args.all:
                        accepted.append((key, detail))
                    else:
                        new.append((key, detail))

    for _key, detail in new:
        print(detail)
    if accepted:
        print("\n%d accepted (see ACCEPTED in this file)" % len(accepted))
    if new:
        print("\n%d capture(s) to look at. Read the state inside the "
              "handler, or add an entry to ACCEPTED saying why it cannot "
              "go stale." % len(new))
        return 1
    print("\nno stale captures")
    return 0


if __name__ == "__main__":
    sys.exit(main())
